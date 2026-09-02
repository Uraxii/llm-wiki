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


def init_kb(root: Path) -> Path:
    """A fresh, otherwise-clean kb at `root`: empty wiki/, sources/,
    config, and log, ready for a test to drop its own pages into."""
    for sub in ("wiki", "sources"):
        (root / sub).mkdir(parents=True)
    (root / "config.toml").write_text("")
    (root / "log.md").write_text("# log\n")
    return root


def write_page(kb: Path, name: str, frontmatter: str) -> None:
    (kb / "wiki" / name).write_text(frontmatter)


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
        dup = init_kb(self.tmp / "dup-for-coverage")
        write_page(dup, "a.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        write_page(dup, "b.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        fired = (
            pairs(lint_pages(self.copy("recipe")))
            | pairs(lint_pages(self.copy("security")))
            | pairs(lint_pages(dup))
        )
        checks_fired = {check for _name, check in fired}
        self.assertEqual(checks_fired, {name for name, _fn in CHECKS})

    def test_duplicate_title_flags_both_pages(self) -> None:
        kb = init_kb(self.tmp / "dup-exact")
        write_page(kb, "a.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        write_page(kb, "b.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        self.assertEqual(
            pairs(lint_pages(kb)),
            {("a.md", "duplicate-title"), ("b.md", "duplicate-title")},
        )

    def test_duplicate_title_flags_slug_collision_of_different_titles(self) -> None:
        kb = init_kb(self.tmp / "dup-slug")
        write_page(kb, "a.md", "---\ntitle: Coffee Gear\n---\n\nBody.\n")
        write_page(kb, "b.md", "---\ntitle: Coffee, Gear!\n---\n\nBody.\n")
        self.assertEqual(
            pairs(lint_pages(kb)),
            {("a.md", "duplicate-title"), ("b.md", "duplicate-title")},
        )

    def test_duplicate_title_message_names_slug_and_other_page(self) -> None:
        kb = init_kb(self.tmp / "dup-message")
        write_page(kb, "a.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        write_page(kb, "b.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        detail_by_name = {f.path.name: f.detail for f in lint_pages(kb) if f.check == "duplicate-title"}
        self.assertIn("coffee-gear-runbook", detail_by_name["a.md"])
        self.assertIn("b.md", detail_by_name["a.md"])
        self.assertIn("a.md", detail_by_name["b.md"])

    def test_duplicate_title_no_finding_when_stem_matches_own_slug(self) -> None:
        kb = init_kb(self.tmp / "clean-stems")
        write_page(kb, "coffee-gear.md", "---\ntitle: Coffee Gear\n---\n\nBody.\n")
        write_page(kb, "tea-set.md", "---\ntitle: Tea Set\n---\n\nBody.\n")
        self.assertEqual(pairs(lint_pages(kb)), set())

    def test_duplicate_title_skips_page_with_no_title(self) -> None:
        kb = init_kb(self.tmp / "no-title")
        write_page(kb, "a.md", "---\ntitle: Coffee Gear\n---\n\nBody.\n")
        write_page(kb, "b.md", "---\nkind: story\n---\n\nBody.\n")
        self.assertEqual(pairs(lint_pages(kb)), set())

    def test_duplicate_title_skips_page_with_unparseable_frontmatter(self) -> None:
        kb = init_kb(self.tmp / "bad-frontmatter")
        write_page(kb, "a.md", "---\ntitle: Coffee Gear\n---\n\nBody.\n")
        write_page(kb, "b.md", "no frontmatter here\n")
        self.assertEqual(pairs(lint_pages(kb)), {("b.md", "frontmatter")})

    def test_cites_summary_still_fires_when_an_agent_page_shares_a_summarys_title(self) -> None:
        # kind_by_key used to be single-valued: whichever page with this
        # title slug got read LAST won the map, so a later agent-written
        # page with the same title as an earlier summary silently erased
        # the summary's entry and cites-summary went blind to a real
        # citation. Filenames are alphabetical so the note reads after
        # the summary and would clobber a single-valued map.
        kb = init_kb(self.tmp / "cites-summary-collision")
        digest = "deadbeef"
        (kb / "sources" / f"{digest}.toml").write_text('url = "https://x"\n')
        (kb / "sources" / f"{digest}.md").write_text("byte content")
        write_page(
            kb,
            "a-summary.md",
            f"---\nkind: summary\ntitle: Shared Title\nsource: {digest}\nidentifiers: []\n---\n\nBody.\n",
        )
        write_page(kb, "b-note.md", "---\nkind: note\ntitle: Shared Title\n---\n\nBody.\n")
        write_page(kb, "c-linker.md", "---\nkind: note\ntitle: Linker\n---\n\n[[Shared Title]]\n")

        cites = {(f.path.name, f.detail) for f in lint_pages(kb) if f.check == "cites-summary"}
        self.assertEqual(cites, {("c-linker.md", "[[Shared Title]] is a summary")})

    def test_duplicate_title_silent_when_every_sharer_is_a_summary_page(self) -> None:
        kb = init_kb(self.tmp / "dup-two-summaries")
        for name, digest in (("a.md", "aaa111"), ("b.md", "bbb222")):
            (kb / "sources" / f"{digest}.toml").write_text('url = "https://x"\n')
            (kb / "sources" / f"{digest}.md").write_text("byte content")
            write_page(
                kb,
                name,
                f"---\nkind: summary\ntitle: Coffee gear runbook\nsource: {digest}\nidentifiers: []\n---\n\nBody.\n",
            )
        self.assertEqual(pairs(lint_pages(kb)), set())

    def test_duplicate_title_silent_when_every_sharer_is_a_story_page(self) -> None:
        kb = init_kb(self.tmp / "dup-two-stories")
        write_page(kb, "a.md", "---\nkind: story\ntitle: Coffee gear runbook\nmembers: []\n---\n\nBody.\n")
        write_page(kb, "b.md", "---\nkind: story\ntitle: Coffee gear runbook\nmembers: []\n---\n\nBody.\n")
        self.assertEqual(pairs(lint_pages(kb)), set())

    def test_duplicate_title_silent_when_an_agent_page_shares_a_summarys_title(self) -> None:
        kb = init_kb(self.tmp / "dup-agent-and-summary")
        digest = "cafef00d"
        (kb / "sources" / f"{digest}.toml").write_text('url = "https://x"\n')
        (kb / "sources" / f"{digest}.md").write_text("byte content")
        write_page(
            kb,
            "a-summary.md",
            f"---\nkind: summary\ntitle: Coffee gear runbook\nsource: {digest}\nidentifiers: []\n---\n\nBody.\n",
        )
        write_page(kb, "b-note.md", "---\nkind: note\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        self.assertEqual(pairs(lint_pages(kb)), set())

    def test_unreadable_page_is_skipped_not_raised(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        kb = init_kb(self.tmp / "unreadable-page")
        write_page(
            kb,
            "good.md",
            "---\ntitle: Good\nidentifiers:\n  - bogus:1\n---\n\nBody.\n",
        )
        locked = kb / "wiki" / "locked.md"
        write_page(kb, "locked.md", "---\ntitle: Locked\n---\n\nBody.\n")
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o644)
        findings = lint_pages(kb)
        self.assertEqual(pairs(findings), {("good.md", "identifier-key")})

    def test_duplicate_title_caps_names_and_summarises_the_rest(self) -> None:
        kb = init_kb(self.tmp / "dup-quadratic")
        for i in range(7):
            write_page(kb, f"page-{i}.md", "---\nkind: note\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        detail_by_name = {f.path.name: f.detail for f in lint_pages(kb) if f.check == "duplicate-title"}
        self.assertEqual(len(detail_by_name), 7)
        self.assertEqual(
            detail_by_name["page-0.md"],
            "title slug 'coffee-gear-runbook' also used by "
            "page-1.md, page-2.md, page-3.md, page-4.md, page-5.md and 1 more",
        )

    def test_cites_summary_resolves_by_stem_before_title_slug(self) -> None:
        # Old bug: kind_by_key merged filename stems and title slugs into
        # one dict. [[coffee-gear]] names the note's exact filename, not
        # the summary that happens to share its title, but the merge made
        # the note's stem key inherit the summary's kind.
        kb = init_kb(self.tmp / "stem-vs-slug")
        write_page(kb, "coffee-gear.md", "---\nkind: note\ntitle: Coffee Gear\n---\n\nBody.\n")
        digest = "beadbead"
        (kb / "sources" / f"{digest}.toml").write_text('url = "https://x"\n')
        (kb / "sources" / f"{digest}.md").write_text("byte content")
        write_page(
            kb,
            "coffee-gear-d2.md",
            f"---\nkind: summary\ntitle: Coffee Gear\nsource: {digest}\nidentifiers: []\n---\n\nBody.\n",
        )
        write_page(
            kb,
            "linker.md",
            "---\nkind: note\ntitle: Linker\n---\n\n[[coffee-gear]] and [[Coffee Gear]]\n",
        )
        cites = {(f.path.name, f.detail) for f in lint_pages(kb) if f.check == "cites-summary"}
        self.assertEqual(cites, {("linker.md", "[[Coffee Gear]] is a summary")})

    def test_duplicate_title_agent_pages_stay_flagged_after_summary_joins(self) -> None:
        # Old bug: the check silenced the whole slug group the moment ANY
        # page in it was CLI-owned. summarize writing a third page with
        # the same title made a genuine agent-agent duplicate vanish.
        kb = init_kb(self.tmp / "dup-agent-pair-plus-summary")
        write_page(kb, "note-a.md", "---\nkind: note\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        write_page(kb, "note-b.md", "---\nkind: note\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        before = pairs(lint_pages(kb))
        self.assertEqual(
            before,
            {("note-a.md", "duplicate-title"), ("note-b.md", "duplicate-title")},
        )

        digest = "feedface"
        (kb / "sources" / f"{digest}.toml").write_text('url = "https://x"\n')
        (kb / "sources" / f"{digest}.md").write_text("byte content")
        write_page(
            kb,
            "z-summary.md",
            f"---\nkind: summary\ntitle: Coffee gear runbook\nsource: {digest}\nidentifiers: []\n---\n\nBody.\n",
        )
        after = {(name, check) for name, check in pairs(lint_pages(kb)) if check == "duplicate-title"}
        self.assertEqual(
            after,
            {("note-a.md", "duplicate-title"), ("note-b.md", "duplicate-title")},
        )

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

    def test_dangling_source_needs_a_byte_file_not_just_a_sidecar(self) -> None:
        kb = self.tmp / "sidecar-only"
        for sub in ("wiki", "sources"):
            (kb / sub).mkdir(parents=True)
        (kb / "config.toml").write_text("")
        digest = "abc123"
        page = kb / "wiki" / "summary.md"
        page.write_text(
            "---\n"
            "kind: summary\n"
            "title: Summary\n"
            f"source: {digest}\n"
            "identifiers: []\n"
            "---\n\n"
            "Body.\n"
        )
        (kb / "sources" / f"{digest}.toml").write_text('url = "https://x"\n')

        self.assertEqual(pairs(lint_pages(kb)), {("summary.md", "dangling-source")})

        (kb / "sources" / f"{digest}.md").write_text("byte content")

        self.assertEqual(pairs(lint_pages(kb)), set())

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

    def test_duplicate_title_exits_nonzero_over_the_cli(self) -> None:
        kb = init_kb(self.tmp / "dup-cli")
        write_page(kb, "a.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        write_page(kb, "b.md", "---\ntitle: Coffee gear runbook\n---\n\nBody.\n")
        run = self.run_module(kb, [])
        self.assertEqual(run.returncode, 1)
        lines = run.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("duplicate-title" in line for line in lines))

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
