"""Lint checks over two kbs with different vocabularies, plus the CLI verb."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llmwiki.cli import main  # noqa: E402
from llmwiki.lint import CHECKS, Finding, lint_pages, prompt_block  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
FIXTURES = ROOT / "fixtures"

EXPECTED = {
    "recipe": {
        ("bad-isbn.md", "identifier-value"),
        ("bad-key.md", "identifier-key"),
        ("broken.md", "frontmatter"),
        ("cites-summary.md", "cites-summary"),
    },
    "security": {
        ("bad-cve.md", "identifier-value"),
        ("bad-story.md", "story-member"),
        ("dangling-summary.md", "dangling-source"),
        ("unparseable.md", "frontmatter"),
    },
}


def pairs(findings: list[Finding]) -> set[tuple[str, str]]:
    return {(f.path.name, f.check) for f in findings}


class LintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def copy(self, name: str) -> Path:
        return Path(shutil.copytree(FIXTURES / name / ".kb", self.tmp / name))

    def test_findings_per_kb(self) -> None:
        for name, expected in EXPECTED.items():
            with self.subTest(kb=name):
                self.assertEqual(pairs(lint_pages(self.copy(name))), expected)

    def test_security_has_six_findings_over_seven_pages(self) -> None:
        kb = self.copy("security")
        findings = lint_pages(kb)
        self.assertEqual(len(findings), 6)
        self.assertEqual(len(list((kb / "wiki").glob("*.md"))), 7)

    def test_every_check_fires_at_least_once(self) -> None:
        fired = pairs(lint_pages(self.copy("recipe"))) | pairs(lint_pages(self.copy("security")))
        checks_fired = {check for _name, check in fired}
        self.assertEqual(checks_fired, {name for name, _fn in CHECKS})

    def test_single_page_scope(self) -> None:
        kb = self.copy("recipe")
        findings = lint_pages(kb, [kb / "wiki" / "bad-key.md"])
        self.assertEqual(pairs(findings), {("bad-key.md", "identifier-key")})

    def test_no_identifiers_table_flags_every_identifier(self) -> None:
        kb = self.copy("recipe")
        (kb / "config.toml").write_text('[models]\nsummarize = "cheap"\n')
        keys = {f.path.name for f in lint_pages(kb) if f.check == "identifier-key"}
        self.assertEqual(
            keys,
            {"omelette-summary.md", "omelette-story.md", "breakfast.md", "bad-key.md", "bad-isbn.md"},
        )
        self.assertIn("No identifier keys", prompt_block({}))

    def test_prompt_block_lists_declared_keys(self) -> None:
        import tomllib

        config = tomllib.loads((FIXTURES / "security" / ".kb" / "config.toml").read_text())
        block = prompt_block(config)
        self.assertIn("- cve\t^CVE-", block)
        self.assertIn("- host\t(any non-empty value)\thost", block)


class LintCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def copy(self, name: str) -> Path:
        return Path(shutil.copytree(FIXTURES / name / ".kb", self.tmp / name))

    def run_module(self, kb: Path, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(kb), "lint", *args]
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(cmd, cwd=str(cwd or REPO_ROOT), capture_output=True, text=True, env=env)

    def test_exit_code_and_log_on_findings(self) -> None:
        kb = self.copy("security")
        run = self.run_module(kb, [])
        self.assertEqual(run.returncode, 1)
        self.assertEqual(len(run.stdout.splitlines()), 6)
        self.assertIn("## [lint] 6 findings over 7 pages - ", (kb / "log.md").read_text())

    def test_recipe_exit_code_and_line_count(self) -> None:
        kb = self.copy("recipe")
        run = self.run_module(kb, [])
        self.assertEqual(run.returncode, 1)
        self.assertEqual(len(run.stdout.splitlines()), 4)

    def test_clean_kb_is_silent_and_zero(self) -> None:
        clean = self.tmp / "clean"
        for sub in ("wiki", "sources"):
            (clean / sub).mkdir(parents=True)
        (clean / "config.toml").write_text("")
        (clean / "log.md").write_text("# log\n")
        run = self.run_module(clean, [])
        self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.assertIn("## [lint] 0 findings over 0 pages - ", (clean / "log.md").read_text())

    def test_single_page_argument_filters(self) -> None:
        kb = self.copy("recipe")
        run = self.run_module(kb, ["wiki/bad-key.md"], cwd=kb)
        self.assertEqual(run.returncode, 1)
        lines = run.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("bad-key.md", lines[0])
        self.assertIn("identifier-key", lines[0])


if __name__ == "__main__":
    unittest.main()
