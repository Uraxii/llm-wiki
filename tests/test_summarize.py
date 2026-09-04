"""Summarize: one summary page per source, against the fake endpoint."""

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.core import Kb, parse_frontmatter  # noqa: E402
from llmwiki import summarize  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from kb_config import config_toml  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent

ISBN_TABLE = '[identifiers.isbn]\npattern = "^[0-9]{13}$"\n'


def _run_quiet(root, digests):
    """summarize.run, with its `N planned` print swallowed so it does
    not bury the unittest summary."""
    with redirect_stdout(io.StringIO()):
        return summarize.run(root, digests)


class SummarizeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")

    def _write_config(self, url: str, identifiers: str = "") -> None:
        (self.root / "config.toml").write_text(
            config_toml(url, {"summarize": "cheap"}, extra=identifiers)
        )

    def _store_source(
        self,
        text: str = "Some widget source text.",
        content_type: str = "text/markdown",
        suffix: str = ".md",
    ) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (self.root / "sources" / f"{digest}{suffix}").write_text(text)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/widget"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            f'content_type = "{content_type}"\n'
            'job = "manual"\n'
        )
        return digest

    def _store_binary_source(
        self, data: bytes, suffix: str, content_type: str
    ) -> str:
        digest = hashlib.sha256(data).hexdigest()
        (self.root / "sources" / f"{digest}{suffix}").write_bytes(data)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/attachment"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            f'content_type = "{content_type}"\n'
            'job = "manual"\n'
        )
        return digest

    @staticmethod
    def _ordered_texts(first_label: str, second_label: str) -> tuple[str, str]:
        """Two texts whose sha256 digests sort in the given order, so a
        test asserting sweep order does not depend on which way an
        arbitrary pair of strings happened to hash (agent-kb-74p item
        E)."""
        i = 0
        while True:
            first = f"{first_label} {i}"
            second = f"{second_label} {i}"
            if (
                hashlib.sha256(first.encode()).hexdigest()
                < hashlib.sha256(second.encode()).hexdigest()
            ):
                return first, second
            i += 1

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
        self.assertEqual(fields["model"], "test:cheap")
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

    def test_reply_fencing_only_the_frontmatter_is_parsed(self) -> None:
        """A real reply from the configured provider, captured verbatim.
        The model fenced the frontmatter and left the body outside the
        fence, so the block carries no `---` lines of its own and the
        fence markers stand where they belong."""
        digest = self._store_source()
        reply = (
            "```yaml\n"
            "kind: summary\n"
            "title: Archify Reference\n"
            "identifiers: []\n"
            "```\n"
            "\n"
            "This document details Archify, a tool for generating "
            "interactive architectural diagrams from JSON specifications.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["title"], "Archify Reference")
        self.assertTrue(body.startswith("This document details Archify"))

    def test_fenced_frontmatter_over_a_body_that_fences_a_block(self) -> None:
        """The reply's last line is a fence closing a block in the body,
        not the frontmatter's. Which fence closes the opening one is
        decided by the line after it, not by the reply's last line."""
        digest = self._store_source()
        reply = (
            "```yaml\n"
            "title: Fenced Body Widget\n"
            "identifiers: []\n"
            "```\n"
            "\n"
            "Abstract text.\n"
            "\n"
            "```py\n"
            "widget = 1\n"
            "```\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["title"], "Fenced Body Widget")
        self.assertIn("widget = 1", body)

    def test_fence_around_a_frontmatter_block_that_kept_its_dashes(self) -> None:
        """The fence closes on the frontmatter's own closing `---`, and
        the body sits outside it and ends with a fenced block of its
        own. Everything after that first closing fence is body."""
        digest = self._store_source()
        reply = (
            "```markdown\n"
            "---\n"
            "title: Dashed Widget\n"
            "identifiers: []\n"
            "---\n"
            "```\n"
            "\n"
            "Abstract text.\n"
            "\n"
            "```py\n"
            "widget = 1\n"
            "```\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["title"], "Dashed Widget")
        self.assertTrue(body.strip().startswith("Abstract text."))
        self.assertIn("widget = 1", body)

    def test_a_fenced_page_with_no_dashes_drops_rather_than_guessing(self) -> None:
        """The whole reply is fenced and carries no `---` line. Reading
        the fence markers as the frontmatter's own would swallow the
        first body line that holds a colon and write a page with an
        empty body, so this drops instead."""
        digest = self._store_source()
        reply = (
            "```\n"
            "title: Guessed Widget\n"
            "identifiers: []\n"
            "\n"
            "Overview: this widget does things.\n"
            "```"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertIn("unparseable reply", err.getvalue())
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])

    def test_source_of_exactly_the_text_cap_is_summarized(self) -> None:
        digest = self._store_source(
            "w" * summarize.MAX_SOURCE_TEXT_BYTES, "text/plain", ".txt"
        )
        reply = (
            "---\n"
            "title: Capped Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Abstract text.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])

        self.assertEqual(err.getvalue(), "")
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.root / "wiki").glob("*.md"))), 1)

    def test_unparseable_drop_names_what_arrived_instead(self) -> None:
        digest = self._store_source()

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "```yaml\nkind: x\n"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        for message in (err.getvalue(), (self.root / "log.md").read_text()):
            self.assertIn("unparseable reply", message)
            self.assertIn("```yaml", message)
            self.assertIn("16 chars", message)

    def test_a_long_reply_is_excerpted_not_copied_into_the_log(self) -> None:
        digest = self._store_source()
        reply = "x" * 500

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                _run_quiet(self.root, [digest])

        message = err.getvalue()
        self.assertIn("500 chars", message)
        self.assertIn("x" * summarize.REPLY_EXCERPT_CHARS, message)
        self.assertNotIn("x" * (summarize.REPLY_EXCERPT_CHARS + 1), message)

    def test_text_types_off_the_network_allowlist_are_summarized(self) -> None:
        """`.xml`, `.json` and `.py` are what the operator ingests from
        disk; `mimetypes.guess_type` gives them content types that
        `fetch.ACCEPTED_TYPES` never listed, because that allowlist
        guards downloads, not stored bytes."""
        stored = [
            self._store_source("<class name='Widget'/>", "text/xml", ".xml"),
            self._store_source('{"widget": true}', "application/json", ".json"),
            self._store_source("def widget():\n    pass\n", "text/x-python", ".py"),
        ]
        reply = (
            "---\n"
            "title: Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Abstract text.\n"
        )
        sent = []

        def respond(_path: str, body: dict) -> dict:
            sent.append(body)
            return {"choices": [{"message": {"content": reply}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, sorted(stored))

        self.assertEqual(err.getvalue(), "")
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(list((self.root / "wiki").glob("*.md"))), 3)

    def test_source_over_the_text_cap_drops_before_the_model_call(self) -> None:
        size = summarize.MAX_SOURCE_TEXT_BYTES + 1
        digest = self._store_source("w" * size, "text/plain", ".txt")

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        log = (self.root / "log.md").read_text()
        self.assertIn(digest, log)
        self.assertIn(str(size), log)
        self.assertIn(str(summarize.MAX_SOURCE_TEXT_BYTES), log)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])

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

    def test_rollback_restores_non_utf8_previous_page_byte_for_byte(self) -> None:
        digest = self._store_source()
        original = (
            b"---\n"
            b"kind: summary\n"
            b"title: Old Widget\n"
            b"source: " + digest.encode() + b"\n"
            b"identifiers:\n"
            b"  - isbn:1234567890123\n"
            b"---\n\n"
            b"body with \xff\xfe bad bytes\n"
        )
        page_path = self.root / "wiki" / "old-widget.md"
        page_path.write_bytes(original)

        bad_reply = (
            "---\n"
            "title: Bad Widget\n"
            "identifiers:\n"
            "  - asin:B000123\n"
            "---\n\n"
            "Bad abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": bad_reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url, ISBN_TABLE)
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertEqual(page_path.read_bytes(), original)

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

    def test_unreadable_source_does_not_abort_sweep_and_logs_without_traceback(
        self,
    ) -> None:
        # agent-kb-74p item E: `blocked_text`/`second_text` are picked so
        # `blocked`'s digest sorts before `second`'s (asserted below), so
        # the "later digest still processed" half of this test is not
        # riding on which way two arbitrary strings happen to hash.
        # Also the axis this test pins: a chmod-000 source is now
        # actionable (an operator can chmod it back), so a bare sweep
        # over it returns 1, not 0 -- only the earlier ABORT was the bug.
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        blocked_text, second_text = self._ordered_texts(
            "Blocked source text.", "Second source text."
        )
        blocked = self._store_source(blocked_text)
        second = self._store_source(second_text)
        self.assertLess(blocked, second)
        blocked_path = self.root / "sources" / f"{blocked}.md"
        blocked_path.chmod(0o000)
        self.addCleanup(blocked_path.chmod, 0o644)
        reply = (
            "---\n"
            "title: Second Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "An abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, None)

        self.assertEqual(code, 1)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        fields, _body = parse_frontmatter(pages[0].read_text())
        self.assertEqual(fields["source"], second)
        log = (self.root / "log.md").read_text()
        self.assertIn(blocked, log)
        self.assertIn("dropped", log)
        stderr = err.getvalue()
        self.assertIn(blocked, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("PermissionError", stderr)

    def test_bare_sweep_over_undecodable_source_exits_0_and_reports_stderr_each_run(
        self,
    ) -> None:
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
            err1 = io.StringIO()
            with redirect_stderr(err1):
                first = _run_quiet(self.root, None)
            err2 = io.StringIO()
            with redirect_stderr(err2):
                second = _run_quiet(self.root, None)
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertIn(digest, err1.getvalue())
        self.assertIn(digest, err2.getvalue())

    def test_bare_sweep_over_undecodable_text_source_exits_0_twice(self) -> None:
        # The ticket's own case (agent-kb-74p): a text content_type
        # whose bytes are not valid UTF-8. `_resolve_content` returns
        # "cannot decode source: ...", which is inert -- no rerun under
        # any configuration fixes bytes that are not UTF-8 -- so a bare
        # sweep must not exit 1 forever over it. The second run is the
        # part that matters: "exits 1 forever" was the whole bug, and a
        # fix that only worked once would still be broken.
        data = b"Some markdown \xff\xfe not utf-8"
        digest = hashlib.sha256(data).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_bytes(data)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/bad-text"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            'content_type = "text/markdown"\n'
            'job = "manual"\n'
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            err1 = io.StringIO()
            with redirect_stderr(err1):
                first = _run_quiet(self.root, None)
            err2 = io.StringIO()
            with redirect_stderr(err2):
                second = _run_quiet(self.root, None)
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertIn("cannot decode source", err1.getvalue())
        self.assertIn("cannot decode source", err2.getvalue())
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])

    def test_explicit_digest_over_undecodable_text_source_returns_1(self) -> None:
        # Same case as above, but ingest.py calls
        # summarize.run(root, [digest]) explicitly, and that path must
        # still fail even though a bare sweep does not. Pinned next to
        # the bare-sweep case since the two are easy to break together.
        data = b"Some markdown \xff\xfe not utf-8"
        digest = hashlib.sha256(data).hexdigest()
        (self.root / "sources" / f"{digest}.md").write_bytes(data)
        (self.root / "sources" / f"{digest}.toml").write_text(
            'url = "https://example.com/bad-text"\n'
            'fetched = "2024-01-01T00:00:00Z"\n'
            'content_type = "text/markdown"\n'
            'job = "manual"\n'
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(code, 1)

    def test_bare_sweep_reply_level_drop_still_returns_1(self) -> None:
        self._store_source()

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "not frontmatter"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, None)

        self.assertEqual(code, 1)
        self.assertIn("unparseable reply", err.getvalue())

    def test_bare_sweep_pdf_part_none_exits_1(self) -> None:
        # A PDF source with pdf_part = "none" is an actionable drop: an
        # operator can edit [endpoint].pdf_part and rerun, so it must
        # not be lumped in with the two inert reasons.
        digest = self._store_binary_source(
            b"%PDF-1.4 fake", ".pdf", "application/pdf"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with (self.root / "config.toml").open("a") as handle:
                handle.write('pdf_part = "none"\n')
            with redirect_stderr(err):
                code = _run_quiet(self.root, None)
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(code, 1)
        self.assertIn(digest, err.getvalue())
        self.assertIn("pdf_part", err.getvalue())

    def test_bare_sweep_missing_sidecar_exits_1(self) -> None:
        digest = self._store_source()
        (self.root / "sources" / f"{digest}.toml").unlink()

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, None)
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        self.assertIn(digest, err.getvalue())

    def test_bare_sweep_malformed_sidecar_exits_1(self) -> None:
        digest = self._store_source()
        (self.root / "sources" / f"{digest}.toml").write_text(
            "content_type = = broken\n[[[\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, None)
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        self.assertIn(digest, err.getvalue())
        self.assertIn("provenance", err.getvalue())

    def test_unreadable_sources_directory_is_actionable_not_a_traceback(self) -> None:
        # agent-kb-74p item D as briefed claims `_source_path`'s glob can
        # raise OSError when sources/ itself is unreadable, mislabeled by
        # the `except (OSError, ValueError)` clause as "cannot read
        # provenance". Verified against the runtime this repo pins
        # (Python 3.14): `pathlib.Path.glob` swallows a scandir OSError
        # and yields no matches instead of raising (glob.py's `_iterdir`,
        # `except OSError: return`, the same fix `glob.glob` shipped in
        # 3.13 for gh-101398). `_source_path` can then only ever raise
        # `FileNotFoundError`, already labeled "cannot read source"
        # correctly. This test proves that verified behavior: no
        # traceback, actionable, no "cannot read provenance" mislabel.
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        self._store_source()
        sources_dir = self.root / "sources"
        sources_dir.chmod(0o000)
        self.addCleanup(sources_dir.chmod, 0o755)

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, ["deadbeef" * 8])

        self.assertEqual(code, 1)
        stderr = err.getvalue()
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("cannot read provenance", stderr)
        self.assertIn("cannot read source", stderr)

    def test_unreadable_visual_source_is_actionable_not_fatal(self) -> None:
        # agent-kb-74p item A: the visual-branch OSError guard in
        # `_resolve_content` (the `path.read_bytes()` call for an image
        # or PDF attachment) had zero test coverage; deleting it by hand
        # left the full suite green. This test fails without the guard:
        # `path.read_bytes()` then raises PermissionError straight out
        # of `run`, instead of a clean actionable drop.
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        digest = self._store_binary_source(b"\x89PNG fake bytes", ".png", "image/png")
        image_path = self.root / "sources" / f"{digest}.png"
        image_path.chmod(0o000)
        self.addCleanup(image_path.chmod, 0o644)

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        err = io.StringIO()
        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 0)

        self.assertEqual(code, 1)
        stderr = err.getvalue()
        self.assertIn(digest, stderr)
        self.assertIn("cannot read source", stderr)
        self.assertNotIn("Traceback", stderr)

    # -- agent-kb-yr0: a malformed [models].summarize_image must fail
    # loudly, never read as "summarize_image not configured".

    def test_malformed_summarize_image_config_fails_loud_not_fallback(self) -> None:
        """A typo'd or malformed [models].summarize_image used to be
        caught by the same `except ModelError` that unset
        summarize_image hits, so an image source silently summarized
        against the plain [models].summarize model instead. It must
        now fail the run rather than fall back."""
        digest = self._store_binary_source(b"\x89PNG fake bytes", ".png", "image/png")

        def respond(_path: str, _body: dict) -> dict:
            raise AssertionError("must not reach the model on a malformed config")

        with FakeEndpoint(respond) as fake:
            (self.root / "config.toml").write_text(
                '[models]\nsummarize = "test:cheap"\nsummarize_image = 123\n\n'
                f'[providers.test]\nurl = "{fake.url}"\n'
            )
            err = io.StringIO()
            with redirect_stderr(err):
                code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 1)
        self.assertEqual(fake.requests, [])
        stderr = err.getvalue()
        self.assertIn("[models].summarize_image", stderr)
        self.assertIn("not a string", stderr)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])

    def test_unset_summarize_image_config_still_falls_back_to_summarize(self) -> None:
        """A genuinely unset [models].summarize_image must still take
        its documented fallback: the plain summarize model."""
        digest = self._store_binary_source(b"\x89PNG fake bytes", ".png", "image/png")
        reply = (
            "---\ntitle: Picture Overview\nidentifiers: []\n---\n\n"
            "An abstract of the picture.\n"
        )

        def respond(_path: str, body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)  # no summarize_image key
            code = _run_quiet(self.root, [digest])

        self.assertEqual(code, 0)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0].body["model"], "cheap")

    def test_unreadable_wiki_page_does_not_abort_summary_index(self) -> None:
        # agent-kb-74p item B: `_summary_index` caught only
        # FileNotFoundError from `core.read_page_text`, so a chmod-000
        # page under wiki/ raised PermissionError before the sweep's
        # loop even started (RC1's bug class, one function over).
        #
        # Isolation note: a second, separate gap with the identical
        # shape lives in `llmwiki/lint.py`'s `_read_pages` (also
        # FileNotFoundError-only; confirmed unchanged by the concurrent
        # lint.py rework via `git diff -- llmwiki/lint.py`, which never
        # touches that except clause). `_commit_summary_page` calls
        # `lint_pages`, which globs and re-reads every page in wiki/
        # for cross-page context regardless of which page it is asked
        # to lint, so any run that still needs to write a page hits
        # that second gap too, independent of this fix. lint.py is
        # owned by another agent this session and is out of scope
        # here, so this test drives the run through the path that
        # stays in scope: a locked page present while every digest is
        # already up to date, proving the sweep's setup (building the
        # index) survives it instead of dying before it can even see
        # what remains.
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        digest = self._store_source()
        reply = (
            "---\n"
            "title: Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "An abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            first = _run_quiet(self.root, [digest])
            self.assertEqual(first, 0)
            self.assertEqual(len(fake.requests), 1)

            locked_page = self.root / "wiki" / "locked.md"
            locked_page.write_text("---\nkind: story\ntitle: Locked\n---\n\nBody.\n")
            locked_page.chmod(0o000)
            self.addCleanup(locked_page.chmod, 0o644)

            second = _run_quiet(self.root, [digest])
            self.assertEqual(len(fake.requests), 1)  # still up to date, no new call

        self.assertEqual(second, 0)

    def test_summary_index_skips_unreadable_page_directly(self) -> None:
        # A tighter unit test at the exact call site item B names:
        # `_summary_index` itself must not raise on a chmod-000 page,
        # and must still index a good page alongside it.
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        digest = self._store_source()
        good_page = self.root / "wiki" / "good.md"
        good_page.write_text(
            "---\n"
            "kind: summary\n"
            "title: Good\n"
            f"source: {digest}\n"
            "prompt_fingerprint: abc123\n"
            "---\n\n"
            "Body.\n"
        )
        locked_page = self.root / "wiki" / "locked.md"
        locked_page.write_text("---\nkind: story\ntitle: Locked\n---\n\nBody.\n")
        locked_page.chmod(0o000)
        self.addCleanup(locked_page.chmod, 0o644)

        index = summarize._summary_index(Kb(self.root))

        self.assertIn(digest, index)
        self.assertEqual(index[digest], (good_page, "abc123"))

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

    def test_malformed_sidecar_returns_1_no_page_no_call_logs_digest(self) -> None:
        digest = self._store_source()
        (self.root / "sources" / f"{digest}.toml").write_text(
            "content_type = = broken\n[[[\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "wiki").glob("*.md")), [])
        log = (self.root / "log.md").read_text()
        self.assertIn(digest, log)
        self.assertIn("provenance", log)

    def test_missing_sidecar_does_not_disturb_existing_good_page(self) -> None:
        good_digest = self._store_source("A good widget's source text.")
        reply = (
            "---\n"
            "title: Good Widget\n"
            "identifiers: []\n"
            "---\n\n"
            "Good abstract.\n"
        )

        def respond(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

        with FakeEndpoint(respond) as fake:
            self._write_config(fake.url)
            self.assertEqual(_run_quiet(self.root, [good_digest]), 0)

        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        good_page_before = pages[0].read_bytes()

        broken_digest = self._store_source("A different, broken widget.")
        (self.root / "sources" / f"{broken_digest}.toml").unlink()

        def respond_unused(_path: str, _body: dict) -> dict:
            return {"choices": [{"message": {"content": "unused"}}]}

        with FakeEndpoint(respond_unused) as fake:
            self._write_config(fake.url)
            code = _run_quiet(self.root, [good_digest, broken_digest])
            self.assertEqual(fake.requests, [])

        self.assertEqual(code, 1)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].read_bytes(), good_page_before)
        log = (self.root / "log.md").read_text()
        self.assertIn(broken_digest, log)
        self.assertIn("cannot read source", log)

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

    def test_bare_identifiers_key_is_kept_as_empty_list(self) -> None:
        # agent-kb-79x: a fresh kb declares no identifier vocabulary, and
        # a real model replied with a bare `identifiers:` key rather than
        # `identifiers: []`. `parse_frontmatter` reads that as the empty
        # string, not a list; it must still be kept as an empty list.
        digest = self._store_source()
        reply = (
            "---\n"
            "title: No Vocabulary Widget\n"
            "identifiers:\n"
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
        # A bare key round-trips as the empty string, same as any other
        # page core.as_list already reads as an empty list (see its
        # docstring); the page is kept, which is the whole fix.
        self.assertEqual(fields["identifiers"], "")
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


class DropReasonTest(unittest.TestCase):
    """`_drop_reason` called directly: the acceptance rule for
    `identifiers`, without a model or the fake endpoint in the way."""

    def test_absent_identifiers_key_drops(self) -> None:
        parsed = ({"title": "Widget"}, "body")
        self.assertEqual(
            summarize._drop_reason(parsed, "Widget"),
            "identifiers missing or not a list",
        )

    def test_identifiers_list_with_blank_item_drops(self) -> None:
        parsed = ({"title": "Widget", "identifiers": ["isbn:1", ""]}, "body")
        self.assertEqual(summarize._drop_reason(parsed, "Widget"), "blank identifier")

    def test_bare_identifiers_value_is_kept(self) -> None:
        parsed = ({"title": "Widget", "identifiers": ""}, "body")
        self.assertIsNone(summarize._drop_reason(parsed, "Widget"))

    def test_empty_identifiers_list_is_kept(self) -> None:
        parsed = ({"title": "Widget", "identifiers": []}, "body")
        self.assertIsNone(summarize._drop_reason(parsed, "Widget"))

    def test_non_empty_scalar_identifiers_drops(self) -> None:
        parsed = ({"title": "Widget", "identifiers": "isbn:1"}, "body")
        self.assertEqual(
            summarize._drop_reason(parsed, "Widget"),
            "identifiers missing or not a list",
        )


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
                config_toml(fake.url, {"summarize": "cheap"})
            )
            cmd = [
                sys.executable, "-m", "llmwiki",
                "--kb", str(self.root), "summarize", digest,
            ]
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
            run = subprocess.run(
                cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env
            )

        self.assertEqual(run.returncode, 0, run.stderr)
        pages = list((self.root / "wiki").glob("*.md"))
        self.assertEqual(len(pages), 1)
        self.assertIn("CLI Widget", pages[0].read_text())


if __name__ == "__main__":
    unittest.main()
