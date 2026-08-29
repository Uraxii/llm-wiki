"""Same lint code over two kbs with different vocabularies."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb_lint import Finding, lint_pages, prompt_block  # noqa: E402

ROOT = Path(__file__).resolve().parent
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

    def test_single_page_scope(self) -> None:
        kb = self.copy("recipe")
        findings = lint_pages(kb, [kb / "wiki" / "bad-key.md"])
        self.assertEqual(pairs(findings), {("bad-key.md", "identifier-key")})

    def test_no_identifiers_table_flags_every_identifier(self) -> None:
        kb = self.copy("recipe")
        (kb / "config.toml").write_text('[models]\nsummarize = "cheap"\n')
        keys = {f.path.name for f in lint_pages(kb) if f.check == "identifier-key"}
        self.assertEqual(keys, {"omelette-summary.md", "omelette-story.md", "breakfast.md", "bad-key.md", "bad-isbn.md"})
        self.assertIn("No identifier keys", prompt_block({}))

    def test_cli_exit_code_and_log(self) -> None:
        kb = self.copy("security")
        cmd = [sys.executable, str(ROOT.parent / "kb_lint.py"), "--kb", str(kb)]
        run = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(len(run.stdout.splitlines()), 6)
        self.assertIn("## [lint] 6 findings over 7 pages - ", (kb / "log.md").read_text())
        clean = self.tmp / "clean"
        for sub in ("wiki", "sources"):
            (clean / sub).mkdir(parents=True)
        (clean / "config.toml").write_text("")
        run = subprocess.run(cmd[:3] + [str(clean)], capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.assertIn("## [lint] 0 findings over 0 pages - ", (clean / "log.md").read_text())

    def test_prompt_block_lists_declared_keys(self) -> None:
        import tomllib
        block = prompt_block(tomllib.loads((FIXTURES / "security" / ".kb" / "config.toml").read_text()))
        self.assertIn("- cve\t^CVE-", block)
        self.assertIn("- host\t(any non-empty value)\thost", block)


if __name__ == "__main__":
    unittest.main()
