"""Summarize: one summary page per source, against the fake endpoint."""

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import Kb, parse_frontmatter  # noqa: E402
from llmwiki.model import API_KEY_FILE_VAR, API_KEY_VAR  # noqa: E402
from llmwiki import summarize  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent

ISBN_TABLE = '[identifiers.isbn]\npattern = "^[0-9]{13}$"\n'


def _run_quiet(root, digests):
    """summarize.run, with its `N planned` print swallowed so it does
    not bury the unittest summary."""
    with redirect_stdout(io.StringIO()):
        return summarize.run(root, digests)


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


class SummarizeTest(unittest.TestCase):
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

    def _store_source(self, text: str = "Some widget source text.") -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(text)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/widget"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            'content_type = "text/markdown"\n'
            'job = "manual"\n'
        )
        return digest

    def test_writes_summary_page_with_all_frontmatter_keys(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Widget Overview\n"
            "identifiers:\n"
            "  - isbn:1234567890123\n"
            "---\n\n"
            "A short abstract of the widget.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, ISBN_TABLE)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["kind"], "summary")
        self.assertEqual(fields["title"], "Widget Overview")
        self.assertEqual(fields["source"], digest)
        self.assertEqual(fields["source_url"], "https://example.com/widget")
        self.assertEqual(fields["fetched"], "2024-01-01T00:00:00Z")
        self.assertEqual(fields["model"], "cheap")
        self.assertIn("prompt_fingerprint", fields)
        self.assertEqual(fields["identifiers"], ["isbn:1234567890123"])
        self.assertEqual(body.strip(), "A short abstract of the widget.")

    def test_fingerprint_stable_across_two_runs_of_the_same_prefix(self) -> None:
        self._write_config("http://unused", ISBN_TABLE)
        kb = Kb(self.root)
        first = summarize.prompt_fingerprint(summarize.prompt_prefix(kb))
        second = summarize.prompt_fingerprint(summarize.prompt_prefix(kb))
        self.assertEqual(first, second)

    def test_fingerprint_changes_with_summarize_md(self) -> None:
        self._write_config("http://unused", ISBN_TABLE)
        before = summarize.prompt_fingerprint(summarize.prompt_prefix(Kb(self.root)))
        (self.root / "SUMMARIZE.md").write_text("Extra house rules.\n")
        after = summarize.prompt_fingerprint(summarize.prompt_prefix(Kb(self.root)))
        self.assertNotEqual(before, after)

    def test_fingerprint_changes_with_declared_identifiers(self) -> None:
        self._write_config("http://unused", ISBN_TABLE)
        before = summarize.prompt_fingerprint(summarize.prompt_prefix(Kb(self.root)))
        self._write_config(
            "http://unused",
            ISBN_TABLE + '\n[identifiers.asin]\npattern = "^[A-Z0-9]{10}$"\n',
        )
        after = summarize.prompt_fingerprint(summarize.prompt_prefix(Kb(self.root)))
        self.assertNotEqual(before, after)

    def test_code_fenced_reply_is_unwrapped_and_parsed(self) -> None:
        digest = self._store_source()
        inner = (
            "---\n"
            "title: Fenced Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Fenced abstract.\n"
        )
        reply = "```markdown\n" + inner + "```"

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["title"], "Fenced Widget")
        self.assertEqual(body.strip(), "Fenced abstract.")

    def test_undeclared_identifier_drops_page_leaves_source_and_logs(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Undeclared Widget\n"
            "identifiers:\n"
            "  - asin:B000123\n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, ISBN_TABLE)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        self.assertTrue((self.root / "sources" / f"{digest}.md").exists())
        self.assertTrue((self.root / "sources" / f"{digest}.toml").exists())
        log = (self.root / "log.md").read_text()
        self.assertIn(digest, log)
        self.assertIn("identifier-key", log)

    def test_rerun_over_up_to_date_page_makes_zero_model_calls(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Stable Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Stable abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            first = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 1)
            second = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 1)

        self.assertEqual((first, second), (0, 0))

    def test_rerun_whose_reply_fails_lint_keeps_previous_page(self) -> None:
        digest = self._store_source()
        good_reply = (
            "---\n"
            "title: Good Widget\n"
            "identifiers:\n"
            "  - isbn:1234567890123\n"
            "---\n\n"
            "Good abstract.\n"
        )
        bad_reply = (
            "---\n"
            "title: Bad Widget\n"
            "identifiers:\n"
            "  - asin:B000123\n"
            "---\n\n"
            "Bad abstract.\n"
        )
        replies = iter([good_reply, bad_reply])

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": next(replies)}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, ISBN_TABLE)
            _run_quiet(self.root, [digest])
            pages = list((self.root / "wiki").glob("*.md"))
            self.assertEqual(len(pages), 1)
            page_path = pages[0]
            before = page_path.read_text()

            # Force a second call by changing the prompt prefix.
            (self.root / "SUMMARIZE.md").write_text("Extra note.\n")
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertEqual(page_path.read_text(), before)

    def test_model_error_aborts_run_and_writes_nothing(self) -> None:
        digest = self._store_source()

        def respond(_path: str, _body: dict) -> dict:
            return {"error": "boom"}  # missing "choices" -> ModelError

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertIn("choices", err.getvalue())
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])

    def test_undecodable_source_is_logged_and_skipped_not_fatal(self) -> None:
        data = b"\xff\xfe not utf-8"
        digest = hashlib.sha256(data).hexdigest()
        (self.root / "sources" / f"{digest}.bin").write_bytes(data)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/bad"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            'content_type = "application/octet-stream"\n'
            'job = "manual"\n'
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(code, 1)
        self.assertIn(digest, (self.root / "log.md").read_text())

    def test_title_collision_leaves_two_pages_each_with_own_source(self) -> None:
        digest_a = self._store_source("First source body.")
        digest_b = self._store_source("Second source body.")
        reply = (
            "---\n"
            "title: Same Title\n"
            "identifiers: []\n"
            "---\n\n"
            "An abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, None)

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 2)
        sources_seen = set()
        for page in pages:
            fields, _body = parse_frontmatter(page.read_text())
            sources_seen.add(fields["source"])
        self.assertEqual(sources_seen, {digest_a, digest_b})

    def test_rerun_after_title_collision_makes_zero_model_calls(self) -> None:
        self._store_source("First source body.")
        self._store_source("Second source body.")
        reply = (
            "---\n"
            "title: Same Title\n"
            "identifiers: []\n"
            "---\n\n"
            "An abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            _run_quiet(self.root, None)
            self.assertEqual(len(fake.requests), 2)
            second = _run_quiet(self.root, None)
            self.assertEqual(len(fake.requests), 2)

        self.assertEqual(second, 0)
        self.assertEqual(len(list((self.root / "wiki").glob("*.md"))), 2)

    def test_agent_owned_page_at_slug_is_untouched_and_summary_suffixed(self) -> None:
        digest = self._store_source()
        owned_path = self.root / "wiki" / "widget-overview.md"
        owned_content = (
            "---\n"
            "kind: story\n"
            "title: Widget Overview\n"
            "---\n\n"
            "The agent's own page, not a CLI summary.\n"
        )
        owned_path.write_text(owned_content)
        reply = (
            "---\n"
            "title: Widget Overview\n"
            "identifiers: []\n"
            "---\n\n"
            "A short abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        self.assertEqual(owned_path.read_text(), owned_content)
        suffixed = self.root / "wiki" / f"widget-overview-{digest[:12]}.md"
        self.assertTrue(suffixed.is_file())
        fields, _body = parse_frontmatter(suffixed.read_text())
        self.assertEqual(fields["source"], digest)

    def test_unknown_digest_returns_1_no_page_no_call_logs_hash(self) -> None:
        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, ["deadbeef"])
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        self.assertIn("deadbeef", (self.root / "log.md").read_text())

    def test_missing_sidecar_returns_1_no_page_no_call_logs_digest(self) -> None:
        digest = self._store_source()
        (self.root / "sources" / f"{digest}.toml").unlink()

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        self.assertIn(digest, (self.root / "log.md").read_text())

    def test_identifiers_not_a_list_drops_page_and_logs_digest(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Bad Shape Widget\n"
            "identifiers: not-a-list\n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        self.assertIn(digest, (self.root / "log.md").read_text())

    def test_blank_identifier_item_drops_page(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Blank Identifier Widget\n"
            "identifiers:\n"
            "  - isbn:1234567890123\n"
            "  - \n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, ISBN_TABLE)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        self.assertIn(digest, (self.root / "log.md").read_text())

    def test_empty_identifiers_list_is_kept(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: No Identifiers Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        self.assertNotIn("dropped", (self.root / "log.md").read_text())

    def test_correctly_indented_identifiers_list_is_kept_as_list(self) -> None:
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Two Item Widget\n"
            "identifiers:\n"
            "  - ingredient:flour\n"
            "  - isbn:1234567890123\n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, ISBN_TABLE + "\n[identifiers.ingredient]\n")
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, _body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(
            fields["identifiers"], ["ingredient:flour", "isbn:1234567890123"]
        )

    def test_reply_with_leading_newline_is_parsed_and_written(self) -> None:
        digest = self._store_source()
        reply = (
            "\n---\n"
            "title: Leading Newline Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, _body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["title"], "Leading Newline Widget")


class BuiltInPromptTest(unittest.TestCase):
    def test_prompt_shows_an_exact_indented_frontmatter_example(self) -> None:
        lines = summarize.BUILT_IN_PROMPT.split("\n")
        self.assertGreaterEqual(sum(1 for line in lines if line == "---"), 2)
        self.assertTrue(
            any(line.startswith("  - ") for line in lines),
            "prompt must show a two-space-indented block list item",
        )


class SummarizeCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")

    def test_summarize_verb_writes_a_page_end_to_end(self) -> None:
        text = "Some CLI widget source text."
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_text(text)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/cli-widget"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            'content_type = "text/markdown"\n'
            'job = "manual"\n'
        )
        reply = (
            "---\n"
            "title: CLI Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "CLI abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            (self.root / "config.toml").write_text(
                '[models]\nsummarize = "cheap"\n\n'
                f'[endpoint]\nurl = "{fake.url}"\n'
            )
            cmd = [
                sys.executable, "-m", "llmwiki",
                "--kb", str(self.root), "summarize", digest,
            ]
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), API_KEY_VAR: "cli-key"}
            run = subprocess.run(
                cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env
            )

        self.assertEqual(run.returncode, 0, run.stderr)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        self.assertIn("CLI Widget", pages[0].read_text())


if __name__ == "__main__":
    unittest.main()
