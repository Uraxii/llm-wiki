"""One vec0 table per embed model: a current vector per wiki page,
kept in sync by `sweep`, queried by `search` and by dedup's vector
neighbour seam (`neighbours`).

One table holds `path`, `kind`, `file_hash`, `title` and the vector
together (not a joined side table) because vec0 applies `k` BEFORE an
outer SQL filter: a `kind` column outside vec0 would make `--kind` a
silently lossy post-filter instead of a true pre-filter.
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from llmwiki.core import (
    TIMESTAMP_FORMAT,
    Kb,
    append_log_entry,
    as_list,
    flatten,
    parse_frontmatter,
    read_page_text,
)
from llmwiki.lint import select_pages
from llmwiki.model import ModelError, ModelTarget, embed, resolve_target, step_is_configured

TOP_K = 10              # default -n for search
BODY_HEAD_CHARS = 2000  # body prefix embedded when a page has no summary field

# Explicit, not Python's implicit connect() default: a concurrent writer
# gets this long to finish before sqlite raises "database is locked".
BUSY_TIMEOUT_MS = 5000

# Minimum similarity for a dedup vector candidate. Measured over all
# 1653 page-to-page pairs in the arena corpus: cross-domain pairs, which
# cannot be the same story, top out at 0.3123 (p99 0.2483), while
# within-domain pairs run p50 0.3025 / p90 0.4446 / max 0.6922. 0.35
# excludes every certainly-unrelated pair with 0.04 of headroom.
NEIGHBOUR_FLOOR = 0.35

# There is no search cutoff by design: over 250 top-10 hits from 25
# queries on 58 pages, hand-relevant hits ran min 0.3079 / median 0.5216
# / max 0.8448 while non-relevant ran min 0.1863 / median 0.3311 / max
# 0.6093. The bands overlap from 0.3079 to 0.6093, so no threshold
# separates them. A 0.50 cutoff kept only 19 of 35 true positives and
# returned nothing for 8 of 25 queries. Relevance judgment is the
# user's agent's job, not this module's.


NO_EMBED_MODEL = "missing [models].embed in config.toml"


def slug(model_id: str) -> str:
    """`/` and `:` become `--`, so a model id is one filename."""
    return model_id.replace("/", "--").replace(":", "--")


def db_path(kb: Kb, target: ModelTarget) -> Path:
    """Creates no directory. Keyed on `target.id`, so two providers
    serving the same model name get separate databases."""
    return kb.vectors / f"{slug(target.id)}.sqlite"


def _embed_target(kb: Kb) -> ModelTarget | None:
    """The configured embed target, or `None` when `[models].embed` is
    unset. Callers print NO_EMBED_MODEL for the `None` case. A
    malformed id raises instead of reading as unset."""
    if not step_is_configured(kb.config, "embed"):
        return None
    return resolve_target(kb.config, "embed")


def _connect(kb: Kb, target: ModelTarget) -> sqlite3.Connection:
    kb.vectors.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(kb, target))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _table_missing(exc: sqlite3.OperationalError) -> bool:
    """True only when the top-level `pages` table itself is absent,
    matching sqlite3's own message for that case exactly ('no such
    table: pages'). A locked database, a genuine dimension mismatch,
    or a broken vec0 shadow table (`pages_chunks`, ...; that error
    names the shadow table, e.g. 'no such table: main.pages_chunks',
    while `pages` itself is present and populated) raise the same
    exception type with a different message and must propagate, never
    read back as an empty result."""
    return str(exc) == "no such table: pages"


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def _ensure_table(conn: sqlite3.Connection, dim: int, db_file: Path) -> None:
    """Creates the `pages` table at `dim` when absent. When a table
    already exists at a different dimension, `CREATE ... IF NOT EXISTS`
    silently keeps the old one and a later INSERT dies with a raw
    sqlite3 dimension-mismatch error; read the stored dimension back
    from `sqlite_master` and raise an actionable error here instead."""
    conn.execute(
        # file_hash: historical name, now holds the embed-input hash
        # (kind + title + embed text), not a whole-file hash.
        "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING vec0("
        "path TEXT PRIMARY KEY, kind TEXT, +file_hash TEXT, +title TEXT, "
        f"embedding FLOAT[{dim}] distance_metric=cosine)"
    )
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'pages'"
    ).fetchone()[0]
    stored_dim = int(re.search(r"FLOAT\[(\d+)\]", ddl).group(1))
    if stored_dim != dim:
        raise ValueError(
            f"{db_file}: existing vector table is {stored_dim}-dimensional, "
            f"embed model now produces {dim}. Delete {db_file} and run "
            "embed again."
        )


def _stored_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT path, file_hash FROM pages").fetchall()
    except sqlite3.OperationalError as exc:
        if not _table_missing(exc):
            raise
        return {}  # table not created yet: nothing stored
    return dict(rows)


def _embed_text(fields: dict, body: str) -> str:
    """title + identifiers + the `summary` field if present, else the
    first BODY_HEAD_CHARS of the body (decision .1: not the whole file,
    because of cosine length bias)."""
    title = str(fields.get("title", ""))
    identifiers = " ".join(as_list(fields.get("identifiers")))
    summary_field = fields.get("summary")
    tail = str(summary_field) if summary_field else body[:BODY_HEAD_CHARS]
    return "\n".join([title, identifiers, tail])


def _embed_hash(kind: str, title: str, embed_text: str) -> str:
    """sha256 over exactly what a row stores and what the embedding
    depends on: `kind`, `title`, `embed_text`, NUL-joined so no field
    boundary is ambiguous. A frontmatter field the embedding never
    reads (dedup's `story:` back-reference, say) changes the file
    without changing this."""
    joined = "\x00".join([kind, title, embed_text])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _page_row(path: Path) -> tuple[str, str, str, str, str, str]:
    """Read `path` once: `(name, kind, embed_hash, title, embed_text,
    source)`. embed_hash covers `kind`, `title` and `embed_text`, the
    row's own semantic content, never the whole file; staleness is
    judged by this, never by mtime.

    An unparseable page is NOT skipped: skipping it would mean it never
    gets a vector, so `search`'s refusal would fire forever with no way
    to clear it. It gets kind="", title=path.name, and its embed text
    is the filename plus the first BODY_HEAD_CHARS of raw text.

    Uses `core.read_page_text`: the hash no longer needs the raw bytes
    themselves, only the same decoded text `parse_frontmatter` reads.
    """
    text = read_page_text(path)
    parsed = parse_frontmatter(text)
    if parsed is None:
        embed_text = f"{path.name}\n{text[:BODY_HEAD_CHARS]}"
        embed_hash = _embed_hash("", path.name, embed_text)
        return path.name, "", embed_hash, path.name, embed_text, ""

    fields, body = parsed
    kind = str(fields.get("kind", ""))
    title = str(fields.get("title") or path.name)
    source = str(fields.get("source", "")) if kind == "summary" else ""
    embed_text = _embed_text(fields, body)
    embed_hash = _embed_hash(kind, title, embed_text)
    return path.name, kind, embed_hash, title, embed_text, source


def _plan(kb: Kb, conn: sqlite3.Connection | None) -> tuple[list[Path], list[str], set[str]]:
    """ONE walk of wiki/*.md: pages needing an embed (new or embed_hash
    changed), row paths (filenames) to delete because their page is
    gone, and every source digest a summary page claims. `status`,
    `sweep` and `search`'s refusal all call this; no second walk
    anywhere. `conn=None` (no embed model configured) means nothing is
    stored, so every page is stale. A page unlinked between the glob
    inside `select_pages` and this read is skipped, not raised on."""
    pages = select_pages(kb.root, None)
    stored = _stored_hashes(conn) if conn is not None else {}
    stale: list[Path] = []
    seen_sources: set[str] = set()
    current_names: set[str] = set()
    for path in pages:
        try:
            name, _kind, embed_hash, _title, _text, source = _page_row(path)
        except FileNotFoundError:
            continue  # unlinked between select_pages and this read
        current_names.add(name)
        if stored.get(name) != embed_hash:
            stale.append(path)
        if source:
            seen_sources.add(source)
    to_delete = [name for name in stored if name not in current_names]
    return stale, to_delete, seen_sources


def _unsummarized(kb: Kb, seen_sources: set[str]) -> list[str]:
    all_sources = {
        p.stem for p in kb.sources.glob("*") if p.is_file() and p.suffix != ".toml"
    }
    return sorted(all_sources - seen_sources)


def _write_page(conn: sqlite3.Connection, row: tuple, vector: list[float]) -> None:
    """`INSERT OR REPLACE` does not work on vec0 (UNIQUE constraint
    failure): re-embedding a page is delete then insert, both in one
    transaction so a crash mid-write never leaves an orphaned half-row."""
    name, kind, embed_hash, title, _text, _source = row
    with conn:
        conn.execute("DELETE FROM pages WHERE path = ?", (name,))
        conn.execute(
            "INSERT INTO pages(path, kind, file_hash, title, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, kind, embed_hash, title, _pack(vector)),
        )


def _delete_page(conn: sqlite3.Connection, name: str) -> None:
    with conn:
        conn.execute("DELETE FROM pages WHERE path = ?", (name,))


def _nearest(
    conn: sqlite3.Connection, vector: list[float], n: int, kind: str | None
) -> list[tuple[float, str, str]]:
    """`(similarity, path, title)`, nearest first. `kind`, when given,
    is a TRUE pre-filter inside the vec0 MATCH: vec0 applies `k` before
    any outer filter, so a side-table filter would silently lose rows."""
    packed = _pack(vector)
    if kind is None:
        sql = "SELECT path, title, distance FROM pages WHERE embedding MATCH ? AND k = ? ORDER BY distance"
        params: tuple = (packed, n)
    else:
        sql = (
            "SELECT path, title, distance FROM pages WHERE embedding MATCH ? "
            "AND k = ? AND kind = ? ORDER BY distance"
        )
        params = (packed, n, kind)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if not _table_missing(exc):
            raise
        return []  # table not created yet
    return [(1.0 - distance, path, title) for path, title, distance in rows]


def sweep(kb: Kb, paths: list[Path] | None = None) -> int:
    """Bring `paths` (every wiki page when `None`) to a current vector,
    and delete rows for pages that no longer exist. Returns the number
    of pages embedded. ONE model.embed call for the whole stale batch,
    none at all when nothing is stale. Raises ModelError when
    [models] embed is unset. `to_delete` names rows as of `_plan`'s one
    walk; each is rechecked against disk immediately before its row is
    deleted, so a page that came back in between keeps its row."""
    target = resolve_target(kb.config, "embed")
    with contextlib.closing(_connect(kb, target)) as conn:
        stale, to_delete, _seen = _plan(kb, conn)
        if paths is not None:
            wanted = {p.resolve() for p in paths}
            stale = [p for p in stale if p.resolve() in wanted]
        # decision .8: every fan-out discloses its planned count before
        # any paid call. `run` (the `embed` verb) prints its own line
        # and never calls `sweep`, so this is the only place `ingest`
        # gets one.
        print(f"embed: {len(stale) + len(to_delete)} planned")
        for name in to_delete:
            if (kb.wiki / name).is_file():
                continue  # came back since the plan was made; keep its row
            _delete_page(conn, name)
        if not stale:
            return 0
        rows = [_page_row(p) for p in stale]
        vectors = embed(target, [row[4] for row in rows])
        _ensure_table(conn, len(vectors[0]), db_path(kb, target))
        for row, vector in zip(rows, vectors):
            _write_page(conn, row, vector)
        return len(stale)


def run(root: Path, paths: list[Path] | None) -> int:
    """CLI `embed`."""
    kb = Kb(root)
    if not step_is_configured(kb.config, "embed"):
        print(f"llmwiki: embed: {NO_EMBED_MODEL}", file=sys.stderr)
        return 2
    target = resolve_target(kb.config, "embed")

    embedded = 0
    deleted = 0
    with contextlib.closing(_connect(kb, target)) as conn:
        stale, to_delete, _seen = _plan(kb, conn)
        if paths is not None:
            wanted = {p.resolve() for p in paths}
            stale = [p for p in stale if p.resolve() in wanted]
        print(f"embed: {len(stale) + len(to_delete)} planned")

        for name in to_delete:
            if (kb.wiki / name).is_file():
                continue  # came back since the plan was made; keep its row
            _delete_page(conn, name)
            print(f"{name}\tdeleted")
            deleted += 1

        if stale:
            rows = [_page_row(p) for p in stale]
            try:
                vectors = embed(target, [row[4] for row in rows])
            except ModelError as exc:
                print(f"llmwiki: embed: {exc}", file=sys.stderr)
                return 1
            _ensure_table(conn, len(vectors[0]), db_path(kb, target))
            for row, vector in zip(rows, vectors):
                _write_page(conn, row, vector)
                print(f"{row[0]}\tembedded")
                embedded += 1

    append_log_entry(kb.log, "embed", f"{embedded} embedded, {deleted} deleted")
    return 0


def status(root: Path) -> int:
    """CLI `status`. Read-only, no model call."""
    kb = Kb(root)
    target = _embed_target(kb)
    conn = _connect(kb, target) if target is not None else None
    try:
        stale, _to_delete, seen = _plan(kb, conn)
    finally:
        if conn is not None:
            conn.close()
    if target is None:
        print(f"llmwiki: status: {NO_EMBED_MODEL}", file=sys.stderr)
    else:
        for path in stale:
            print(f"{path.name}\tno vector")
    for digest in _unsummarized(kb, seen):
        print(f"{digest}\tno summary")
    return 0


def search(root: Path, query: str, n: int = TOP_K, kind: str | None = None) -> int:
    """CLI `search`."""
    kb = Kb(root)
    try:
        ranking = rank(kb, query, n, kind)
    except NoEmbedModel as exc:
        print(f"llmwiki: search: {exc.config_error}", file=sys.stderr)
        return 1
    except StaleVectors as exc:
        word = "page" if exc.missing == 1 else "pages"
        print(f"llmwiki: search: {exc.missing} {word} without a current "
              "vector; run embed first", file=sys.stderr)
        return 1
    except ModelError as exc:
        if isinstance(exc, RankModelError) and exc.unsummarized:
            print(
                f"llmwiki: search: {exc.unsummarized} sources without a "
                "summary page",
                file=sys.stderr,
            )
        print(f"llmwiki: search: {exc}", file=sys.stderr)
        return 2

    if ranking.unsummarized:
        print(
            f"llmwiki: search: {ranking.unsummarized} sources without a summary page",
            file=sys.stderr,
        )
    for hit in ranking.hits:
        print(f"{hit.score:.2f}\t{hit.name}\t{hit.title}\t{hit.updated}\t{hit.size}")
    return 0


def neighbours(kb: Kb, path: Path, kind: str, n: int = TOP_K) -> list[tuple[float, Path]]:
    """The `n` pages of kind `kind` nearest the ALREADY-STORED vector of
    `path`. Makes NO model call: reads the stored vector by primary
    key. Returns [] when `path` has no row, the db file does not
    exist, or the `pages` table itself does not exist yet. Any other
    sqlite3.OperationalError (a genuine dimension mismatch, a broken
    vec0 shadow table, a locked database) propagates: the caller
    decides what a real failure means for its run, same contract as
    `_nearest`."""
    target = _embed_target(kb)
    if target is None:
        return []
    db = db_path(kb, target)
    if not db.is_file():
        return []
    conn = _connect(kb, target)
    try:
        row = conn.execute(
            "SELECT embedding FROM pages WHERE path = ?", (path.name,)
        ).fetchone()
        if row is None:
            return []
        vector = _unpack(row[0])
        hits = _nearest(conn, vector, n + 1, kind)
    except sqlite3.OperationalError as exc:
        if not _table_missing(exc):
            raise
        return []  # table not created yet
    finally:
        conn.close()

    results: list[tuple[float, Path]] = []
    for score, name, _title in hits:
        if name == path.name:
            continue
        if score < NEIGHBOUR_FLOOR:
            break  # sorted descending: everything after this is lower too
        results.append((score, kb.wiki / name))
        if len(results) == n:
            break
    return results


# `rank` and its return/error types sit here, after every earlier symbol,
# so adding them shifts no line number `docs/plans/**/*.md` already cites
# (phase-03-read-endpoints.md pins several against this file's pre-split
# layout). Do not move this block back up top on tidiness grounds; that
# reshuffle is exactly what scripts/check-plan-citations.py exists to
# catch, and fixing the citations is out of scope for this change.
from typing import NamedTuple  # noqa: E402


class Hit(NamedTuple):
    """One ranked page, as `rank` returns it."""

    score: float  # raw cosine similarity, unrounded; `search` formats to 2dp
    name: str  # the page filename, e.g. "cross-site-request-forgery.md"
    title: str  # already through core.flatten, as `search` prints it
    updated: str  # TIMESTAMP_FORMAT, from st_mtime, UTC
    size: int  # st_size


class Ranking(NamedTuple):
    """`rank`'s return: `hits` best first, at most `n`. `unsummarized`
    is the count of sources with no summary page; `search` warns on
    it, the service ignores it."""

    hits: list[Hit]
    unsummarized: int


class StaleVectors(Exception):
    """Raised by `rank` when one or more pages lack a current vector.
    `missing` is the count, needed verbatim by `search`'s warning line
    and by the service's 503 body."""

    def __init__(self, missing: int) -> None:
        super().__init__(f"{missing} pages without a current vector")
        self.missing = missing


class NoEmbedModel(Exception):
    """Raised by `rank` when `[models] embed` is unset: `_embed_target`
    then returns `None` before `_plan` ever runs. `missing` carries
    the same count `StaleVectors` would, for the service's response
    shape; `config_error` is the real `ModelError` text `search`
    prints instead of a staleness count."""

    def __init__(self, missing: int, config_error: str) -> None:
        super().__init__(config_error)
        self.missing, self.config_error = missing, config_error


def rank(kb: Kb, query: str, n: int = TOP_K, kind: str | None = None) -> Ranking:
    """At most `n` pages nearest `query`, best first. Fewer when a row
    survives an embed whose page has since been unlinked; those are
    skipped, exactly as `search` skips them today. Makes ONE paid
    embedding call, for the query, and only after the staleness check
    passes. Raises NoEmbedModel when [models] embed is unset, checked
    before staleness so an empty wiki still raises it instead of
    reaching the paid embed call below; `missing` is the stale count at
    that point, 0 on an empty wiki. Raises StaleVectors, carrying
    `missing`, the number of pages lacking a current vector, when an
    embed model IS set but pages are stale, because the route's 503
    body has to report it. Raises ModelError from the endpoint. ONE
    _plan walk and ONE connection, per _plan's own contract at
    vectors.py:180."""
    target = _embed_target(kb)
    conn = _connect(kb, target) if target is not None else None
    try:
        stale, _to_delete, seen = _plan(kb, conn)
        if target is None:
            raise NoEmbedModel(len(stale), NO_EMBED_MODEL)
        if stale:
            raise StaleVectors(len(stale))
        unsummarized = len(_unsummarized(kb, seen))

        try:
            (vector,) = embed(target, [query])
        except ModelError as exc:
            raise RankModelError(unsummarized, exc) from exc

        hits: list[Hit] = []
        for score, name, title in _nearest(conn, vector, n, kind):
            path = kb.wiki / name
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue  # row survives from an embed; page removed since
            updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                TIMESTAMP_FORMAT
            )
            hits.append(Hit(score, name, flatten(title), updated, stat.st_size))
        return Ranking(hits, unsummarized)
    finally:
        if conn is not None:
            conn.close()


class RankModelError(ModelError):
    """Raised by `rank` in place of the bare `ModelError` its own
    `embed` call raised, so `unsummarized`, already computed before
    that call, survives the failure instead of being lost with the
    stack. `str` stays byte identical to the wrapped error's message:
    `search` prints it unchanged, and the service's `except ModelError`
    still matches and reads the same text."""

    def __init__(self, unsummarized: int, cause: ModelError) -> None:
        super().__init__(str(cause))
        self.unsummarized = unsummarized
