"""The one row shape, its hash, and the schema that holds it.

Every plugin emits `Row`s and nothing else. Every field name here appears
verbatim as a column in `wiki_row`, so this module and the DDL below are
the single definition of the shared schema.

Two fields carry the invariant the whole design rests on: `ids` and `raw`
are copied from the source VERBATIM. Distillation writes `body`; it may
never rewrite, normalise, trim, lowercase or truncate a join key.
"""
from __future__ import annotations

import sqlite3
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DDL",
    "KINDS",
    "PROVENANCES",
    "Row",
    "connect",
    "content_hash",
    "ensure_schema",
]

# A row is either a snapshot of a thing that persists, or a rollup of
# things that happened. Nothing else. Events are never stored as raw rows.
KINDS = ("state", "event")

# How the claim came to exist: read off a source, asserted by an agent, or
# settled as a decision. Confidence is deliberately NOT stored: trust is a
# function of the question, so the retrieving agent judges it.
PROVENANCES = ("observed", "asserted", "decided")

@dataclass(frozen=True)
class Row:
    """One atom of collected knowledge, keyed by (source, object_id).

    Successive collections of the same remote object are versions of ONE
    row, not new rows. `content_hash` decides whether a collection is a
    new version or just a freshness confirmation.

    Invariants (enforced by `write.ingest`, not by this dataclass):
        - `ids` values are the source's own identifiers, byte-for-byte.
        - `raw` is the connector payload as returned, unedited.
        - `body` is derived and may contradict neither of the above.
        - `content_hash` covers body + ids + raw and NO timestamp.
    """

    source: str
    """`<plugin_name>/<connection_id>`. Becomes `wiki_row.source`."""

    object_id: str
    """The source's own identifier for this object, VERBATIM.

    Never a hash of the content, never a page position, never a
    timestamp: it must be stable across collections or upsert breaks and
    every run inserts duplicates. For an event rollup it is the window
    identity, `<query-id>/<window-start>/<window-end>`.
    """

    entity: str
    """Name of the wiki page this row belongs to.

    Convention: `<source-kind>/<object-type>/<name>`. This is the join
    surface for the whole wiki, so it is a naming decision, not a label.
    """

    kind: str
    """One of `KINDS`."""

    provenance: str
    """One of `PROVENANCES`."""

    body: str
    """Prose. For `state`, a distillation of the object. For `event`, the
    rollup of the window. This is what a human or an agent reads."""

    ids: dict[str, str] = field(default_factory=dict)
    """Flat map of join keys, values VERBATIM from the source.

    Stored as JSON. All values must be `str`; a plugin that wants to
    preserve a number preserves its exact string form. See
    `agent_kb.identifiers.CANONICAL_ID_TYPES` for the names to prefer.
    """

    raw: dict = field(default_factory=dict)
    """The connector payload for this object, as returned. Stored as JSON
    and keyword-indexed. For an `event` row this is the TRANSFORM
    description (window, query, parameters, result count), never the log
    lines themselves."""

    observed_at: str = ""
    """ISO 8601 UTC. The source's own timestamp for the observation. The
    write path substitutes the run start when a plugin leaves it empty,
    because a source that reports no time is not evidence of no time."""

    half_life_days: float = 0.0
    """Declared by the plugin, carried on the row, never computed here.
    The read side may derive a decay scalar for sort order."""

    source_ref: str = ""
    """Addressable pointer back to where this came from: enough to
    re-fetch it. Postcondition: a reader can reproduce the observation
    from this string alone."""

    # ingested_at, as_of and content_hash are stamped by the write path,
    # never by a plugin, so they are not fields here.


def content_hash(row: Row) -> str:
    """Return the sha256 hex digest identifying this row's CONTENT.

    Covers `body`, `ids` and `raw` under a canonical JSON encoding
    (sorted keys, no insignificant whitespace). Deliberately excludes
    every timestamp, so a source that stamps a fresh fetch time on each
    response does not look like a change.

    Postcondition: two Rows with equal body/ids/raw hash equal,
    regardless of field ordering inside the dicts or of any timestamp.
    """
    payload = {"body": row.body, "ids": row.ids, "raw": row.raw}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


DDL = """
CREATE TABLE IF NOT EXISTS wiki_row (
    id             INTEGER PRIMARY KEY,
    source         TEXT    NOT NULL,
    object_id      TEXT    NOT NULL,
    entity         TEXT    NOT NULL,
    kind           TEXT    NOT NULL
                   CHECK (kind IN ('state', 'event')),
    provenance     TEXT    NOT NULL
                   CHECK (provenance IN ('observed', 'asserted', 'decided')),
    body           TEXT    NOT NULL,
    ids            TEXT    NOT NULL,
    raw            TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    observed_at    TEXT    NOT NULL,
    ingested_at    TEXT    NOT NULL,
    as_of          TEXT    NOT NULL,
    half_life_days REAL    NOT NULL,
    source_ref     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS wiki_row_object
    ON wiki_row (source, object_id);
CREATE INDEX IF NOT EXISTS wiki_row_entity ON wiki_row (entity);
CREATE INDEX IF NOT EXISTS wiki_row_freshness ON wiki_row (source, as_of);

CREATE TABLE IF NOT EXISTS identifiers (
    row_id   INTEGER NOT NULL REFERENCES wiki_row(id) ON DELETE CASCADE,
    id_type  TEXT NOT NULL,
    id_value TEXT NOT NULL,
    PRIMARY KEY (row_id, id_type, id_value)
);
CREATE INDEX IF NOT EXISTS identifiers_lookup
    ON identifiers (id_type, id_value);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_row_fts USING fts5(
    entity, object_id, body, ids, raw,
    content = 'wiki_row',
    content_rowid = 'id'
);

CREATE TRIGGER IF NOT EXISTS wiki_row_ai AFTER INSERT ON wiki_row BEGIN
    INSERT INTO wiki_row_fts (rowid, entity, object_id, body, ids, raw)
    VALUES (new.id, new.entity, new.object_id, new.body, new.ids, new.raw);
END;

CREATE TRIGGER IF NOT EXISTS wiki_row_ad AFTER DELETE ON wiki_row BEGIN
    INSERT INTO wiki_row_fts (wiki_row_fts, rowid, entity, object_id,
                              body, ids, raw)
    VALUES ('delete', old.id, old.entity, old.object_id, old.body,
            old.ids, old.raw);
END;

CREATE TRIGGER IF NOT EXISTS wiki_row_au AFTER UPDATE ON wiki_row BEGIN
    INSERT INTO wiki_row_fts (wiki_row_fts, rowid, entity, object_id,
                              body, ids, raw)
    VALUES ('delete', old.id, old.entity, old.object_id, old.body,
            old.ids, old.raw);
    INSERT INTO wiki_row_fts (rowid, entity, object_id, body, ids, raw)
    VALUES (new.id, new.entity, new.object_id, new.body, new.ids, new.raw);
END;

CREATE TABLE IF NOT EXISTS source_state (
    source         TEXT PRIMARY KEY,
    last_ok_at     TEXT,
    last_error     TEXT,
    last_error_at  TEXT,
    last_row_count INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open `wiki.db`, creating its parent directory if missing.

    Returns a connection with `row_factory` set to `sqlite3.Row` and
    foreign keys on. Callers own closing it.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Apply `DDL`. Idempotent: every statement is IF NOT EXISTS.

    Precondition: `connection` is open.
    Postcondition: `wiki_row`, its indexes, `wiki_row_fts`, the three sync
    triggers and `source_state` all exist.
    """
    connection.executescript(DDL)
