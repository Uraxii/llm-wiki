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
    atomic_write_bytes,
    atomic_write_text,
    flatten,
    load_config,
    parse_frontmatter,
    read_page_text,
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

    def test_round_trip_survives_unicode_line_separator_in_scalar(self) -> None:
        fields = {"title": "part one\u2028part two"}
        rendered = render_frontmatter(fields, "body\n")
        self.assertEqual(parse_frontmatter(rendered), (fields, "body\n"))


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

    def test_title_with_unicode_line_separator_stays_one_line(self) -> None:
        kb = self.copy("recipe")
        log_path = kb / "log.md"
        before = log_path.read_text(encoding="utf-8")
        append_log_entry(log_path, "kind", "title\u2028forged line")
        after = log_path.read_text(encoding="utf-8")
        appended = after[len(before) :]
        self.assertEqual(len(appended.splitlines()), 1)


class FlattenTest(unittest.TestCase):
    def test_collapses_ascii_control_chars(self) -> None:
        self.assertEqual(flatten("a\nb\tc\x7fd"), "a b c d")

    def test_collapses_unicode_line_breaks(self) -> None:
        # NEL, LINE SEPARATOR, PARAGRAPH SEPARATOR: str.splitlines()
        # treats each as a line break, same as the ASCII breaks flatten
        # already collapsed.
        for name, char in (
            ("NEL", "\x85"),
            ("LINE SEPARATOR", "\u2028"),
            ("PARAGRAPH SEPARATOR", "\u2029"),
        ):
            with self.subTest(name=name):
                self.assertEqual(flatten(f"a{char}b"), "a b")

    def test_collapses_every_splitlines_break_char(self) -> None:
        # Hardcoded from the documented str.splitlines() break set
        # (docs.python.org, str.splitlines, "Line boundaries" table),
        # since Python exposes no API to read that set back.
        splitlines_breaks = (
            "\n", "\r", "\v", "\f",
            "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
        )
        for char in splitlines_breaks:
            with self.subTest(char=repr(char)):
                flattened = flatten(f"a{char}b")
                self.assertEqual(flattened.splitlines(), [flattened])


class AtomicWriteTextTest(TmpDirTest):
    def test_writes_content(self) -> None:
        target = self.tmp / "out.txt"
        atomic_write_text(target, "hello\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_missing_parent_raises_oserror(self) -> None:
        target = self.tmp / "no-such-dir" / "out.txt"
        with self.assertRaises(OSError):
            atomic_write_text(target, "hello\n")


class AtomicWriteBytesTest(TmpDirTest):
    def test_writes_bytes_exactly(self) -> None:
        target = self.tmp / "out.bin"
        data = b"body with \xff\xfe bad bytes\n"
        atomic_write_bytes(target, data)
        self.assertEqual(target.read_bytes(), data)

    def test_missing_parent_raises_oserror(self) -> None:
        target = self.tmp / "no-such-dir" / "out.bin"
        with self.assertRaises(OSError):
            atomic_write_bytes(target, b"hello\n")

    def test_text_write_is_byte_identical_to_bytes_write(self) -> None:
        text_target = self.tmp / "text.txt"
        bytes_target = self.tmp / "bytes.txt"
        content = "hello\nworld\n"
        atomic_write_text(text_target, content)
        atomic_write_bytes(bytes_target, content.encode("utf-8"))
        self.assertEqual(text_target.read_bytes(), bytes_target.read_bytes())


class ReadPageTextTest(TmpDirTest):
    def test_replaces_invalid_utf8(self) -> None:
        target = self.tmp / "bad.md"
        target.write_bytes(b"---\ntitle: x\n---\n\nbody with \xff\xfe bad bytes\n")
        text = read_page_text(target)
        self.assertIn("�", text)
        self.assertTrue(text.startswith("---\ntitle: x\n---\n\n"))

    def test_valid_utf8_round_trips(self) -> None:
        target = self.tmp / "good.md"
        target.write_text("---\ntitle: x\n---\n\nplain body\n", encoding="utf-8")
        self.assertEqual(read_page_text(target), "---\ntitle: x\n---\n\nplain body\n")


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
        self.assertEqual(config["models"]["summarize"], "test:cheap")


if __name__ == "__main__":
    unittest.main()
