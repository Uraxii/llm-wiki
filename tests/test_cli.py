"""Subprocess smoke test of every verb in the CLI's verb table."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki import cli, remotes  # noqa: E402
from llmwiki.model import API_KEY_VAR  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from fake_wiki import FakeWiki, Reply  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

GOOD_REPLY = (
    "---\n"
    "title: Some Title\n"
    "identifiers: []\n"
    "---\n\n"
    "An abstract.\n"
)

STATUSES = {"new", "exists", "failed"}


def _records(stdout: str) -> list[list[str]]:
    """Ingest's own report lines. `summarize` and `dedup` print planned
    counts here too (no tabs), and `dedup.push` prints a warning that is
    ALSO three tab-separated fields, so the status column is what
    identifies a record."""
    rows = [line.split("\t") for line in stdout.split("\n")]
    return [r for r in rows if len(r) == 3 and r[1] in STATUSES]


class VerbTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.kb = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.kb / sub).mkdir(parents=True)
        (self.kb / "log.md").write_text("# log\n")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        self.fake = FakeEndpoint(respond)
        self.addCleanup(self.fake.close)
        (self.kb / "config.toml").write_text(
            '[models]\nsummarize = "cheap"\n\n'
            f'[endpoint]\nurl = "{self.fake.url}"\n'
        )
        self.env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), API_KEY_VAR: "fake-key"}

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.kb), *args]
        return subprocess.run(
            cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env
        )

    def _ingest(self, source: Path) -> str:
        """Ingest `source` and return its digest, selecting ingest's own
        record out of stdout by status column (see `_records`)."""
        result = self._run(["ingest", str(source)])
        self.assertEqual(result.returncode, 0, result.stderr)
        records = _records(result.stdout)
        self.assertEqual(len(records), 1)
        return records[0][0]

    def test_where_exits_0(self) -> None:
        result = self._run(["where"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.kb))

    def test_lint_exits_0_on_a_clean_wiki(self) -> None:
        result = self._run(["lint"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ingest_with_no_arguments_exits_2_and_prints_usage(self) -> None:
        result = self._run(["ingest"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage:", result.stderr)

    def test_ingest_a_file_exits_0(self) -> None:
        source = self.tmp / "widget.txt"
        source.write_text("Widget source text.")
        self._ingest(source)  # asserts exit 0 internally

    def test_summarize_exits_0_over_an_already_stored_source(self) -> None:
        source = self.tmp / "widget.txt"
        source.write_text("Widget source text.")
        digest = self._ingest(source)

        result = self._run(["summarize", digest])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dedup_exits_0_over_an_existing_summary(self) -> None:
        source = self.tmp / "widget.txt"
        source.write_text("Widget source text.")
        self._ingest(source)  # already runs dedup once, but idempotent

        result = self._run(["dedup"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_init_exits_0_on_a_fresh_root(self) -> None:
        fresh = self.tmp / "fresh" / ".kb"
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(fresh), "init"]
        result = subprocess.run(
            cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_verb_exits_2(self) -> None:
        result = self._run(["bogus"])
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(result.stderr, "")

    def test_ingest_usage_mentions_url(self) -> None:
        result = self._run(["ingest"])
        self.assertIn("url", result.stderr)

    def test_ingest_job_flag_mixed_with_a_url_is_rejected(self) -> None:
        result = self._run(["ingest", "--job", "myjob", "https://example.test"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_ingest_job_flag_with_no_name_is_rejected(self) -> None:
        result = self._run(["ingest", "--job"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_ingest_unknown_job_name_exits_2(self) -> None:
        result = self._run(["ingest", "--job", "no-such-job"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("[jobs.no-such-job]", result.stderr)

    def test_init_generates_config_with_an_active_embed_model(self) -> None:
        """Decision .1 (P6) required the embed model uncommented from
        day one so vectors exist without a manual edit; superseded by
        CLAUDE.md's ban on vendor names in example configs, so the
        line is now a vendor-free placeholder, still uncommented so
        `embed` never fails with a missing-config error on a fresh
        kb."""
        fresh = self.tmp / "fresh-embed" / ".kb"
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(fresh), "init"]
        subprocess.run(cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env)

        config = (fresh / "config.toml").read_text()
        self.assertRegex(config, r'(?m)^embed = "\S+/\S+"$')

        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(fresh), "embed"]
        result = subprocess.run(cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("missing [models].embed", result.stderr)


class EmbedAwareCliTest(unittest.TestCase):
    """Verbs exercised with `[models] embed` configured: `search`'s
    `-n`/`--kind` parsing (P3, P4) and `ingest`'s embed disclosure
    (P2)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.kb = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.kb / sub).mkdir(parents=True)
        (self.kb / "log.md").write_text("# log\n")

        def respond(path: str, body: dict) -> dict:
            if path == "/embeddings":
                data = [
                    {"index": i, "embedding": [1.0, 0.0]}
                    for i in range(len(body["input"]))
                ]
                return {"data": data}
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        self.fake = FakeEndpoint(respond)
        self.addCleanup(self.fake.close)
        (self.kb / "config.toml").write_text(
            '[models]\nsummarize = "cheap"\nembed = "embed-model"\n\n'
            f'[endpoint]\nurl = "{self.fake.url}"\n'
        )
        self.env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), API_KEY_VAR: "fake-key"}

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.kb), *args]
        return subprocess.run(
            cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env
        )

    # -- P3: trailing flag with no value -----------------------------

    def test_trailing_n_flag_with_no_value_is_usage_exit_2(self) -> None:
        result = self._run(["search", "cats", "-n"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_trailing_kind_flag_with_no_value_is_usage_exit_2(self) -> None:
        result = self._run(["search", "cats", "--kind"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    # -- P4: -n below 1, rejected before the paid query embed --------

    def test_n_zero_is_usage_exit_2_before_any_model_call(self) -> None:
        result = self._run(["search", "-n", "0", "cats"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_n_negative_is_usage_exit_2_before_any_model_call(self) -> None:
        result = self._run(["search", "-n", "-3", "cats"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_n_not_a_number_is_usage_exit_2_before_any_model_call(self) -> None:
        result = self._run(["search", "-n", "notanumber", "cats"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.fake.requests, [])

    # -- P2: ingest discloses its embed spend -------------------------

    def test_ingest_prints_embed_planned_line(self) -> None:
        source = self.tmp / "widget.txt"
        source.write_text("Widget source text.")
        result = self._run(["ingest", str(source)])
        self.assertEqual(result.returncode, 0, result.stderr)
        planned = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("embed:") and line.endswith("planned")
        ]
        self.assertTrue(planned, result.stdout)


class MainErrorBoundaryTest(unittest.TestCase):
    """agent-kb-3o4: no traceback ever escapes `main`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_an_escaping_exception_becomes_one_line_and_exit_1(self) -> None:
        def boom(root: Path, args: list[str]) -> int:
            raise ValueError("kaboom")

        stderr = io.StringIO()
        with unittest.mock.patch.dict(cli.VERBS, {"where": (boom, cli.VERBS["where"][1])}):
            with contextlib.redirect_stderr(stderr):
                rc = cli.main(["--kb", str(self.tmp), "where"])

        self.assertEqual(rc, 1)
        self.assertEqual(stderr.getvalue(), "llmwiki: where: ValueError: kaboom\n")

    def test_keyboard_interrupt_propagates_out_of_main(self) -> None:
        def interrupt(root: Path, args: list[str]) -> int:
            raise KeyboardInterrupt

        with unittest.mock.patch.dict(cli.VERBS, {"where": (interrupt, cli.VERBS["where"][1])}):
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["--kb", str(self.tmp), "where"])

    def test_a_verb_returning_2_still_returns_2(self) -> None:
        rc = cli.main(["--kb", str(self.tmp), "ingest"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()


TOKEN_VAR = "HOMELAB_KB_TOKEN"
TOKEN = "token-value-nothing-may-print"


def _wiki_body(*titles: str, high: bool = True) -> bytes:
    """A search response. `high` and low scores exist only to prove the
    client never sorts across two wikis by them."""
    base = 0.9 if high else 0.2
    hits = [
        {
            "score": base - index / 100,
            "name": f"{title.lower()}.md",
            "title": title,
            "updated": "2026-01-01T00:00:00Z",
            "size": 100 + index,
        }
        for index, title in enumerate(titles)
    ]
    return json.dumps({"hits": hits}).encode("utf-8")


class RemoteCliFixture:
    """A kb, a fake model endpoint, and helpers for driving `cli.main`
    against real sockets. `parse_remotes` is patched so a plain-http
    fake is reachable while the https rule stays enforced where a
    user's config actually touches it, per the phase's own verification
    note."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.kb = self.tmp / ".kb"
        (self.kb / "wiki").mkdir(parents=True)
        (self.kb / "sources").mkdir()
        (self.kb / "log.md").write_text("# log\n")
        for name, title in (("north.md", "North"), ("south.md", "South")):
            (self.kb / "wiki" / name).write_text(
                f"---\ntitle: {title}\nkind: summary\n---\n\nA page.\n"
            )

        def respond(path: str, body: dict) -> dict:
            if path == "/embeddings":
                return {
                    "data": [
                        {"index": i, "embedding": self._vector(text)}
                        for i, text in enumerate(body["input"])
                    ]
                }
            raise AssertionError(f"unexpected path {path!r}")

        self.endpoint = FakeEndpoint(respond)
        self.addCleanup(self.endpoint.close)
        (self.kb / "config.toml").write_text(
            '[models]\nembed = "cheap-embed"\n\n'
            f'[endpoint]\nurl = "{self.endpoint.url}"\n'
        )
        self.env = unittest.mock.patch.dict(
            os.environ, {API_KEY_VAR: "model-key-value"}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:8]]

    def _run(self, argv: list[str]) -> tuple[int, bytes, str]:
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--kb", str(self.kb), *argv])
        out.flush()
        return code, raw.getvalue(), err.getvalue()

    def _embed(self) -> None:
        code, _out, err = self._run(["embed"])
        self.assertEqual(code, 0, err)

    def _wiki(self, respond) -> FakeWiki:
        wiki = FakeWiki(respond)
        self.addCleanup(wiki.close)
        return wiki

    def _serving(self, body: bytes) -> FakeWiki:
        return self._wiki(lambda _request: Reply(body=body))

    @contextlib.contextmanager
    def _table(self, table: dict[str, remotes.Remote]):
        with unittest.mock.patch.object(
            remotes, "parse_remotes", return_value=table
        ):
            yield

    @staticmethod
    def _remote(name: str, wiki: FakeWiki, token_env=None) -> remotes.Remote:
        return remotes.Remote(name, wiki.url + f"/kb/{name}", token_env)


class RemoteSearchCliTest(RemoteCliFixture, unittest.TestCase):
    """`search --remote` and `--all` through the real verb."""

    def test_two_wikis_stay_in_two_blocks_in_the_order_named(self) -> None:
        low = self._serving(_wiki_body("Alpha", "Beta", high=False))
        high = self._serving(_wiki_body("Gamma", high=True))
        table = {
            "lowscore": self._remote("lowscore", low),
            "highscore": self._remote("highscore", high),
        }
        self._embed()
        with self._table(table):
            code, out, err = self._run(
                ["search", "cold", "--remote", "lowscore",
                 "--remote", "highscore"]
            )
        self.assertEqual((code, err), (0, ""))
        lines = out.decode().splitlines()
        self.assertEqual(
            [line for line in lines if line.startswith("#")],
            ["# local", "# lowscore", "# highscore"],
        )
        self.assertEqual(lines[lines.index("# lowscore") + 1].split("\t")[:3],
                         ["1", "alpha.md", "Alpha"])
        self.assertEqual(lines[lines.index("# lowscore") + 2].split("\t")[:3],
                         ["2", "beta.md", "Beta"])
        self.assertEqual(lines[lines.index("# highscore") + 1].split("\t")[:3],
                         ["1", "gamma.md", "Gamma"])
        for line in lines:
            self.assertNotIn("0.9", line)
            self.assertNotIn("0.2", line)

    def test_n_is_per_wiki_not_a_total(self) -> None:
        many = [f"Page{i}" for i in range(9)]

        def respond(request):
            """Honours `n` the way phase 3's route does, so the block
            size proves the client sent 5 to each wiki, not 5 split
            between them."""
            limit = int(request.query["n"][0])
            return Reply(body=_wiki_body(*many[:limit]))

        first = self._wiki(respond)
        second = self._wiki(respond)
        table = {
            "first": self._remote("first", first),
            "second": self._remote("second", second),
        }
        self._embed()
        with self._table(table):
            code, out, _err = self._run(["search", "cold", "-n", "5", "--all"])
        self.assertEqual(code, 0)
        blocks = out.decode().split("# ")[1:]
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            hits = [line for line in block.splitlines()[1:] if line]
            self.assertLessEqual(len(hits), 5)
        self.assertEqual(first.requests[0].query["n"], ["5"])

    def test_a_pointer_only_kb_prints_no_local_block(self) -> None:
        pointer = self.tmp / "pointer.kb"
        pointer.mkdir()
        (pointer / "config.toml").write_text("")
        self.kb = pointer
        wiki = self._serving(_wiki_body("Alpha"))
        with self._table({"a": self._remote("a", wiki)}):
            code, out, err = self._run(["search", "cold", "--all"])
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("# local", out.decode())
        self.assertIn("# a", out.decode())
        self.assertEqual(self.endpoint.requests, [], "zero paid calls")

    def test_all_over_three_remotes_costs_one_model_call(self) -> None:
        wikis = {
            name: self._remote(name, self._serving(_wiki_body("Alpha")))
            for name in ("a", "b", "c")
        }
        self._embed()
        embeds = len(self.endpoint.requests)
        with self._table(wikis):
            code, _out, err = self._run(["search", "cold", "--all"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.endpoint.requests) - embeds, 1)

    def test_one_dead_remote_leaves_the_others_printing(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        alive = self._serving(_wiki_body("Alpha"))
        table = {
            "dead": remotes.Remote(
                "dead", f"http://127.0.0.1:{dead_port}/kb/dead", None
            ),
            "alive": self._remote("alive", alive),
        }
        self._embed()
        with self._table(table):
            code, out, err = self._run(
                ["search", "cold", "--remote", "dead", "--remote", "alive"]
            )
        self.assertEqual(code, 1)
        self.assertEqual(err, "dead\tunreachable\n")
        self.assertIn("# alive", out.decode())
        self.assertNotIn("# dead", out.decode())

    def test_two_slow_remotes_cost_one_timeout_not_two(self) -> None:
        held = threading.Event()
        self.addCleanup(held.set)

        def respond(_request):
            held.wait(10)
            return Reply(body=_wiki_body("Alpha"))

        table = {
            name: self._remote(name, self._wiki(respond))
            for name in ("slow1", "slow2")
        }
        with self._table(table), unittest.mock.patch.object(
            remotes, "REMOTE_TIMEOUT_SEC", 0.5
        ):
            started = time.monotonic()
            code, _out, err = self._run(
                ["search", "cold", "--remote", "slow1", "--remote", "slow2"]
            )
            elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertIn("slow1\ttimeout", err)
        self.assertIn("slow2\ttimeout", err)
        self.assertLess(elapsed, 1.0, "a sequential fan-out fails this")

    def test_a_local_failure_is_one_more_failed_participant(self) -> None:
        wiki = self._serving(_wiki_body("Alpha"))
        with self._table({"a": self._remote("a", wiki)}):
            code, out, err = self._run(["search", "cold", "--all"])
        self.assertEqual(code, 1)
        self.assertIn("local\tindex_stale", err)
        self.assertIn("# a", out.decode())

    def test_an_unknown_remote_name_exits_2_with_no_request(self) -> None:
        wiki = self._serving(_wiki_body("Alpha"))
        with self._table({"a": self._remote("a", wiki)}):
            code, out, err = self._run(["search", "cold", "--remote", "b"])
        self.assertEqual(code, 2)
        self.assertIn("unknown remote: b", err)
        self.assertEqual(out, b"")
        self.assertEqual(wiki.requests, [])

    def test_a_refused_remotes_table_exits_2_with_no_request(self) -> None:
        wiki = self._serving(_wiki_body("Alpha"))
        (self.kb / "config.toml").write_text(
            (self.kb / "config.toml").read_text()
            + '\n[remotes.a]\nurl = "http://insecure.example/kb/a"\n'
        )
        code, out, err = self._run(["search", "cold", "--all"])
        self.assertEqual(code, 2)
        self.assertIn("remote a", err)
        self.assertEqual(out, b"")
        self.assertEqual(wiki.requests, [])

    def test_plain_search_makes_no_request_to_a_declared_remote(self) -> None:
        wiki = self._serving(_wiki_body("Alpha"))
        self._embed()
        with self._table({"a": self._remote("a", wiki)}):
            code, out, _err = self._run(["search", "cold"])
        self.assertEqual(code, 0)
        self.assertEqual(wiki.requests, [])
        self.assertNotIn(b"# local", out)
        self.assertIn(b"north.md", out)

    def test_no_stream_ever_carries_the_token(self) -> None:
        good = self._serving(_wiki_body("Alpha"))
        refusing = self._wiki(
            lambda _r: Reply(status=401, body=b'{"error": "unauthorized"}')
        )
        table = {
            "good": self._remote("good", good, TOKEN_VAR),
            "refusing": self._remote("refusing", refusing, TOKEN_VAR),
            "notoken": remotes.Remote("notoken", good.url + "/kb/x", "ABSENT"),
        }
        self._embed()
        with self._table(table), unittest.mock.patch.dict(
            os.environ, {TOKEN_VAR: TOKEN}
        ):
            code, out, err = self._run(["search", "cold", "--all"])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.decode())
        self.assertNotIn(TOKEN, err)
        self.assertNotIn("model-key-value", out.decode())
        self.assertNotIn("model-key-value", err)
        self.assertIn("refusing\tunauthorized", err)
        self.assertIn("notoken\tno_token", err)


class PageVerbTest(RemoteCliFixture, unittest.TestCase):
    """`page`, remote form and local form."""

    def test_a_remote_page_is_written_as_bytes(self) -> None:
        raw = b"# North\n\xff\xfe not utf-8\n"
        wiki = self._wiki(
            lambda _r: Reply(body=raw, content_type="text/markdown")
        )
        with self._table({"a": self._remote("a", wiki)}):
            code, out, err = self._run(["page", "--remote", "a", "north.md"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, raw)

    def test_a_remote_page_typed_html_writes_nothing(self) -> None:
        wiki = self._wiki(
            lambda _r: Reply(body=b"<html>", content_type="text/html")
        )
        with self._table({"a": self._remote("a", wiki)}):
            code, out, err = self._run(["page", "--remote", "a", "north.md"])
        self.assertEqual(code, 1)
        self.assertEqual(out, b"")
        self.assertEqual(err, "a\tbad_response\n")

    def test_an_unknown_remote_name_exits_2(self) -> None:
        wiki = self._serving(_wiki_body("Alpha"))
        with self._table({"a": self._remote("a", wiki)}):
            code, _out, err = self._run(["page", "--remote", "b", "north.md"])
        self.assertEqual(code, 2)
        self.assertIn("unknown remote: b", err)

    def test_a_local_page_comes_back_whole(self) -> None:
        code, out, err = self._run(["page", "north.md"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (self.kb / "wiki" / "north.md").read_bytes())

    def test_a_local_page_on_a_symlinked_root_comes_back(self) -> None:
        link = self.tmp / "linked.kb"
        link.symlink_to(self.kb)
        self.kb = link
        code, out, err = self._run(["page", "north.md"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(b"North", out)

    def test_local_page_refuses_a_walk_a_separator_and_a_subdirectory(self):
        (self.kb / "wiki" / "sub").mkdir()
        (self.kb / "wiki" / "sub" / "deep.md").write_text("deep\n")
        for name in ("../config.toml", "sub/deep.md", "north.txt",
                     "no\x00rth.md"):
            with self.subTest(name=name):
                code, out, err = self._run(["page", name])
                self.assertEqual(code, 1)
                self.assertEqual(out, b"")
                self.assertEqual(err, "local\tnot_found\n")

    def test_page_with_no_argument_exits_2(self) -> None:
        code, _out, err = self._run(["page"])
        self.assertEqual(code, 2)
        self.assertIn("usage", err)
