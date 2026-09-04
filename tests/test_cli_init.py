"""CLI verbs: init and where."""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llmwiki import cli  # noqa: E402
from llmwiki.cli import (  # noqa: E402
    CONFIG_TOML,
    GLOBAL_STORE,
    SCHEMA_SKELETON,
    main,
)
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

    def test_config_summarize_model_is_a_vendor_free_placeholder(self) -> None:
        self.run_main(["--kb", str(self.kb), "init"])
        config = load_config(self.kb)
        summarize_model = config["models"]["summarize"]
        self.assertTrue(summarize_model)
        self.assertNotRegex(
            summarize_model, r"(?i)google|openai|gemini|anthropic|claude|gpt-"
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
    def test_shipped_example_is_uncommented_and_pattern_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_write_text(root / "config.toml", CONFIG_TOML)
            config = load_config(root)
            pattern = config["identifiers"]["isbn"]["pattern"]
            self.assertRegex("9780306406157", pattern)

    def test_names_a_join_key_and_warns_off_a_shared_key(self) -> None:
        lines = CONFIG_TOML.splitlines()
        start = next(
            i for i, line in enumerate(lines) if line.startswith("[identifiers.")
        )
        comment_lines = []
        for line in reversed(lines[:start]):
            if not line.startswith("#"):
                break
            comment_lines.insert(0, line)
        comment = "\n".join(comment_lines)
        self.assertIn("JOIN key", comment)
        self.assertIn("discriminate", comment)


class FreshInitAgreementTest(TmpDirTest):
    """A clean `lint` beside a failing `status` is the state these two
    tests exist to prevent. The skill tells an agent to run `lint`
    before finishing, so a clean `lint` is what it reports success on."""

    def test_unedited_stub_fails_both_lint_and_status(self) -> None:
        self.run_main(["--kb", str(self.kb), "init"])
        lint_code, out, _err = self.run_main(["--kb", str(self.kb), "lint"])
        status_code, _out, err = self.run_main(["--kb", str(self.kb), "status"])
        self.assertEqual((lint_code, status_code), (1, 1))
        for stream in (out, err):
            self.assertIn("[providers.hosted] is not in config.toml", stream)

    def test_uncommenting_one_provider_table_passes_both(self) -> None:
        self.run_main(["--kb", str(self.kb), "init"])
        with (self.kb / "config.toml").open("a", encoding="utf-8") as handle:
            handle.write('\n[providers.hosted]\nurl = "http://unused"\n')
        lint_code, out, _err = self.run_main(["--kb", str(self.kb), "lint"])
        status_code, _out, _err = self.run_main(["--kb", str(self.kb), "status"])
        self.assertEqual((lint_code, out, status_code), (0, "", 0))

    def test_every_models_id_names_one_provider(self) -> None:
        # The stub used to point embed at a second provider, so
        # uncommenting the one table the comments and the error message
        # both name still left the kb broken. One provider means one
        # edit clears every fault at once, which the test above proves.
        with tempfile.TemporaryDirectory() as tmp:
            atomic_write_text(Path(tmp) / "config.toml", CONFIG_TOML)
            models = load_config(Path(tmp))["models"]
        providers = {value.partition(":")[0] for value in models.values()}
        self.assertEqual(len(providers), 1)
        self.assertIn(f"[providers.{providers.pop()}]", CONFIG_TOML)


class GlobalStoreFallbackTest(TmpDirTest):
    """Resolution falling through to the global store from inside a
    repository used to be silent, so project knowledge landed in the
    global store and nothing complained."""

    def where_from(self, cwd: Path) -> tuple[int, str, str]:
        with contextlib.chdir(cwd):
            return self.run_main(["where"])

    def test_repository_without_a_kb_gets_a_stderr_note(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        code, out, err = self.where_from(repo / "sub")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(GLOBAL_STORE))
        self.assertIn(f"no .kb in {repo}", err)

    def test_repository_with_a_kb_stays_silent(self) -> None:
        repo = self.tmp / "repo-with-kb"
        (repo / ".git").mkdir(parents=True)
        (repo / ".kb").mkdir()
        code, out, err = self.where_from(repo)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), str(repo / ".kb"))

    def test_plain_directory_stays_silent(self) -> None:
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        code, out, err = self.where_from(plain)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), str(GLOBAL_STORE))


class VersionTest(TmpDirTest):
    def test_names_the_package_directory_that_ran(self) -> None:
        code, out, _err = self.run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("llmwiki "))
        self.assertIn(str(Path(cli.__file__).resolve().parent), out)


if __name__ == "__main__":
    unittest.main()
