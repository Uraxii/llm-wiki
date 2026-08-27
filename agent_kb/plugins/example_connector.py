"""Worked example: one query against one connector.

FICTIONAL. `example_records` stands in for one endpoint of a system of
record reached over HTTP. Copy this file, change the declaration and the
two mapping functions, delete this docstring.

A plugin never resolves its own credentials or settings. It declares
LOGICAL names in `SECRETS` / `SETTINGS`, and the runner hands `collect` a
`CollectContext` carrying a masked `Secret` handle per secret name
(`ctx.secret(name)`, revealed only at the point of use) and a plain
`str` per setting name (`ctx.setting(name)`). The plugin does not know,
and cannot ask, which environment variable or file backed either one.

A plugin makes its own calls. There is no shared subprocess wrapper: a
vendor with more than one plugin gets a stdlib-only thin client at
`agent_kb/plugins/<vendor>/_client.py` holding auth and paging; a
single-plugin vendor may call directly from `collect`. Either way the
call itself, and everything below it, is stage 2/3 work. See
`agent_kb/secrets.py` for the credential contract this plugin follows.
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

SECRETS = ("api_token",)
"""LOGICAL names only. This module does not know, and cannot ask, where
the value lives or which variable backed it: the parent resolves it and
hands `collect` a masked handle through `ctx.secret("api_token")`. A
plugin against a public source declares `SECRETS = ()` instead."""

SETTINGS = ("scope",)
"""Logical names of the plain per-connection values this plugin needs.
Plain means loggable and git-safe, which is why they come from
`connections.toml` and a secret never does."""


# --- Collection ----------------------------------------------------------


def collect(ctx: CollectContext) -> Iterator[Row]:
    """Yield one Row per record, paging until the payload has no cursor.

    STAGE 1 STUB. Not implemented. The intended shape, for stage 2/3.
    This is a single-module plugin, so the call is made directly here,
    not through a sibling `_client.py` (that split is for a vendor with
    more than one plugin, per the module docstring above):

        secret = ctx.secret("api_token")
        scope = ctx.setting("scope")
        cursor = ""
        while True:
            payload = _call_api(secret, scope, cursor)
            for record in payload["result"]:
                yield to_row(record, ctx)
            cursor = payload["page"].get("next")
            if not cursor:
                break

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
    raise NotImplementedError("TODO: stage 2/3, see docstring")


def to_row(record: dict, ctx: CollectContext) -> Row:
    """Map one connector record to a Row.

    STAGE 1 STUB. Not implemented.

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
    raise NotImplementedError("TODO: stage 2/3, see docstring")


def summarize(record: dict) -> str:
    """One or two prose sentences describing this record.

    STAGE 1 STUB. Not implemented.

    Deterministic template, no model call: the distillation an agent
    reads first, cheap enough to run on every row of every collection.
    See open question 3 in the design doc before reaching for a model.

    Postcondition: mentions every identifier in the row's `ids`, so a
    keyword hit on the prose and a keyword hit on the raw agree.
    """
    raise NotImplementedError("TODO: stage 2/3, see docstring")
