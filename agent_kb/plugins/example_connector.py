"""Worked example: one query against one connector.

FICTIONAL. `example-connector` is a stand-in for a thin read-only CLI
that reads a system of record, takes its credentials from environment
variables, prints JSON on stdout in machine-readable mode, and pages with
an opaque cursor. Copy this file, change the declaration and the two
mapping functions, delete this docstring.

Shape of the connector this wraps:

    example-connector records --scope SCOPE --json [--cursor CURSOR]

    stdout: {"result": [ {...}, ... ],
             "page": {"next": "<cursor>" | null}}
    stderr: diagnostics only, never parsed
    exit:   0 on success, non-zero on any error
"""
from __future__ import annotations

from collections.abc import Iterator

from ..plugin import CollectContext
from ..row import Row

# --- Declaration ---------------------------------------------------------
# Read once at discovery by `plugin.read_declaration`. These seven names
# are the entire contract's declarative half.

NAME = "example_records"
"""Unique across plugins. Becomes `Row.source`."""

CADENCE_SEC = 3600
"""This inventory changes hourly at most, so asking more often is waste."""

HALF_LIFE_DAYS = 7.0
"""How fast a record's usefulness decays once unconfirmed. Stamped on
every row; the read side may derive a sort scalar from it."""

KIND = "state"
"""Records are things that persist, so snapshots version one row. An
event plugin would set "event" and emit one rollup row per window."""

PROVENANCE = "observed"
"""Read off a system of record, not asserted by an agent."""

REQUIRED_ENV = ("EXAMPLE_API_TOKEN", "EXAMPLE_API_SCOPE")
"""Checked for presence before this module runs. The values are NEVER
read here: the subprocess inherits `os.environ` and the connector
resolves them itself, which is why no credential can reach a Row."""

CONNECTOR = "example-connector"
"""Bare executable name, resolved under `AGENT_KB_CONNECTORS`."""


# --- Collection ----------------------------------------------------------


def collect(ctx: CollectContext) -> Iterator[Row]:
    """Yield one Row per record, paging until the payload has no cursor.

    Paging is private to this function: no cursor survives the run, and
    the next run re-pages from the start. Upsert by (source, object_id)
    is what makes that redo free.

    Yields lazily so the write path commits incrementally. If page 9
    raises, pages 1 to 8 are already durably stored and `last_ok_at` is
    not advanced, so the next tick retries the whole thing.

    Postconditions:
        - every Row carries the connector's own identifiers verbatim
        - nothing is written anywhere by this function
    """
    raise NotImplementedError(
        "TODO: cursor = None; while True: payload = ctx.run_connector([...]); "
        "yield from (to_row(r, ctx) for r in payload['result']); "
        "cursor = payload['page']['next']; break when falsy"
    )


def to_row(record: dict, ctx: CollectContext) -> Row:
    """Map one connector record to a Row.

    The ONE place this connector's output shape is known. Everything
    downstream sees only `Row`.

    Invariant: `ids` and `raw` are copied VERBATIM. No `.lower()`, no
    `.strip()`, no zero-padding, no truncation, no reformatting of a
    digest or an identifier of any kind. `body` is the only derived
    field, and it may not contradict what it was derived from.

    Raises:
        KeyError: the record is missing an identifier this plugin
            promises. Better a loud plugin failure than a row whose join
            key was quietly invented.
    """
    raise NotImplementedError(
        "TODO: Row(source=NAME, object_id=record['id'], "
        "entity=f'example/record/{record[\"name\"]}', kind=KIND, "
        "provenance=PROVENANCE, body=summarize(record), "
        "ids={...verbatim...}, raw=record, "
        "observed_at=record.get('updated_at', ''), "
        "half_life_days=HALF_LIFE_DAYS, source_ref=...)"
    )


def summarize(record: dict) -> str:
    """One or two prose sentences describing this record.

    Deterministic template, no model call: the distillation an agent
    reads first, cheap enough to run on every row of every collection.
    See open question 3 in the design doc before reaching for a model.

    Postcondition: mentions every identifier in the row's `ids`, so a
    keyword hit on the prose and a keyword hit on the raw agree.
    """
    raise NotImplementedError("TODO: f-string over the record's named fields")
