"""Vectors: one vec0 table per embed model, kept current by `sweep`,
queried by `search` and by dedup's vector-neighbour seam."""

import io
import shutil
import sqlite3
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlite_vec  # noqa: E402

from llmwiki.core import render_frontmatter  # noqa: E402
from llmwiki.model import ModelTarget  # noqa: E402
from llmwiki import dedup, vectors  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402
from kb_config import config_toml  # noqa: E402

# A target matching the id `config_toml(..., {"embed": "embed-model"})`
# produces ("test:embed-model"), for tests that talk to db_path/_connect
# directly rather than through a config file.
EMBED_TARGET = ModelTarget("test", "http://unused", "embed-model", None, None, "file")


def _respond(vector_for, chat_reply="NONE\n"):
    """A `FakeEndpoint` respond function answering `/embeddings` from
    `vector_for(text)` and `/chat/completions` with a fixed reply.
    `chat_reply` defaults to a harmless NONE so an *unexpected* chat
    call never hangs a test; whether one happened is checked afterward
    against `fake.requests`, not by making the handler raise (raising
    inside the server thread risks a client-side timeout, not a fast
    test failure)."""

    def respond(path: str, body: dict) -> dict:
        if path == "/embeddings":
            data = [
                {"index": i, "embedding": vector_for(text)}
                for i, text in enumerate(body["input"])
            ]
            return {"data": data}
        if path == "/chat/completions":
            return {"choices": [{"message": {"content": chat_reply}}]}
        raise AssertionError(f"unexpected request path {path!r}")

    return respond


def _rows(db_path: Path) -> dict[str, tuple]:
    """Every row in `db_path`'s `pages` table, keyed by path. Read-only,
    driving the same public schema the module writes -- not a private
    helper of the module under test."""
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        return {
            row[0]: row
            for row in conn.execute("SELECT path, kind, file_hash, title FROM pages")
        }
    finally:
        conn.close()


class VectorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")

    def _write_config(self, url: str | None = None, extra: str = "") -> None:
        # `url=None` still writes a resolvable provider table: every
        # `url=None` caller only exercises a staleness or status path
        # that raises before any network call, so a placeholder url is
        # enough. resolve_target, unlike the old model_name, needs the
        # provider table to exist to answer "which target is this".
        (self.root / "config.toml").write_text(
            config_toml(
                url or "http://unused",
                {"summarize": "cheap", "embed": "embed-model"},
                extra=extra,
            )
        )

    def _write_page(self, name: str, title: str, kind: str = "note", body: str = "Body text.") -> Path:
        fields = {"kind": kind, "title": title}
        path = self.root / "wiki" / f"{name}.md"
        path.write_text(render_frontmatter(fields, body))
        return path

    # -- staleness / deletion --------------------------------------

    def test_sweep_embeds_once_then_makes_no_call_when_current(self) -> None:
        self._write_page("north", "North Page")
        self._write_page("south", "South Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            embedded = vectors.sweep(kb)
            self.assertEqual(embedded, 2)
            self.assertEqual(len(fake.requests), 1)  # one batch call

            embedded_again = vectors.sweep(kb)
            self.assertEqual(embedded_again, 0)
            self.assertEqual(len(fake.requests), 1)  # still one: nothing stale

    def test_sweep_reembeds_only_the_changed_page(self) -> None:
        north = self._write_page("north", "North Page")
        self._write_page("south", "South Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)

            north.write_text(render_frontmatter({"kind": "note", "title": "North Page 2"}, "New body."))
            embedded = vectors.sweep(kb)

        self.assertEqual(embedded, 1)
        self.assertEqual(len(fake.requests), 2)

    def test_frontmatter_only_change_does_not_reembed(self) -> None:
        """A field the embedding never reads (dedup's `story:`
        back-reference, standing in here) must not force a re-embed:
        the staleness key covers kind, title and embed text, never the
        whole file."""
        path = self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)
            self.assertEqual(len(fake.requests), 1)

            path.write_text(
                render_frontmatter(
                    {"kind": "note", "title": "North Page", "story": "some-story"},
                    "Body text.",
                )
            )
            embedded = vectors.sweep(kb)

        self.assertEqual(embedded, 0)
        self.assertEqual(len(fake.requests), 1)  # no new call at all

    def test_summary_field_change_reembeds(self) -> None:
        path = self.root / "wiki" / "north.md"
        path.write_text(
            render_frontmatter(
                {"kind": "summary", "title": "North Page", "summary": "Old summary."},
                "Body text.",
            )
        )

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)
            self.assertEqual(len(fake.requests), 1)

            path.write_text(
                render_frontmatter(
                    {"kind": "summary", "title": "North Page", "summary": "New summary."},
                    "Body text.",
                )
            )
            embedded = vectors.sweep(kb)

        self.assertEqual(embedded, 1)
        self.assertEqual(len(fake.requests), 2)

    def test_body_change_reembeds_when_no_summary_field(self) -> None:
        path = self._write_page("north", "North Page", body="Old body.")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)
            self.assertEqual(len(fake.requests), 1)

            path.write_text(
                render_frontmatter({"kind": "note", "title": "North Page"}, "New body.")
            )
            embedded = vectors.sweep(kb)

        self.assertEqual(embedded, 1)
        self.assertEqual(len(fake.requests), 2)

    def test_sweep_deletes_row_for_vanished_page(self) -> None:
        north = self._write_page("north", "North Page")
        self._write_page("south", "South Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)
            db = vectors.db_path(kb, EMBED_TARGET)
            self.assertEqual(set(_rows(db)), {"north.md", "south.md"})

            north.unlink()
            vectors.sweep(kb)
            self.assertEqual(set(_rows(db)), {"south.md"})

    # -- refusals -----------------------------------------------------

    def test_search_refuses_when_a_page_lacks_a_current_vector(self) -> None:
        self._write_page("north", "North Page")
        self._write_config(None)

        err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = vectors.search(self.root, "north", n=5)

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(
            "llmwiki: search: 1 page without a current vector; run embed first",
            err.getvalue(),
        )

    def test_search_refusal_pluralizes_two_or_more_pages(self) -> None:
        self._write_page("north", "North Page")
        self._write_page("south", "South Page")
        self._write_config(None)

        err = io.StringIO()
        with redirect_stderr(err):
            code = vectors.search(self.root, "north", n=5)

        self.assertEqual(code, 1)
        self.assertIn(
            "llmwiki: search: 2 pages without a current vector; run embed first",
            err.getvalue(),
        )

    # -- agent-kb-5ty: missing [models] embed named as cause, not "no vector"

    def test_status_names_missing_embed_config_instead_of_no_vector(self) -> None:
        """Nothing CAN be stored (no embed model configured) differs
        from nothing IS stored (stale); status must say which."""
        self._write_page("north", "North Page")
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\n')

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = vectors.status(self.root)

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(
            "llmwiki: status: missing [models].embed in config.toml",
            err.getvalue(),
        )

    def test_search_names_missing_embed_config_before_any_paid_call(self) -> None:
        """Same cause as the status case above; search must also name
        it, and never reach the paid query embed to find out."""
        self._write_page("north", "North Page")
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\n')

        def vector_for(_text):
            raise AssertionError("must not embed before the config check")

        with FakeEndpoint(_respond(vector_for)) as fake:
            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = vectors.search(self.root, "north", n=5)

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(
            "llmwiki: search: missing [models].embed in config.toml",
            err.getvalue(),
        )
        self.assertEqual(fake.requests, [])

    def test_embed_missing_config_key_named_and_exits_2(self) -> None:
        self._write_page("north", "North Page")
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\n')

        err = io.StringIO()
        with redirect_stderr(err):
            code = vectors.run(self.root, None)

        self.assertEqual(code, 2)
        self.assertIn("missing [models].embed in config.toml", err.getvalue())

    # -- agent-kb-yr0: a malformed [models].embed must fail loudly,
    # never read as "embed not configured".

    def test_embed_target_raises_on_malformed_embed_instead_of_config_error(self) -> None:
        """`_embed_target` used to catch any `ModelError`, including one
        from a malformed value, and hand it back as error text. A
        malformed value must now raise instead."""
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\nembed = 123\n')
        from llmwiki.core import Kb

        with self.assertRaises(vectors.ModelError) as ctx:
            vectors._embed_target(Kb(self.root))
        self.assertIn("[models].embed", str(ctx.exception))
        self.assertIn("not a string", str(ctx.exception))

    def test_embed_target_unset_embed_returns_none(self) -> None:
        """A genuinely unset [models].embed returns `None`; callers
        print the byte-identical NO_EMBED_MODEL text status and search
        print verbatim."""
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\n')
        from llmwiki.core import Kb

        self.assertIsNone(vectors._embed_target(Kb(self.root)))
        self.assertEqual(vectors.NO_EMBED_MODEL, "missing [models].embed in config.toml")

    def test_embed_run_raises_on_malformed_config_instead_of_exiting_2(self) -> None:
        """`run`'s own [models].embed check used to catch any
        `ModelError`, printing "missing" and exiting 2 for a malformed
        value too. It must now raise instead of misreporting the
        failure as a missing key."""
        self._write_page("north", "North Page")
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\nembed = 123\n')

        with self.assertRaises(vectors.ModelError) as ctx:
            vectors.run(self.root, None)
        self.assertIn("[models].embed", str(ctx.exception))
        self.assertIn("not a string", str(ctx.exception))

    # -- P2: planned count ------------------------------------------------

    def test_sweep_prints_planned_count_before_the_paid_call(self) -> None:
        """`sweep` is the only path `ingest` embeds through; it must
        disclose spend itself, same shape as summarize/dedup (decision
        .8)."""
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            out = io.StringIO()
            with redirect_stdout(out):
                embedded = vectors.sweep(Kb(self.root))

        self.assertEqual(embedded, 1)
        self.assertIn("embed: 1 planned", out.getvalue().splitlines())

    # -- P1: no pages table yet -------------------------------------------

    def test_neighbours_returns_empty_when_db_exists_with_no_pages_table(self) -> None:
        """A db file can exist (created by `_connect`) with no `pages`
        table when every candidate got filtered out of `stale` before
        `_ensure_table` ran: `embed <missing page>` on a fresh kb, then
        `dedup`. `neighbours` must return [], never raise."""
        digest = "c" * 64
        (self.root / "sources" / f"{digest}.md").write_text("summary source text")
        summary_path = self.root / "wiki" / "summary-new.md"
        summary_path.write_text(
            render_frontmatter(
                {"kind": "summary", "title": "Summary", "source": digest, "identifiers": []},
                "An abstract.",
            )
        )

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for, chat_reply="NONE\n")) as fake:
            (self.root / "config.toml").write_text(
                config_toml(
                    fake.url,
                    {"summarize": "cheap", "embed": "embed-model", "dedup": "judge"},
                )
            )
            from llmwiki.core import Kb

            kb = Kb(self.root)
            embed_out = io.StringIO()
            with redirect_stdout(embed_out):
                embed_code = vectors.run(self.root, [self.root / "wiki" / "nosuchpage.md"])
            self.assertEqual(embed_code, 0)
            self.assertTrue(vectors.db_path(kb, EMBED_TARGET).is_file())

            self.assertEqual(vectors.neighbours(kb, summary_path, "story"), [])

            dedup_out = io.StringIO()
            with redirect_stdout(dedup_out):
                dedup_code = dedup.run(self.root, [digest])

        self.assertEqual(dedup_code, 0)

    # -- P5: non-UTF-8 pages ------------------------------------------------

    def test_non_utf8_page_gets_a_vector_and_does_not_crash(self) -> None:
        """`wiki/` is the user's agent's directory; the CLI cannot
        control its bytes. A latin-1 page must not traceback `status`,
        `embed` or `search`, and must still get a vector so search's
        refusal has a way to clear."""
        bad = self.root / "wiki" / "bad.md"
        bad.write_bytes(b"---\ntitle: Caf\xe9\nkind: summary\n---\nlatin1 body\n")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            status_out = io.StringIO()
            with redirect_stdout(status_out):
                status_code = vectors.status(self.root)
            self.assertEqual(status_code, 0)

            embed_out = io.StringIO()
            with redirect_stdout(embed_out):
                embed_code = vectors.run(self.root, None)
            self.assertEqual(embed_code, 0)

            search_out = io.StringIO()
            with redirect_stdout(search_out):
                search_code = vectors.search(self.root, "cafe", n=5)
            self.assertEqual(search_code, 0)

            db = vectors.db_path(Kb(self.root), EMBED_TARGET)

        self.assertIn("bad.md", _rows(db))

    # -- P6: _ensure_table dimension guard (agent-kb-1xm) ------------------

    def test_ensure_table_creates_fresh_table_without_raising(self) -> None:
        from llmwiki.core import Kb

        kb = Kb(self.root)
        db = vectors.db_path(kb, EMBED_TARGET)
        conn = vectors._connect(kb, EMBED_TARGET)
        try:
            vectors._ensure_table(conn, 3, db)  # must not raise
        finally:
            conn.close()

    def test_ensure_table_same_dimension_is_a_noop(self) -> None:
        from llmwiki.core import Kb

        kb = Kb(self.root)
        db = vectors.db_path(kb, EMBED_TARGET)
        conn = vectors._connect(kb, EMBED_TARGET)
        try:
            vectors._ensure_table(conn, 3, db)
            vectors._ensure_table(conn, 3, db)  # existing table, must not raise
        finally:
            conn.close()

    def test_ensure_table_dimension_mismatch_names_path_and_remedy(self) -> None:
        """The raw sqlite3 error names WHAT went wrong (a dimension
        mismatch); this proves the raised error also names WHAT TO
        DO: delete the db file and re-run embed."""
        from llmwiki.core import Kb

        kb = Kb(self.root)
        db = vectors.db_path(kb, EMBED_TARGET)
        conn = vectors._connect(kb, EMBED_TARGET)
        try:
            vectors._ensure_table(conn, 3, db)
            with self.assertRaises(ValueError) as ctx:
                vectors._ensure_table(conn, 4, db)
        finally:
            conn.close()

        message = str(ctx.exception)
        self.assertIn(str(db), message)
        self.assertIn("delete", message.lower())
        self.assertIn("embed", message.lower())

    # -- status ---------------------------------------------------------

    def test_status_lists_unvectored_pages_and_unsummarized_sources(self) -> None:
        self._write_page("north", "North Page")
        digest = "a" * 64
        (self.root / "sources" / f"{digest}.md").write_text("orphan source")
        self._write_config(None)

        out = io.StringIO()
        with redirect_stdout(out):
            code = vectors.status(self.root)

        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertIn("north.md\tno vector", lines)
        self.assertIn(f"{digest}\tno summary", lines)

    def test_status_clears_once_embedded_and_summarized(self) -> None:
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            vectors.sweep(Kb(self.root))

        out = io.StringIO()
        with redirect_stdout(out):
            code = vectors.status(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")

    # -- search: order and kind pre-filter -----------------------------

    def test_search_returns_top_k_in_similarity_order(self) -> None:
        self._write_page("va", "Alpha VA")
        self._write_page("vb", "Alpha VB")
        self._write_page("vc", "Alpha VC")
        self._write_page("vd", "Alpha VD")

        table = {
            "Alpha VA": [1.0, 0.0],
            "Alpha VB": [0.8, 0.6],
            "Alpha VC": [0.0, 1.0],
            "Alpha VD": [-1.0, 0.0],
            "query": [1.0, 0.0],
        }

        def vector_for(text: str) -> list[float]:
            for marker, vec in table.items():
                if marker in text:
                    return vec
            raise AssertionError(f"no vector fixture for {text!r}")

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            vectors.sweep(Kb(self.root))

            out = io.StringIO()
            with redirect_stdout(out):
                code = vectors.search(self.root, "query", n=3)

        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        order = [line.split("\t")[1] for line in lines]
        self.assertEqual(order, ["va.md", "vb.md", "vc.md"])  # vd excluded, least similar

    def test_kind_is_a_true_prefilter_not_a_lossy_postfilter(self) -> None:
        self._write_page("story-a", "MARK Story A", kind="story")
        self._write_page("story-b", "MARK Story B", kind="story")
        self._write_page("summary-a", "MARK Summary A", kind="summary")
        self._write_page("summary-b", "MARK Summary B", kind="summary")

        table = {
            "MARK Story A": [1.0, 0.0],
            "MARK Story B": [0.9962, 0.0872],
            "MARK Summary A": [0.7071, 0.7071],
            "MARK Summary B": [0.6428, 0.7660],
            "query": [1.0, 0.0],
        }

        def vector_for(text: str) -> list[float]:
            for marker, vec in table.items():
                if marker in text:
                    return vec
            raise AssertionError(f"no vector fixture for {text!r}")

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            vectors.sweep(Kb(self.root))

            out = io.StringIO()
            with redirect_stdout(out):
                code = vectors.search(self.root, "query", n=2, kind="summary")

        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        # A post-filter (kind outside vec0) would let the two nearer
        # story pages consume the k=2 budget and return zero summaries.
        self.assertEqual(len(lines), 2)
        names = {line.split("\t")[1] for line in lines}
        self.assertEqual(names, {"summary-a.md", "summary-b.md"})

    def test_kind_matching_nothing_returns_no_rows(self) -> None:
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            vectors.sweep(Kb(self.root))

            out = io.StringIO()
            with redirect_stdout(out):
                code = vectors.search(self.root, "north", n=5, kind="nothing-matches-this")

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")

    def test_search_on_empty_wiki_returns_no_rows_no_refusal(self) -> None:
        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            out = io.StringIO()
            with redirect_stdout(out):
                code = vectors.search(self.root, "anything", n=5)

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")

    def test_search_prints_unsummarized_warning_before_endpoint_failure(self) -> None:
        """Pre-split, `search` computed and printed the unsummarized
        count BEFORE calling embed, so a dead endpoint still produced
        both stderr lines. The split moved that count inside `rank`,
        where an embed failure raised it away unprinted; this pins the
        line back."""
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            vectors.sweep(Kb(self.root))  # north.md now has a current vector

        # one source with no summary page; the endpoint above is dead now
        (self.root / "sources" / ("a" * 64 + ".md")).write_text("orphan source")

        err = io.StringIO()
        with redirect_stderr(err):
            code = vectors.search(self.root, "anything", n=5)

        self.assertEqual(code, 2)
        lines = err.getvalue().splitlines()
        self.assertEqual(lines[0], "llmwiki: search: 1 sources without a summary page")
        self.assertTrue(lines[1].startswith("llmwiki: search: request to"))

    # -- rank: phase-03 split -----------------------------------------

    def test_rank_makes_exactly_one_embedding_call_for_the_query(self) -> None:
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)  # one call, embedding north.md
            self.assertEqual(len(fake.requests), 1)

            vectors.rank(kb, "query", n=5)
            self.assertEqual(len(fake.requests), 2)  # exactly one more

    def test_rank_checks_staleness_before_embedding(self) -> None:
        """Reversing check-before-embed would cost a paid call on every
        503; only reading the endpoint's call count back after the
        raise catches that."""
        self._write_page("north", "North Page")

        def vector_for(_text):
            raise AssertionError("must not embed before the staleness check")

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)  # never swept: north.md is stale
            with self.assertRaises(vectors.StaleVectors) as ctx:
                vectors.rank(kb, "query", n=5)

        self.assertEqual(ctx.exception.missing, 1)
        self.assertEqual(fake.requests, [])

    def test_rank_raises_no_embed_model_when_embed_is_unset(self) -> None:
        self._write_page("north", "North Page")
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\n')
        from llmwiki.core import Kb

        with self.assertRaises(vectors.NoEmbedModel) as ctx:
            vectors.rank(Kb(self.root), "query", n=5)

        self.assertEqual(ctx.exception.missing, 1)

    def test_rank_raises_no_embed_model_on_an_empty_wiki(self) -> None:
        """An empty wiki has no stale pages, so the model check must
        run before the staleness check or rank falls through to a
        paid embed call with no model configured (the defect this
        test pins)."""
        (self.root / "config.toml").write_text('[models]\nsummarize = "cheap"\n')
        from llmwiki.core import Kb

        with self.assertRaises(vectors.NoEmbedModel) as ctx:
            vectors.rank(Kb(self.root), "query", n=5)

        self.assertEqual(ctx.exception.missing, 0)

    def test_rank_propagates_model_error_from_the_endpoint(self) -> None:
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)  # not stale, so rank reaches the query embed

        with FakeEndpoint(_respond(vector_for), status=500) as broken:
            self._write_config(broken.url)
            with self.assertRaises(vectors.ModelError):
                vectors.rank(kb, "query", n=5)

    def test_rank_skips_a_page_unlinked_since_its_embed(self) -> None:
        north = self._write_page("north", "North Page")
        self._write_page("south", "South Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)
            north.unlink()  # row survives; sweep never ran again to drop it

            ranking = vectors.rank(kb, "query", n=5)

        self.assertEqual([hit.name for hit in ranking.hits], ["south.md"])

    def test_rank_score_is_unrounded(self) -> None:
        self._write_page("va", "Alpha VA")

        table = {"Alpha VA": [1.0, 1.0], "query": [1.0, 2.0]}

        def vector_for(text: str) -> list[float]:
            for marker, vec in table.items():
                if marker in text:
                    return vec
            raise AssertionError(f"no vector fixture for {text!r}")

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)
            ranking = vectors.rank(kb, "query", n=1)

        self.assertEqual(len(ranking.hits), 1)
        score = ranking.hits[0].score
        self.assertNotEqual(score, round(score, 2))  # not truncated to 2dp

    def test_rank_plans_exactly_once_and_opens_one_connection(self) -> None:
        self._write_page("north", "North Page")

        def vector_for(_text):
            return [1.0, 0.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)

            plan_calls = []
            real_plan = vectors._plan

            def counting_plan(*args, **kwargs):
                plan_calls.append(1)
                return real_plan(*args, **kwargs)

            connect_calls = []
            real_connect = vectors._connect

            def counting_connect(*args, **kwargs):
                connect_calls.append(1)
                return real_connect(*args, **kwargs)

            original_plan, original_connect = vectors._plan, vectors._connect
            vectors._plan = counting_plan
            vectors._connect = counting_connect
            try:
                vectors.rank(kb, "query", n=5)
            finally:
                vectors._plan = original_plan
                vectors._connect = original_connect

        self.assertEqual(plan_calls, [1])
        self.assertEqual(connect_calls, [1])


class DedupVectorSeamTest(unittest.TestCase):
    """The live defect fix: a vector neighbour with zero shared
    identifiers must survive `candidates()` and reach `judge`, but only
    when a dedup model is configured (the gate)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / ".kb"
        for sub in ("wiki", "sources"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "log.md").write_text("# log\n")

    def _write_config(self, url: str, with_dedup_model: bool) -> None:
        models = {"summarize": "cheap", "embed": "embed-model"}
        if with_dedup_model:
            models["dedup"] = "judge"
        (self.root / "config.toml").write_text(
            config_toml(url, models, extra="[identifiers.tag]\n")
        )

    def _seed(self) -> tuple[str, Path]:
        story_path = self.root / "wiki" / "story-other.md"
        story_path.write_text(
            render_frontmatter(
                {"kind": "story", "title": "MARKER Story", "identifiers": ["tag:zzz"], "members": []},
                "An old story body.",
            )
        )
        digest = "b" * 64
        (self.root / "sources" / f"{digest}.md").write_text("summary source text")
        summary_path = self.root / "wiki" / "summary-new.md"
        summary_path.write_text(
            render_frontmatter(
                {
                    "kind": "summary",
                    "title": "MARKER Summary",
                    "source": digest,
                    "identifiers": ["tag:unrelated"],
                },
                "A fresh abstract.",
            )
        )
        return digest, story_path

    def _fields(self, path: Path) -> dict:
        from llmwiki.core import parse_frontmatter

        return parse_frontmatter(path.read_text())[0]

    def test_vector_only_candidate_joins_when_dedup_model_configured(self) -> None:
        digest, _story_path = self._seed()

        def vector_for(text: str) -> list[float]:
            return [1.0, 0.0] if "MARKER" in text else [0.0, 1.0]

        with FakeEndpoint(_respond(vector_for, chat_reply="story-other\n")) as fake:
            self._write_config(fake.url, with_dedup_model=True)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)  # both pages get the same MARKER vector: similarity 1.0

            out = io.StringIO()
            with redirect_stdout(out):
                code = dedup.run(self.root, [digest])

        self.assertEqual(code, 0)
        # identifiers share nothing ("zzz" vs "unrelated"); only the
        # vector seam can have produced this join.
        self.assertEqual(self._fields(self.root / "wiki" / "summary-new.md")["story"], "story-other")
        self.assertIn(digest, self._fields(self.root / "wiki" / "story-other.md")["members"])

    def test_gate_blocks_vector_candidate_with_no_dedup_model(self) -> None:
        digest, _story_path = self._seed()

        def vector_for(text: str) -> list[float]:
            return [1.0, 0.0] if "MARKER" in text else [0.0, 1.0]

        with FakeEndpoint(_respond(vector_for)) as fake:
            self._write_config(fake.url, with_dedup_model=False)
            from llmwiki.core import Kb

            kb = Kb(self.root)
            vectors.sweep(kb)

            out = io.StringIO()
            with redirect_stdout(out):
                code = dedup.run(self.root, [digest])

        self.assertEqual(code, 0)
        # No dedup model: the gate keeps the seam off, exactly today's
        # deterministic-fallback behaviour -- a singleton, not a join.
        self.assertNotEqual(self._fields(self.root / "wiki" / "summary-new.md")["story"], "story-other")
        self.assertNotIn(digest, self._fields(self.root / "wiki" / "story-other.md")["members"])
        self.assertFalse(any(r.path == "/chat/completions" for r in fake.requests))


class TwoProvidersSameModelNameTest(unittest.TestCase):
    """agent-kb-9i7: `db_path` keys on `target.id`, the full
    `provider:model` string, so two providers serving a model of the
    same bare name never share one vectors/*.sqlite file."""

    def test_db_path_differs_by_provider(self) -> None:
        from llmwiki.core import Kb

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        kb_root = root / ".kb"
        for sub in ("wiki", "sources"):
            (kb_root / sub).mkdir(parents=True)
        (kb_root / "log.md").write_text("# log\n")
        (kb_root / "config.toml").write_text(
            "[models]\n"
            'summarize = "hosted:cheap"\n\n'
            '[providers.hosted]\n'
            'url = "http://unused-a"\n\n'
            '[providers.desktop]\n'
            'url = "http://unused-b"\n'
        )
        kb = Kb(kb_root)

        hosted = ModelTarget("hosted", "http://unused-a", "same-name", None, None, "file")
        desktop = ModelTarget("desktop", "http://unused-b", "same-name", None, None, "file")

        self.assertNotEqual(vectors.db_path(kb, hosted), vectors.db_path(kb, desktop))
        self.assertEqual(
            vectors.db_path(kb, hosted).name, "hosted--same-name.sqlite"
        )
        self.assertEqual(
            vectors.db_path(kb, desktop).name, "desktop--same-name.sqlite"
        )


if __name__ == "__main__":
    unittest.main()
