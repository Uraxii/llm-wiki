"""Subprocess smoke test of every verb in the CLI's verb table."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llmwiki.model import API_KEY_VAR  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
