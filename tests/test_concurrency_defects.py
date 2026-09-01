"""RED regression gates for five defects from the concurrency audit,
reproduced deterministically (monkeypatch or direct disk/db edits), no
sleep, no thread timing, no OS-level race.

    .venv/bin/python -m unittest tests.test_concurrency_defects -v

Each test names the defect (D2/D3/D5/D7/D10) and the exact site from
the audit handoff. Every one fails against the unmodified tree; that
failure is the proof the defect is real. No production code changes
here.
"""

import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlite_vec  # noqa: E402

from llmwiki.core import Kb, render_frontmatter  # noqa: E402
from llmwiki.model import API_KEY_FILE_VAR, API_KEY_VAR  # noqa: E402
from llmwiki import dedup, lint, summarize, vectors  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402


def _respond(vector_for):
    """`FakeEndpoint` handler answering `/embeddings` from
    `vector_for(text)`. Copied in shape from `tests/test_vectors.py`'s
    own `_respond`; not imported from it since that helper also wires
    a chat reply this file never needs."""

    def respond(path: str, body: dict) -> dict:
        if path == "/embeddings":
            return {
                "data": [
                    {"index": i, "embedding": vector_for(text)}
                    for i, text in enumerate(body["input"])
                ]
            }
        raise AssertionError(f"unexpected request path {path!r}")

    return respond


class _KbTestCase(unittest.TestCase):
    """One `.kb` per test, in a tempdir, cleaned up on teardown. Same
    shape as `tests/test_vectors.py`'s fixture."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")
        previous_key = os.environ.get(API_KEY_VAR)
        previous_key_file = os.environ.get(API_KEY_FILE_VAR)
        os.environ[API_KEY_VAR] = "test-key"
        os.environ.pop(API_KEY_FILE_VAR, None)

        def restore() -> None:
            if previous_key is None:
                os.environ.pop(API_KEY_VAR, None)
            else:
                os.environ[API_KEY_VAR] = previous_key
            if previous_key_file is not None:
                os.environ[API_KEY_FILE_VAR] = previous_key_file

        self.addCleanup(restore)

    def _write_page(self, name: str, title: str, kind: str = "note", body: str = "Body text.") -> Path:
        path = self.root / "wiki" / f"{name}.md"
        path.write_text(render_frontmatter({"kind": kind, "title": title}, body))
        return path

    def _write_config(self, url: str) -> None:
        (self.root / "config.toml").write_text(
            '[models]\nsummarize = "cheap"\nembed = "embed-model"\n\n'
            f'[endpoint]\nurl = "{url}"\n'
        )

    def _open_vec_db(self, kb: Kb) -> sqlite3.Connection:
        conn = sqlite3.connect(vectors.db_path(kb, "embed-model"))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn


class D2ReaderTocTou(_KbTestCase):
    """D2: glob-then-read TOCTOU. Five reader sites each glob
    `wiki/*.md` then read a page found by that glob; a page unlinked
    in between must be skipped, not raised on. Each test vanishes the
    page inside the read step of the SAME call the glob ran in, so the
    file is provably still there at glob time and provably gone by
    read time -- no timing, no thread."""

    def test_d2_lint_lint_pages_skips_vanished_page(self) -> None:
        """lint.py:174-176. `lint_pages` globs `wiki/*.md` then reads
        every page found through `read_page_text`. Vanish the one page
        inside that read call."""
        page = self._write_page("vanish", "Vanish")
        original = lint.read_page_text

        def read_then_vanish(path: Path) -> str:
            if path == page:
                path.unlink()
            return original(path)

        with patch.object(lint, "read_page_text", read_then_vanish):
            try:
                findings = lint.lint_pages(self.root)
            except FileNotFoundError as exc:
                self.fail(
                    f"lint_pages raised {exc!r} on a page unlinked between "
                    "its glob and its read, instead of skipping it"
                )
        self.assertEqual(findings, [])

    def test_d2_dedup_load_wiki_skips_vanished_page(self) -> None:
        """dedup.py:321-322. `_load_wiki` globs `wiki/*.md` then reads
        each page through `read_page_text`."""
        page = self._write_page("vanish", "Vanish")
        original = dedup.read_page_text

        def read_then_vanish(path: Path) -> str:
            if path == page:
                path.unlink()
            return original(path)

        kb = Kb(self.root)
        with patch.object(dedup, "read_page_text", read_then_vanish):
            try:
                _summaries, _stories, agent_pages = dedup._load_wiki(kb)
            except FileNotFoundError as exc:
                self.fail(
                    f"_load_wiki raised {exc!r} on a page unlinked between "
                    "its glob and its read, instead of skipping it"
                )
        self.assertEqual(agent_pages, [])

    def test_d2_summarize_summary_index_skips_vanished_page(self) -> None:
        """summarize.py:110-111. `_summary_index` globs `wiki/*.md`
        then reads each page through `read_page_text`."""
        page = self._write_page("vanish", "Vanish", kind="summary")
        original = summarize.read_page_text

        def read_then_vanish(path: Path) -> str:
            if path == page:
                path.unlink()
            return original(path)

        kb = Kb(self.root)
        with patch.object(summarize, "read_page_text", read_then_vanish):
            try:
                index = summarize._summary_index(kb)
            except FileNotFoundError as exc:
                self.fail(
                    f"_summary_index raised {exc!r} on a page unlinked "
                    "between its glob and its read, instead of skipping it"
                )
        self.assertEqual(index, {})

    def test_d2_vectors_plan_skips_vanished_page(self) -> None:
        """vectors.py:183-189. `_plan` globs pages through
        `select_pages`, then reads each one's bytes directly inside
        `_page_row` (not through `core.read_page_text`, a second
        implementation of the same hazard). Vanish the page right
        after `select_pages` hands back the list that still names it."""
        page = self._write_page("vanish", "Vanish")
        original_select_pages = vectors.select_pages

        def select_then_vanish(root: Path, pages) -> list[Path]:
            found = original_select_pages(root, pages)
            for candidate in found:
                if candidate == page:
                    candidate.unlink()
            return found

        kb = Kb(self.root)
        with patch.object(vectors, "select_pages", select_then_vanish):
            try:
                stale, _to_delete, _seen = vectors._plan(kb, None)
            except FileNotFoundError as exc:
                self.fail(
                    f"_plan raised {exc!r} on a page unlinked between its "
                    "glob and its read, instead of skipping it"
                )
        self.assertEqual(stale, [])

    def test_d2_vectors_search_skips_vanished_page(self) -> None:
        """vectors.py:364-365. `search` resolves each vec0 hit back to
        a wiki file with `path.stat()`. A page embedded, then removed
        without a re-`embed`, leaves a stale row `_nearest` still
        returns; `search` must skip that hit, not crash reading a
        file the row itself proves is gone."""
        page = self._write_page("vanish", "Vanish")

        def vector_for(_text: str) -> list[float]:
            return [1.0, 0.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            vectors.sweep(Kb(self.root))

        page.unlink()

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            try:
                code = vectors.search(self.root, "a query")
            except FileNotFoundError as exc:
                self.fail(
                    f"search raised {exc!r} resolving a vec0 hit for a "
                    "page removed since its last embed, instead of "
                    "skipping that hit"
                )
        self.assertEqual(code, 0)


class D3ZeroByteClaim(_KbTestCase):
    """D3: `_claim_new_summary_path` links an empty `mkstemp` file into
    place as its claim step; content lands only on the caller's next
    write. A crash in that window is simulated here by calling the
    claim alone and never reaching the content write -- the exact
    window the audit names."""

    def test_d3_claim_new_summary_path_leaves_zero_byte_page(self) -> None:
        kb = Kb(self.root)
        summarize._claim_new_summary_path(kb, "deadbeef", "New Title")

        zero_byte_pages = [
            p for p in (self.root / "wiki").glob("*.md") if p.stat().st_size == 0
        ]
        self.assertEqual(
            zero_byte_pages,
            [],
            "claim step left a permanent 0-byte page before any content "
            f"was written: {zero_byte_pages}",
        )


class D5NeighboursSwallowsRealErrors(_KbTestCase):
    """D5: `neighbours` wraps its whole body in
    `except sqlite3.OperationalError: return []`, on its own
    connection that skips every pragma `_connect` sets. `_nearest`
    already re-raises a genuine dimension mismatch correctly (only
    `_table_missing`'s narrow case is meant to read back as empty);
    `neighbours`'s outer catch throws that correctness away."""

    def test_d5_neighbours_raises_on_dimension_mismatch_instead_of_hiding_it(self) -> None:
        page = self._write_page("page-a", "Page A", kind="story")

        def vector_for(_text: str) -> list[float]:
            return [1.0, 0.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            kb = Kb(self.root)
            vectors.sweep(kb)

        # A stored embedding is always the table's own width in the
        # real write path (vec0 enforces it at INSERT time). The one
        # way a genuine width mismatch reaches `_nearest`'s MATCH
        # query, matching the audit's cited error text exactly, is a
        # corrupted stored row read back at the wrong width; simulate
        # that at the one seam `neighbours` itself calls, `_unpack`.
        with patch.object(vectors, "_unpack", return_value=[1.0, 0.0]):
            with self.assertRaises(
                sqlite3.OperationalError,
                msg="neighbours() returned [] on a dimension mismatch "
                "instead of raising it",
            ):
                vectors.neighbours(kb, page, "story")


class D7TableMissingMisfiresOnShadowTables(_KbTestCase):
    """D7: `_table_missing` matches any OperationalError whose message
    contains 'no such table', not only the absent-`pages`-table case
    it exists for. Dropping vec0's own `pages_chunks` shadow table
    (real corruption, `pages` itself still present and populated)
    raises 'no such table: main.pages_chunks', which the same
    substring check waves through as 'nothing embedded yet'."""

    def test_d7_nearest_does_not_return_empty_for_a_broken_shadow_table(self) -> None:
        self._write_page("page-a", "Page A", kind="story")

        def vector_for(_text: str) -> list[float]:
            return [1.0, 0.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            kb = Kb(self.root)
            vectors.sweep(kb)

        conn = self._open_vec_db(kb)
        self.addCleanup(conn.close)
        conn.execute("DROP TABLE pages_chunks")
        conn.commit()

        with self.assertRaises(
            sqlite3.OperationalError,
            msg="_nearest returned instead of raising when a vec0 shadow "
            "table is missing, even though pages/ itself still holds rows",
        ):
            vectors._nearest(conn, [1.0, 0.0, 0.0], 10, None)


class D10RollbackWindowDropsLiveRows(_KbTestCase):
    """D10: `sweep`/`run` compute `to_delete` from one glob in `_plan`,
    then delete every named row with no recheck. A page that existed
    all along, but whose row `_plan` still (wrongly, for whatever
    reason: a slow embed batch, a plan snapshot reused past its
    moment) named for deletion, must not lose its row out from under
    it. Driven with a monkeypatch on `_plan` per the brief: the point
    under test is the blind trust in `to_delete`, not how a stale
    `to_delete` entry could arise."""

    def test_d10_sweep_does_not_delete_row_for_a_page_that_still_exists(self) -> None:
        page = self._write_page("keep", "Keep")

        def vector_for(_text: str) -> list[float]:
            return [1.0, 0.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            kb = Kb(self.root)
            vectors.sweep(kb)

        self.assertTrue(page.is_file(), "test setup lost the page before the gate ran")

        with patch.object(vectors, "_plan", return_value=([], ["keep.md"], set())):
            vectors.sweep(kb)

        conn = self._open_vec_db(kb)
        try:
            row = conn.execute(
                "SELECT path FROM pages WHERE path = ?", ("keep.md",)
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(
            row,
            "sweep deleted the row for a page that still exists on disk, "
            "trusting a stale to_delete list with no recheck",
        )


if __name__ == "__main__":
    unittest.main()
