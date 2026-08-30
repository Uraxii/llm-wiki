"""Shared primitives: frontmatter parse/render, slugify, atomic write,
log entries, config loading.
"""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llmwiki.core import (  # noqa: E402
    append_log_entry,
    atomic_write_text,
    load_config,
    parse_frontmatter,
    render_frontmatter,
    slugify,
)

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
LOG_LINE_RE = re.compile(
    r"^## \[kind\] title - \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)


class TmpDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def copy(self, kb_name: str) -> Path:
        return Path(shutil.copytree(FIXTURES / kb_name / ".kb", self.tmp / kb_name))


class FrontmatterRoundTripTest(TmpDirTest):
    def test_literal_round_trip_block_lists(self) -> None:
        kb = self.copy("recipe")
        page_path = kb / "wiki" / "omelette-story.md"
        original = page_path.read_text(encoding="utf-8")
        parsed = parse_frontmatter(original)
        self.assertIsNotNone(parsed)
        fields, body = parsed
        self.assertEqual(render_frontmatter(fields, body), original)

    def test_semantic_round_trip_inline_lists(self) -> None:
        kb = self.copy("security")
        page_path = kb / "wiki" / "incident-story.md"
        original = page_path.read_text(encoding="utf-8")
        first = parse_frontmatter(original)
        self.assertIsNotNone(first)
        rendered = render_frontmatter(*first)
        second = parse_frontmatter(rendered)
        self.assertEqual(second, first)


class FrontmatterMalformedTest(TmpDirTest):
    def test_no_closing_fence(self) -> None:
        kb = self.copy("recipe")
        text = (kb / "wiki" / "broken.md").read_text(encoding="utf-8")
        self.assertIsNone(parse_frontmatter(text))

    def test_orphan_list_item(self) -> None:
        kb = self.copy("security")
        text = (kb / "wiki" / "unparseable.md").read_text(encoding="utf-8")
        self.assertIsNone(parse_frontmatter(text))

    def test_non_key_value_line(self) -> None:
        text = "---\nkind: story\njust some text\n---\n\nbody\n"
        self.assertIsNone(parse_frontmatter(text))


class SlugifyTest(unittest.TestCase):
    def test_nfc_and_nfd_fold_to_one_slug(self) -> None:
        nfc_title = "Café Story"  # single precomposed e-acute (NFC)
        nfd_title = "Café Story"  # bare e plus combining acute (NFD)
        self.assertNotEqual(nfc_title, nfd_title)
        self.assertEqual(slugify(nfc_title), slugify(nfd_title))


class LogEntryTest(TmpDirTest):
    def test_entry_matches_shape(self) -> None:
        kb = self.copy("recipe")
        log_path = kb / "log.md"
        append_log_entry(log_path, "kind", "title")
        last_line = log_path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertRegex(last_line, LOG_LINE_RE)

    def test_missing_log_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            append_log_entry(self.tmp / "no-such-log.md", "kind", "title")


class AtomicWriteTextTest(TmpDirTest):
    def test_writes_content(self) -> None:
        target = self.tmp / "out.txt"
        atomic_write_text(target, "hello\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_missing_parent_raises_oserror(self) -> None:
        target = self.tmp / "no-such-dir" / "out.txt"
        with self.assertRaises(OSError):
            atomic_write_text(target, "hello\n")


class LoadConfigTest(TmpDirTest):
    def test_missing_file_returns_empty_dict(self) -> None:
        self.assertEqual(load_config(self.tmp), {})

    def test_malformed_file_raises_naming_path(self) -> None:
        config_path = self.tmp / "config.toml"
        config_path.write_text("not = valid = toml\n", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            load_config(self.tmp)
        self.assertIn(str(config_path), str(ctx.exception))

    def test_real_fixture_config_loads(self) -> None:
        kb = self.copy("recipe")
        config = load_config(kb)
        self.assertEqual(config["models"]["summarize"], "cheap")


if __name__ == "__main__":
    unittest.main()
