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

    def test_ingest_usage_mentions_url(self) -> None:
        result = self._run(["ingest"])
        self.assertIn("url", result.stderr)

    def test_ingest_job_flag_mixed_with_a_url_is_rejected(self) -> None:
        result = self._run(["ingest", "--job", "myjob", "https://example.test"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_ingest_job_flag_with_no_name_is_rejected(self) -> None:
        result = self._run(["ingest", "--job"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_ingest_unknown_job_name_exits_2(self) -> None:
        result = self._run(["ingest", "--job", "no-such-job"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("[jobs.no-such-job]", result.stderr)

    def test_init_generates_config_with_an_active_embed_model(self) -> None:
        """P6: agent-kb-0zf.21's last comment is a user override to
        openai/text-embedding-3-small, superseding the arena's earlier
        pick; a fresh config.toml must carry it uncommented so vectors
        exist from day one (decision .1)."""
        fresh = self.tmp / "fresh-embed" / ".kb"
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(fresh), "init"]
        subprocess.run(cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env)

        config = (fresh / "config.toml").read_text()
        self.assertIn('embed = "openai/text-embedding-3-small"', config)

        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(fresh), "embed"]
        result = subprocess.run(cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("missing [models].embed", result.stderr)


class EmbedAwareCliTest(unittest.TestCase):
    """Verbs exercised with `[models] embed` configured: `search`'s
    `-n`/`--kind` parsing (P3, P4) and `ingest`'s embed disclosure
    (P2)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.kb = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.kb / sub).mkdir(parents=True)
        (self.kb / "log.md").write_text("# log\n")

        def respond(path: str, body: dict) -> dict:
            if path == "/embeddings":
                data = [
                    {"index": i, "embedding": [1.0, 0.0]}
                    for i in range(len(body["input"]))
                ]
                return {"data": data}
            return {"choices": [{"message": {"content": GOOD_REPLY}}]}

        self.fake = FakeEndpoint(respond)
        self.addCleanup(self.fake.close)
        (self.kb / "config.toml").write_text(
            '[models]\nsummarize = "cheap"\nembed = "embed-model"\n\n'
            f'[endpoint]\nurl = "{self.fake.url}"\n'
        )
        self.env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), API_KEY_VAR: "fake-key"}

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "llmwiki", "--kb", str(self.kb), *args]
        return subprocess.run(
            cmd, cwd=str(self.tmp), capture_output=True, text=True, env=self.env
        )

    # -- P3: trailing flag with no value -----------------------------

    def test_trailing_n_flag_with_no_value_is_usage_exit_2(self) -> None:
        result = self._run(["search", "cats", "-n"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_trailing_kind_flag_with_no_value_is_usage_exit_2(self) -> None:
        result = self._run(["search", "cats", "--kind"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    # -- P4: -n below 1, rejected before the paid query embed --------

    def test_n_zero_is_usage_exit_2_before_any_model_call(self) -> None:
        result = self._run(["search", "-n", "0", "cats"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_n_negative_is_usage_exit_2_before_any_model_call(self) -> None:
        result = self._run(["search", "-n", "-3", "cats"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_n_not_a_number_is_usage_exit_2_before_any_model_call(self) -> None:
        result = self._run(["search", "-n", "notanumber", "cats"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.fake.requests, [])

    # -- P2: ingest discloses its embed spend -------------------------

    def test_ingest_prints_embed_planned_line(self) -> None:
        source = self.tmp / "widget.txt"
        source.write_text("Widget source text.")
        result = self._run(["ingest", str(source)])
        self.assertEqual(result.returncode, 0, result.stderr)
        planned = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("embed:") and line.endswith("planned")
        ]
        self.assertTrue(planned, result.stdout)


if __name__ == "__main__":
    unittest.main()
