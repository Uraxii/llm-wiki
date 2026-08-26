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

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from .plugin import Declaration
from .row import Row
from .vault import Vault

__all__ = ["IngestReport", "entity_key", "ingest", "rebuild", "render_entity"]

# Entity pages live under one prefix so `Vault.list` can walk exactly the
# pages a rebuild needs and nothing else.
ENTITY_PREFIX = "entities/"


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


def validate(row: Row, declaration: Declaration) -> None:
    """Raise unless `row` may be stored.

    Checks, in order:
        - `source` equals `declaration.name`. A plugin cannot write into
          another plugin's key space.
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
    raise NotImplementedError("TODO: guard clauses, one per check")


def ingest(
    connection: sqlite3.Connection,
    vault: Vault,
    declaration: Declaration,
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
    raise NotImplementedError(
        "TODO: loop rows, validate, stamp, hash, upsert, collect dirty "
        "entities, render each"
    )


def entity_key(entity: str) -> str:
    """Vault key for an entity page.

    `ENTITY_PREFIX` + the entity name + `.md`. Pure and total: the same
    entity always yields the same key, so a page is never orphaned by a
    rename that only happened in memory.
    """
    raise NotImplementedError("TODO: f-string, no path logic here")


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
    raise NotImplementedError(
        "TODO: SELECT rows WHERE entity, deterministic sort, "
        "frontmatter + bodies + fenced json blocks"
    )


def rebuild(connection: sqlite3.Connection, vault: Vault) -> int:
    """Rebuild `wiki_row` from the entity pages. Returns rows restored.

    The recovery path: drop the table, walk `vault.list(ENTITY_PREFIX)`,
    parse the fenced JSON blocks out of each page, re-insert. The FTS
    triggers repopulate the index as a side effect of the inserts.

    A page that fails to parse is skipped and reported, never fatal:
    aborting halfway would leave the freshly dropped table holding less
    than the pages do.
    """
    raise NotImplementedError(
        "TODO: DROP + ensure_schema, iterate pages, extract fences, "
        "json.loads, INSERT, skip-and-warn on bad page"
    )
