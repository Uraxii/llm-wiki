"""The single mutator. The only code that changes the store.

Nothing else in this package writes to `wiki.db` or calls `Vault.put`.
That is not a convention, it is the reason the invariants below can be
stated once instead of audited everywhere:

    1. Raw identifiers survive VERBATIM. `ids` and `raw` are stored
       exactly as the source produced them. Distillation may write
       `body`; it may never touch a join key. A row that would lose a
       join key is REJECTED, not silently repaired.
    2. Every row is stamped: observed_at, ingested_at, as_of, kind,
       half_life_days, provenance, source_ref.
    3. Upsert is keyed by (source, object_id). An unchanged content hash
       bumps `as_of` and stores nothing else.
    4. A bad row is rejected and counted. It never aborts a run and never
       reaches the store.
    5. The entity page is a pure function of that entity's rows, so
       re-rendering is byte-for-byte stable.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from .plugin import Declaration
from .identifiers import normalise_identifier
from .row import KINDS, PROVENANCES, Row, content_hash, ensure_schema
from .vault import Vault

__all__ = ["IngestReport", "entity_key", "ingest", "rebuild", "render_entity"]

# Entity pages live under one prefix so `Vault.list` can walk exactly the
# pages a rebuild needs and nothing else.
ENTITY_PREFIX = "entities/"
LOGGER = logging.getLogger(__name__)
ROW_COLUMNS = (
    "source",
    "object_id",
    "entity",
    "kind",
    "provenance",
    "body",
    "ids",
    "raw",
    "content_hash",
    "observed_at",
    "ingested_at",
    "as_of",
    "half_life_days",
    "source_ref",
)


@dataclass(frozen=True)
class IngestReport:
    """What one plugin's ingest did. Returned, not logged, so the caller
    decides what is worth reporting."""

    source: str
    inserted: int
    """Rows never seen before under this (source, object_id)."""
    updated: int
    """Rows whose content hash changed: a new version."""
    refreshed: int
    """Rows whose content hash was unchanged: `as_of` bumped, nothing
    else written. In a healthy steady state this is most of them."""
    rejected: int
    """Rows that failed validation. Counted here so a source quietly
    emitting garbage shows up as a number rather than as silence."""


def validate(row: Row, declaration: Declaration, connection_id: str) -> None:
    """Raise unless `row` may be stored.

    Checks, in order:
        - `source` equals `<declaration.name>/<connection_id>`. A plugin
          cannot write into another plugin's connection key space.
        - `object_id` and `entity` are non-empty strings; `entity`
          matches the `<source-kind>/<object-type>/<name>` convention and
          contains no `..` component, because it becomes a vault key.
        - `kind` is in `row.KINDS` and equals `declaration.kind`.
        - `provenance` is in `row.PROVENANCES`.
        - every value in `ids` is a `str`. Numbers, None and nested
          structures are rejected rather than coerced, because coercion
          is exactly how a join key gets quietly rewritten.
        - `raw` is JSON-serialisable.
        - `source_ref` is non-empty.

    Raises:
        ValueError: any check fails, naming the field and the reason.
    """
    expected_source = f"{declaration.name}/{connection_id}"
    if row.source != expected_source:
        raise ValueError("source does not match declaration and connection")
    if not isinstance(row.object_id, str) or not row.object_id:
        raise ValueError("object_id must be a non-empty string")
    if not isinstance(row.entity, str) or not row.entity:
        raise ValueError("entity must be a non-empty string")
    entity_parts = row.entity.split("/")
    if len(entity_parts) < 3 or any(part in {"", ".", ".."} for part in entity_parts):
        raise ValueError("entity must match <source-kind>/<object-type>/<name>")
    if row.kind not in KINDS or row.kind != declaration.kind:
        raise ValueError("kind does not match declaration")
    if row.provenance not in PROVENANCES:
        raise ValueError("provenance is invalid")
    if row.provenance != declaration.provenance:
        raise ValueError("provenance does not match declaration")
    if not isinstance(row.ids, dict):
        raise ValueError("ids must be a dict")
    for key, value in row.ids.items():
        if not isinstance(key, str):
            raise ValueError("ids keys must be strings")
        if not isinstance(value, str):
            raise ValueError("ids values must be strings")
    _json_text(row.ids)
    _json_text(row.raw)
    if not isinstance(row.source_ref, str) or not row.source_ref:
        raise ValueError("source_ref must be a non-empty string")


def ingest(
    connection: sqlite3.Connection,
    vault: Vault,
    declaration: Declaration,
    connection_id: str,
    rows: Iterable[Row],
    run_started_at: str,
) -> IngestReport:
    """Consume a plugin's rows and write them. THE write path.

    Consumes `rows` lazily and commits incrementally, so an exception
    raised by the generator mid-stream leaves everything already yielded
    durably stored. That exception propagates to the caller, which is
    what stops `last_ok_at` from advancing so the next run retries.

    Per row:
        1. `validate`; on failure count it as rejected and continue.
        2. Stamp `observed_at` (the row's own, else `run_started_at`),
           `ingested_at`, `as_of` and `half_life_days` from the
           declaration.
        3. Compute `content_hash`.
        4. Look up the stored hash for (source, object_id).
           - absent  -> INSERT, count inserted
           - equal   -> UPDATE as_of ONLY, count refreshed, no re-render
           - differs -> UPDATE every column, count updated
        5. Remember the entity of any row that was inserted or updated.

    Then re-render exactly those entities' pages through `vault.put`.
    Refreshed-only entities are not re-rendered, because their bytes
    would be identical.

    Preconditions:
        - `ensure_schema` has run against `connection`.
        - `declaration` is the declaration of the plugin that produced
          `rows`.

    Postconditions:
        - Every stored row carries `ids` and `raw` byte-identical to what
          the plugin yielded.
        - Running this twice with the same rows changes nothing except
          `as_of`.

    Raises:
        Exception: whatever `rows` raises, after committing what it had
            already yielded.
    """
    source = f"{declaration.name}/{connection_id}"
    report = {"inserted": 0, "updated": 0, "refreshed": 0, "rejected": 0}
    for row in rows:
        try:
            validate(row, declaration, connection_id)
        except ValueError:
            report["rejected"] += 1
            continue
        existing = connection.execute(
            """
            SELECT id, content_hash FROM wiki_row
            WHERE source = ? AND object_id = ?
            """,
            (row.source, row.object_id),
        ).fetchone()
        row_hash = content_hash(row)
        observed_at = row.observed_at or run_started_at
        ids_text = _json_text(row.ids)
        raw_text = _json_text(row.raw)
        if existing is None:
            row_id = _insert_row(
                connection, row, row_hash, observed_at, run_started_at,
                ids_text, raw_text, declaration.half_life_days,
            )
            _replace_identifiers(connection, row_id, row.ids)
            report["inserted"] += 1
            vault.put(entity_key(row.entity), render_entity(connection, row.entity))
        elif existing["content_hash"] == row_hash:
            connection.execute(
                """
                UPDATE wiki_row SET as_of = ?
                WHERE source = ? AND object_id = ?
                """,
                (run_started_at, row.source, row.object_id),
            )
            report["refreshed"] += 1
        else:
            row_id = existing["id"]
            _update_row(
                connection, row_id, row, row_hash, observed_at,
                run_started_at, ids_text, raw_text,
                declaration.half_life_days,
            )
            _replace_identifiers(connection, row_id, row.ids)
            report["updated"] += 1
            vault.put(entity_key(row.entity), render_entity(connection, row.entity))
        connection.commit()
    return IngestReport(source=source, **report)


def entity_key(entity: str) -> str:
    """Vault key for an entity page.

    `ENTITY_PREFIX` + the entity name + `.md`. Pure and total: the same
    entity always yields the same key, so a page is never orphaned by a
    rename that only happened in memory.
    """
    return f"{ENTITY_PREFIX}{entity}.md"


def render_entity(connection: sqlite3.Connection, entity: str) -> bytes:
    """Render one entity page from its rows. Pure function of the rows.

    Layout: scalar frontmatter (entity, row count, newest `as_of`), then
    the prose `body` of each row newest first, then a `## Rows` section
    holding one fenced JSON block per row with the FULL row including
    `ids` and `raw` verbatim.

    That fenced block is what makes the markdown the durable record and
    `wiki.db` a rebuildable index rather than the only copy. See open
    question 2 in the design doc: if that call is reversed, this function
    drops the `## Rows` section and `rebuild` disappears with it.

    Postcondition: byte-for-byte deterministic. Rows sorted by
    (source, object_id), JSON keys sorted, no timestamp of rendering.
    """
    rows = connection.execute(
        """
        SELECT source, object_id, entity, kind, provenance, body, ids, raw,
               content_hash, observed_at, ingested_at, as_of,
               half_life_days, source_ref
        FROM wiki_row
        WHERE entity = ?
        ORDER BY source, object_id
        """,
        (entity,),
    ).fetchall()
    newest_as_of = max((row["as_of"] for row in rows), default="")
    lines = [
        "---",
        f"entity: {entity}",
        f"row_count: {len(rows)}",
        f"newest_as_of: {newest_as_of}",
        "---",
        "",
        f"# {entity}",
        "",
    ]
    for row in rows:
        lines.extend([row["body"], ""])
    lines.extend(["## Rows", ""])
    for row in rows:
        lines.extend([
            "```json",
            json.dumps(_row_payload(row), sort_keys=True, indent=2),
            "```",
            "",
        ])
    return "\n".join(lines).encode("utf-8")


def rebuild(connection: sqlite3.Connection, vault: Vault) -> int:
    """Rebuild `wiki_row` from the entity pages. Returns rows restored.

    The recovery path: drop the table, walk `vault.list(ENTITY_PREFIX)`,
    parse the fenced JSON blocks out of each page, re-insert. The FTS
    triggers repopulate the index as a side effect of the inserts.

    A page that fails to parse is skipped and reported, never fatal:
    aborting halfway would leave the freshly dropped table holding less
    than the pages do.
    """
    connection.executescript(
        """
        DROP TABLE IF EXISTS wiki_row_fts;
        DROP TABLE IF EXISTS identifiers;
        DROP TABLE IF EXISTS wiki_row;
        """
    )
    ensure_schema(connection)
    restored = 0
    for key in vault.list(ENTITY_PREFIX):
        try:
            blocks = _json_blocks(vault.get(key).decode("utf-8"))
            for block in blocks:
                row_id = _insert_payload(connection, block)
                ids = json.loads(block["ids"])
                _replace_identifiers(connection, row_id, ids)
                restored += 1
        except (KeyError, KeyError, TypeError, ValueError, sqlite3.Error) as error:
            LOGGER.warning("skipping unrebuildable page %s: %s", key, error)
    connection.commit()
    return restored


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _insert_row(
    connection: sqlite3.Connection,
    row: Row,
    row_hash: str,
    observed_at: str,
    run_started_at: str,
    ids_text: str,
    raw_text: str,
    half_life_days: float,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO wiki_row (
            source, object_id, entity, kind, provenance, body, ids, raw,
            content_hash, observed_at, ingested_at, as_of, half_life_days,
            source_ref
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.source, row.object_id, row.entity, row.kind, row.provenance,
            row.body, ids_text, raw_text, row_hash, observed_at,
            run_started_at, run_started_at, half_life_days, row.source_ref,
        ),
    )
    return int(cursor.lastrowid)


def _update_row(
    connection: sqlite3.Connection,
    row_id: int,
    row: Row,
    row_hash: str,
    observed_at: str,
    run_started_at: str,
    ids_text: str,
    raw_text: str,
    half_life_days: float,
) -> None:
    connection.execute(
        """
        UPDATE wiki_row
        SET source = ?, object_id = ?, entity = ?, kind = ?,
            provenance = ?, body = ?, ids = ?, raw = ?,
            content_hash = ?, observed_at = ?, ingested_at = ?,
            as_of = ?, half_life_days = ?, source_ref = ?
        WHERE id = ?
        """,
        (
            row.source, row.object_id, row.entity, row.kind, row.provenance,
            row.body, ids_text, raw_text, row_hash, observed_at,
            run_started_at, run_started_at, half_life_days, row.source_ref,
            row_id,
        ),
    )


def _replace_identifiers(
    connection: sqlite3.Connection,
    row_id: int,
    ids: dict[str, str],
) -> None:
    connection.execute("DELETE FROM identifiers WHERE row_id = ?", (row_id,))
    connection.executemany(
        """
        INSERT INTO identifiers (row_id, id_type, id_value)
        VALUES (?, ?, ?)
        """,
        [
            (row_id, id_type, normalise_identifier(id_type, value))
            for id_type, value in ids.items()
        ],
    )


def _row_payload(row: sqlite3.Row) -> dict[str, object]:
    payload = {column: row[column] for column in ROW_COLUMNS}
    payload["ids"] = json.loads(str(payload["ids"]))
    payload["raw"] = json.loads(str(payload["raw"]))
    return payload


def _json_blocks(markdown: str) -> list[dict[str, object]]:
    blocks = []
    in_block = False
    current: list[str] = []
    for line in markdown.splitlines():
        if line == "```json":
            in_block = True
            current = []
        elif line == "```" and in_block:
            blocks.append(json.loads("\n".join(current)))
            in_block = False
        elif in_block:
            current.append(line)
    return blocks


def _insert_payload(
    connection: sqlite3.Connection,
    payload: dict[str, object],
) -> int:
    ids_text = _json_text(payload["ids"])
    raw_text = _json_text(payload["raw"])
    values = {
        **payload,
        "ids": ids_text,
        "raw": raw_text,
    }
    cursor = connection.execute(
        """
        INSERT INTO wiki_row (
            source, object_id, entity, kind, provenance, body, ids, raw,
            content_hash, observed_at, ingested_at, as_of, half_life_days,
            source_ref
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(values[column] for column in ROW_COLUMNS),
    )
    return int(cursor.lastrowid)
