"""CLI verbs: init and where."""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llmwiki.cli import CONFIG_TOML, SCHEMA_SKELETON, main  # noqa: E402
from llmwiki.core import atomic_write_text, load_config  # noqa: E402

EXPECTED_ENTRIES = {
    "config.toml",
    "SCHEMA.md",
    "SUMMARIZE.md",
    "log.md",
    ".gitignore",
    "sources",
    "wiki",
}


class TmpDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.kb = self.tmp / ".kb"

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class InitTest(TmpDirTest):
    def test_creates_exact_file_set(self) -> None:
        code, _out, _err = self.run_main(["--kb", str(self.kb), "init"])
        self.assertEqual(code, 0)
        entries = {p.name for p in self.kb.iterdir()}
        self.assertEqual(entries, EXPECTED_ENTRIES)
        self.assertTrue((self.kb / "sources").is_dir())
        self.assertTrue((self.kb / "wiki").is_dir())
        self.assertEqual(list((self.kb / "sources").iterdir()), [])
        self.assertEqual(list((self.kb / "wiki").iterdir()), [])

    def test_config_has_default_summarize_model(self) -> None:
        self.run_main(["--kb", str(self.kb), "init"])
        config = load_config(self.kb)
        self.assertEqual(
            config["models"]["summarize"], "google/gemini-2.5-flash"
        )

    def test_schema_matches_skeleton_bytes(self) -> None:
        self.run_main(["--kb", str(self.kb), "init"])
        written = (self.kb / "SCHEMA.md").read_bytes()
        self.assertEqual(written, SCHEMA_SKELETON.read_bytes())

    def test_second_init_refuses_and_changes_nothing(self) -> None:
        self.run_main(["--kb", str(self.kb), "init"])
        before = {
            p: p.read_bytes() if p.is_file() else None
            for p in self.kb.rglob("*")
        }
        code, _out, err = self.run_main(["--kb", str(self.kb), "init"])
        self.assertEqual(code, 1)
        self.assertNotEqual(err, "")
        after = {
            p: p.read_bytes() if p.is_file() else None
            for p in self.kb.rglob("*")
        }
        self.assertEqual(before, after)


class WhereTest(TmpDirTest):
    def test_prints_explicit_kb_path(self) -> None:
        code, out, _err = self.run_main(["--kb", str(self.kb), "where"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.kb))


class UnknownVerbTest(unittest.TestCase):
    def test_returns_2(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["bogus"])
        self.assertEqual(code, 2)
        self.assertNotEqual(err.getvalue(), "")

    def test_missing_verb_returns_2(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([])
        self.assertEqual(code, 2)
        self.assertNotEqual(err.getvalue(), "")


class SubprocessTest(unittest.TestCase):
    def test_module_invocation(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / ".kb"
            result = subprocess.run(
                [sys.executable, "-m", "llmwiki", "--kb", str(kb), "where"],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), str(kb))


class ConfigIdentifiersExampleTest(unittest.TestCase):
    def test_uncommented_example_parses_and_pattern_matches(self) -> None:
        lines = CONFIG_TOML.splitlines()
        start = next(
            i for i, line in enumerate(lines) if line.startswith("# [identifiers.")
        )
        block = []
        for line in lines[start:]:
            if not line.startswith("#"):
                break
            block.append(line.removeprefix("# "))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_write_text(
                root / "config.toml", CONFIG_TOML + "\n".join(block) + "\n"
            )
            config = load_config(root)
            pattern = config["identifiers"]["isbn"]["pattern"]
            self.assertRegex("9780306406157", pattern)


if __name__ == "__main__":
    unittest.main()
