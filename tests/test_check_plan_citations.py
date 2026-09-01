"""Regression guard for scripts/check-plan-citations.py: proves the RED and
GREEN cases shown by hand in the phase report stay caught after any future
edit to the checker itself.
"""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-plan-citations.py"

_spec = importlib.util.spec_from_file_location("check_plan_citations", SCRIPT)
checker = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checker  # dataclass() needs the module registered
_spec.loader.exec_module(checker)


class ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name: str, text: str) -> None:
        (self.tmp / name).write_text(text, encoding="utf-8")

    def test_correct_citation_is_not_flagged(self) -> None:
        self.write(
            "doc.md",
            "`NEIGHBOUR_FLOOR = 0.35` (`vectors.py:46`) is the standing "
            "proof.\n",
        )
        self.assertEqual(checker.scan(self.tmp), [])

    def test_shifted_line_is_flagged(self) -> None:
        self.write(
            "doc.md",
            "`NEIGHBOUR_FLOOR = 0.35` (`vectors.py:999`) is wrong on "
            "purpose.\n",
        )
        stale = checker.scan(self.tmp)
        self.assertEqual(len(stale), 1)
        self.assertIn("vectors.py:46-46", stale[0][1])

    def test_deleted_symbol_is_flagged(self) -> None:
        self.write(
            "doc.md",
            "`_expected_fingerprint` (`summarize.py:200`) was removed by "
            "cbdc38c.\n",
        )
        stale = checker.scan(self.tmp)
        self.assertEqual(len(stale), 1)
        self.assertIn("deleted or renamed", stale[0][1])

    def test_decisions_tsv_is_skipped(self) -> None:
        self.write("decisions.tsv", "not\treal\t`bogus.py:1`\n")
        self.assertEqual(checker.scan(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
