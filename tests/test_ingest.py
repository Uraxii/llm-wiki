"""Ingest: the serial per-source pipeline (store, summarize, dedup)."""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import parse_frontmatter  # noqa: E402
from llmwiki.model import API_KEY_FILE_VAR, API_KEY_VAR  # noqa: E402
from llmwiki import ingest, summarize  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402


@contextmanager
def _env(values: dict):
    sentinel = object()
    previous = {key: os.environ.get(key, sentinel) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


GOOD_REPLY = (
    "---\n"
    "title: Some Title\n"
    "identifiers: []\n"
    "---\n\n"
    "An abstract.\n"
)

# Missing `identifiers` entirely fails summarize's own self-lint, giving a
# deterministic "summarize failed" without needing a second fake endpoint.
BAD_REPLY = "---\ntitle: Bad Title\n---\n\nBad body.\n"


STATUSES = {"new", "exists", "failed"}


def _records(stdout: str) -> list[list[str]]:
    """Ingest's own report lines. `summarize` and `dedup` print planned
    counts here too (no tabs), and `dedup.push` prints a warning that is
    ALSO three tab-separated fields, so the status column is what
    identifies a record."""
    rows = [line.split("\t") for line in stdout.split("\n")]
    return [r for r in rows if len(r) == 3 and r[1] in STATUSES]


def _run(root, args):
    """ingest.run, returning (code, records) with `_records` picking
    ingest's own per-source lines out of everything else on stdout."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = ingest.run(root, args)
    return code, _records(out.getvalue())


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        env_cm = _env({API_KEY_VAR: "test-key", API_KEY_FILE_VAR: None})
        env_cm.__enter__()
        self.addCleanup(env_cm.__exit__, None, None, None)

    def _write_config(self, url: str, identifiers: str = "") -> None:
        (self.root / "config.toml").write_text(
            '[models]\nsummarize = "cheap"\n\n'
            f'[endpoint]\nurl = "{url}"\n\n' + identifiers
        )

    def _write_source_file(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text)
        return path

    def test_file_ingest_reports_new_and_writes_everything(self) -> None:
        path = self._write_source_file("widget.txt", "Widget source text.")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code, records = _run(self.root, [str(path)])

        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        digest, status, url = records[0]
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # hex
        self.assertEqual(status, "new")
        self.assertEqual(url, str(path.resolve()))

        self.assertTrue((self.root / "sources" / f"{digest}.txt").is_file())
        self.assertTrue((self.root / "sources" / f"{digest}.toml").is_file())
        # One summary page (dedup also places a story page alongside it,
        # which is not this assertion's concern).
        summaries = [
            p
            for p in (self.root / "wiki").glob("*.md")
            if parse_frontmatter(p.read_text())[0].get("kind") == "summary"
        ]
        self.assertEqual(len(summaries), 1)
        fields, _body = parse_frontmatter(summaries[0].read_text())
        self.assertEqual(fields["source"], digest)

        log = (self.root / "log.md").read_text()
        self.assertIn("## [ingest] manual 1 new, 0 exists, 0 failed", log)

    def test_same_file_twice_second_is_exists_one_model_call(self) -> None:
        path = self._write_source_file("widget.txt", "Widget source text.")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code, records = _run(self.root, [str(path), str(path)])
            self.assertEqual(len(fake.requests), 1)

        self.assertEqual(code, 0)
        self.assertEqual(len(records), 2)
        _digest0, status0, _url0 = records[0]
        _digest1, status1, _url1 = records[1]
        self.assertEqual(status0, "new")
        self.assertEqual(status1, "exists")

    def test_missing_path_mid_argv_continues_and_reports_failed(self) -> None:
        good = self._write_source_file("widget.txt", "Widget source text.")
        missing = self.tmp / "does-not-exist.txt"

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code, records = _run(self.root, [str(missing), str(good)])

        self.assertEqual(code, 1)
        self.assertEqual(len(records), 2)
        digest0, status0, url0 = records[0]
        digest1, status1, _url1 = records[1]
        self.assertEqual(digest0, "-")
        self.assertEqual(status0, "failed")
        self.assertEqual(url0, str(missing.resolve()))
        self.assertEqual(status1, "new")
        self.assertEqual(len(digest1), 64)

        log = (self.root / "log.md").read_text()
        self.assertIn(str(missing.resolve()), log)

    def test_missing_path_writes_to_stderr_and_log(self) -> None:
        missing = self.tmp / "does-not-exist.txt"

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            err = io.StringIO()
            out = io.StringIO()
            from contextlib import redirect_stderr

            with redirect_stdout(out), redirect_stderr(err):
                code = ingest.run(self.root, [str(missing)])

        self.assertEqual(code, 1)
        self.assertIn(str(missing.resolve()), err.getvalue())
        self.assertIn(str(missing.resolve()), (self.root / "log.md").read_text())

    def test_failing_summarize_leaves_source_prints_failed_continues(self) -> None:
        bad = self._write_source_file("bad.txt", "Bad source text.")
        good = self._write_source_file("good.txt", "Good source text.")
        replies = iter([BAD_REPLY, GOOD_REPLY])

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": next(replies)}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code, records = _run(self.root, [str(bad), str(good)])

        self.assertEqual(code, 1)
        self.assertEqual(len(records), 2)
        digest0, status0, _url0 = records[0]
        _digest1, status1, _url1 = records[1]
        self.assertEqual(status0, "failed")
        self.assertEqual(status1, "new")

        # The bad source's bytes are still on disk; only its page dropped.
        self.assertTrue((self.root / "sources" / f"{digest0}.txt").is_file())
        # One summary page: the bad source's page was dropped by
        # summarize's own self-lint (dedup never ran for it), and the
        # good source's summary and its dedup-placed story page remain.
        summaries = [
            p
            for p in (self.root / "wiki").glob("*.md")
            if parse_frontmatter(p.read_text())[0].get("kind") == "summary"
        ]
        self.assertEqual(len(summaries), 1)

    def test_dropped_source_is_retried_by_a_later_bare_summarize_run(self) -> None:
        """The phase spec's retry story: a source whose summarize reply
        failed self-lint is not marked done, so a later `summarize.run`
        over every stored source (not just the ones named on an argv)
        picks it back up once the reply is fixed."""
        path = self._write_source_file("widget.txt", "Widget source text.")
        reply = BAD_REPLY

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code, records = _run(self.root, [str(path)])
            digest, status, _url = records[0]

            self.assertEqual(code, 1)
            self.assertEqual(status, "failed")
            self.assertEqual(list((self.root / "wiki").glob("*.md")), [])

            reply = GOOD_REPLY
            with redirect_stdout(io.StringIO()):
                retry_code = summarize.run(self.root, None)

        self.assertEqual(retry_code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, _body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["kind"], "summary")
        self.assertEqual(fields["source"], digest)

    def test_a_push_warning_does_not_masquerade_as_an_ingest_record(self) -> None:
        """`dedup.push` prints `push\\t<path>\\t<identifiers>` on the same
        stdout ingest writes to, and that is ALSO three tab-separated
        fields. `_records` must pick out ingest's own line by status
        column, not by "three fields" or "has a tab" alone."""
        (self.root / "wiki" / "agent-note.md").write_text(
            "---\n"
            "kind: note\n"
            "title: Agent Note\n"
            "identifiers:\n"
            "  - isbn:1234567890123\n"
            "---\n\n"
            "An agent-owned page, not a CLI summary.\n"
        )
        path = self._write_source_file("widget.txt", "Widget source text.")
        reply = (
            "---\n"
            "title: Shared Identifier Widget\n"
            "identifiers:\n"
            "  - isbn:1234567890123\n"
            "---\n\n"
            "An abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, '[identifiers.isbn]\npattern = "^[0-9]{13}$"\n')
            out = io.StringIO()
            with redirect_stdout(out):
                code = ingest.run(self.root, [str(path)])
        stdout = out.getvalue()

        self.assertEqual(code, 0)
        # The collision must really have fired, or this test proves
        # nothing.
        self.assertIn("push\t", stdout)

        records = _records(stdout)
        self.assertEqual(len(records), 1)
        digest, status, _url = records[0]
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # hex, not the literal "push"
        self.assertEqual(status, "new")

    def test_stdin_zero_bytes_fails_and_stores_nothing(self) -> None:
        import unittest.mock

        with unittest.mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.buffer.read.return_value = b""
            code, records = _run(self.root, ["-"])

        self.assertEqual(code, 1)
        self.assertEqual(records, [["-", "failed", "-"]])
        self.assertEqual(list((self.root / "sources").iterdir()), [])

    def test_directory_path_fails_and_continues(self) -> None:
        subdir = self.tmp / "adir"
        subdir.mkdir()

        code, records = _run(self.root, [str(subdir)])

        self.assertEqual(code, 1)
        self.assertEqual(len(records), 1)
        digest, status, url = records[0]
        self.assertEqual(digest, "-")
        self.assertEqual(status, "failed")
        self.assertEqual(url, str(subdir.resolve()))

    def test_empty_argv_logs_all_zero_and_returns_0(self) -> None:
        code, records = _run(self.root, [])

        self.assertEqual(code, 0)
        self.assertEqual(records, [])
        log = (self.root / "log.md").read_text()
        self.assertIn("## [ingest] manual 0 new, 0 exists, 0 failed", log)


if __name__ == "__main__":
    unittest.main()
