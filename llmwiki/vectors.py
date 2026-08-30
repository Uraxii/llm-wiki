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
)
from llmwiki.lint import select_pages
from llmwiki.model import ModelError, embed, model_name

TOP_K = 10              # default -n for search
BODY_HEAD_CHARS = 2000  # body prefix embedded when a page has no summary field

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


def slug(model_id: str) -> str:
    """`/` and `:` become `--`, so a model id is one filename."""
    return model_id.replace("/", "--").replace(":", "--")


def db_path(kb: Kb, model_id: str) -> Path:
    """Creates no directory."""
    return kb.vectors / f"{slug(model_id)}.sqlite"


def _model_id(kb: Kb) -> str | None:
    """The configured `[models] embed` id, or `None` when it is unset."""
    try:
        return model_name(kb.config, "embed", None)
    except ModelError:
        return None


def _connect(kb: Kb, model_id: str) -> sqlite3.Connection:
    kb.vectors.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(kb, model_id))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


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
    except sqlite3.OperationalError:
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


def _page_row(path: Path) -> tuple[str, str, str, str, str, str]:
    """Read `path` once: `(name, kind, file_hash, title, embed_text,
    source)`. file_hash is sha256 of the WHOLE file, frontmatter
    included; staleness is judged by this, never by mtime.

    An unparseable page is NOT skipped: skipping it would mean it never
    gets a vector, so `search`'s refusal would fire forever with no way
    to clear it. It gets kind="", title=path.name, and its embed text
    is the filename plus the first BODY_HEAD_CHARS of raw text.
    """
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    parsed = parse_frontmatter(text)
    if parsed is None:
        embed_text = f"{path.name}\n{text[:BODY_HEAD_CHARS]}"
        return path.name, "", file_hash, path.name, embed_text, ""

    fields, body = parsed
    kind = str(fields.get("kind", ""))
    title = str(fields.get("title") or path.name)
    source = str(fields.get("source", "")) if kind == "summary" else ""
    return path.name, kind, file_hash, title, _embed_text(fields, body), source


def _plan(kb: Kb, conn: sqlite3.Connection | None) -> tuple[list[Path], list[str], set[str]]:
    """ONE walk of wiki/*.md: pages needing an embed (new or file_hash
    changed), row paths (filenames) to delete because their page is
    gone, and every source digest a summary page claims. `status`,
    `sweep` and `search`'s refusal all call this; no second walk
    anywhere. `conn=None` (no embed model configured) means nothing is
    stored, so every page is stale."""
    pages = select_pages(kb.root, None)
    stored = _stored_hashes(conn) if conn is not None else {}
    stale: list[Path] = []
    seen_sources: set[str] = set()
    current_names: set[str] = set()
    for path in pages:
        name, _kind, file_hash, _title, _text, source = _page_row(path)
        current_names.add(name)
        if stored.get(name) != file_hash:
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
    name, kind, file_hash, title, _text, _source = row
    with conn:
        conn.execute("DELETE FROM pages WHERE path = ?", (name,))
        conn.execute(
            "INSERT INTO pages(path, kind, file_hash, title, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, kind, file_hash, title, _pack(vector)),
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
    except sqlite3.OperationalError:
        return []  # table not created yet
    return [(1.0 - distance, path, title) for path, title, distance in rows]


def sweep(kb: Kb, paths: list[Path] | None = None) -> int:
    """Bring `paths` (every wiki page when `None`) to a current vector,
    and delete rows for pages that no longer exist. Returns the number
    of pages embedded. ONE model.embed call for the whole stale batch,
    none at all when nothing is stale. Raises ModelError when
    [models] embed is unset."""
    model_id = model_name(kb.config, "embed", None)
    with contextlib.closing(_connect(kb, model_id)) as conn:
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
            _delete_page(conn, name)
        if not stale:
            return 0
        rows = [_page_row(p) for p in stale]
        vectors = embed(kb.config, [row[4] for row in rows])
        _ensure_table(conn, len(vectors[0]), db_path(kb, model_id))
        for row, vector in zip(rows, vectors):
            _write_page(conn, row, vector)
        return len(stale)


def run(root: Path, paths: list[Path] | None) -> int:
    """CLI `embed`."""
    kb = Kb(root)
    try:
        model_id = model_name(kb.config, "embed", None)
    except ModelError:
        print("llmwiki: embed: missing [models].embed in config.toml", file=sys.stderr)
        return 2

    embedded = 0
    deleted = 0
    with contextlib.closing(_connect(kb, model_id)) as conn:
        stale, to_delete, _seen = _plan(kb, conn)
        if paths is not None:
            wanted = {p.resolve() for p in paths}
            stale = [p for p in stale if p.resolve() in wanted]
        print(f"embed: {len(stale) + len(to_delete)} planned")

        for name in to_delete:
            _delete_page(conn, name)
            print(f"{name}\tdeleted")
            deleted += 1

        if stale:
            rows = [_page_row(p) for p in stale]
            try:
                vectors = embed(kb.config, [row[4] for row in rows])
            except ModelError as exc:
                print(f"llmwiki: embed: {exc}", file=sys.stderr)
                return 1
            _ensure_table(conn, len(vectors[0]), db_path(kb, model_id))
            for row, vector in zip(rows, vectors):
                _write_page(conn, row, vector)
                print(f"{row[0]}\tembedded")
                embedded += 1

    append_log_entry(kb.log, "embed", f"{embedded} embedded, {deleted} deleted")
    return 0


def status(root: Path) -> int:
    """CLI `status`. Read-only, no model call."""
    kb = Kb(root)
    model_id = _model_id(kb)
    conn = _connect(kb, model_id) if model_id is not None else None
    try:
        stale, _to_delete, seen = _plan(kb, conn)
    finally:
        if conn is not None:
            conn.close()
    for path in stale:
        print(f"{path.name}\tno vector")
    for digest in _unsummarized(kb, seen):
        print(f"{digest}\tno summary")
    return 0


def search(root: Path, query: str, n: int = TOP_K, kind: str | None = None) -> int:
    """CLI `search`."""
    kb = Kb(root)
    model_id = _model_id(kb)
    conn = _connect(kb, model_id) if model_id is not None else None
    try:
        stale, _to_delete, seen = _plan(kb, conn)
        if stale:
            print(
                f"llmwiki: search: {len(stale)} pages without a current "
                "vector; run embed first",
                file=sys.stderr,
            )
            return 1
        missing = _unsummarized(kb, seen)
        if missing:
            print(
                f"llmwiki: search: {len(missing)} sources without a summary page",
                file=sys.stderr,
            )

        try:
            (vector,) = embed(kb.config, [query])
        except ModelError as exc:
            print(f"llmwiki: search: {exc}", file=sys.stderr)
            return 2

        for score, name, title in _nearest(conn, vector, n, kind):
            path = kb.wiki / name
            stat = path.stat()
            updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                TIMESTAMP_FORMAT
            )
            print(f"{score:.2f}\t{name}\t{flatten(title)}\t{updated}\t{stat.st_size}")
        return 0
    finally:
        if conn is not None:
            conn.close()


def neighbours(kb: Kb, path: Path, kind: str, n: int = TOP_K) -> list[tuple[float, Path]]:
    """The `n` pages of kind `kind` nearest the ALREADY-STORED vector of
    `path`. Makes NO model call: reads the stored vector by primary
    key. Returns [] when `path` has no row or the db file does not
    exist; never raises, so it can never fail an ingest."""
    model_id = _model_id(kb)
    if model_id is None:
        return []
    db = db_path(kb, model_id)
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        row = conn.execute(
            "SELECT embedding FROM pages WHERE path = ?", (path.name,)
        ).fetchone()
        if row is None:
            return []
        vector = _unpack(row[0])
        hits = _nearest(conn, vector, n + 1, kind)
    except sqlite3.OperationalError:
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
