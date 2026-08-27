"""The entrypoint. `python3 -m agent_kb.collect`.

Invoked by a system timer, not by a daemon. There is no scheduler
process, no queue and no job table: the timer supplies the tick, and
`source_state.last_ok_at` supplies the per-source cadence. The timer
interval is the cadence FLOOR; each plugin decides its own real cadence.

Running this twice is safe and running it too often is cheap: a plugin
that is not due is a single row read.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from .plugin import Declaration
from .write import IngestReport

__all__ = ["build_parser", "is_due", "main", "run_plugin"]

# Store root, overridable so a scratch run cannot touch the real store.
HOME_ENV = "AGENT_KB_HOME"
DEFAULT_HOME = "~/.agent-kb"

# The store's two halves, both under the root.
VAULT_DIRNAME = "vault"
DB_FILENAME = "wiki.db"


def resolve_home(explicit: str | None) -> Path:
    """Store root: explicit value, else `$AGENT_KB_HOME`, else the
    default. Expands `~`. Does not create anything."""
    raise NotImplementedError("TODO: three-way resolve + expanduser")


def is_due(last_ok_at: str | None, cadence_sec: int, now: datetime) -> bool:
    """Whether a plugin's cadence has elapsed.

    True when `last_ok_at` is absent (never succeeded, so always due) or
    when `now - last_ok_at >= cadence_sec`. An unparseable stored
    timestamp is treated as absent: being due too early costs one
    connector call, being stuck never-due costs a silently stale source.
    """
    raise NotImplementedError("TODO: parse ISO, compare against cadence")


def run_plugin(
    connection: sqlite3.Connection,
    vault: object,
    declaration: Declaration,
    run_started_at: str,
) -> IngestReport | None:
    """Run one plugin end to end. Returns None if it was skipped.

    Isolation is the whole job of this function: every failure mode below
    is contained to this one plugin, and the run continues.

        - missing credentials  -> skip, record `last_error`, return None
        - connector non-zero, timeout, or unparseable output
                               -> record `last_error`, do NOT advance
                                  `last_ok_at`, return None
        - anything else raised -> same

    On success it advances `last_ok_at` to `run_started_at` and clears
    `last_error`. `last_ok_at` is set from the RUN START, not from
    completion, so a long run does not push the next tick out.

    Postcondition: rows the plugin yielded before a mid-stream failure
    remain stored. That is intentional and safe, because upsert makes the
    next run's redo free.

    DEFERRED TO STAGE 2: this signature takes no `connection_id` and
    `write.ingest` now requires one (see `write.py`). The per-connection
    loop, the secret/setting resolution into a `CollectContext`, and the
    exact `write.ingest` call shape are all stage 2 work.
    """
    raise NotImplementedError("TODO: stage 2, see docstring")


def record_error(
    connection: sqlite3.Connection, source: str, message: str, at: str
) -> None:
    """Store a plugin's failure in `source_state` without advancing
    `last_ok_at`, which is what makes the next tick the retry.

    Precondition: `message` is already redacted. Callers pass exception
    text, so the connector must never put a credential in an error
    message.
    """
    raise NotImplementedError(
        "TODO: UPSERT source_state last_error + last_error_at"
    )


def build_parser() -> argparse.ArgumentParser:
    """The CLI.

        collect [--home DIR] [--only NAME] [--force] [--rebuild]

    `--force` ignores cadence, `--only` runs one plugin, `--rebuild`
    reconstructs `wiki.db` from the vault pages and exits without
    collecting. None of them change what a scheduled run does.
    """
    raise NotImplementedError("TODO: argparse, four flags, no subcommands")


def main(argv: list[str] | None = None) -> int:
    """Run every due plugin once. Returns 0 unless the run could not
    start.

    Sequence: resolve home, open and ensure the schema, take the writer
    lock with `BEGIN IMMEDIATE` (a second concurrent collect gets
    `SQLITE_BUSY` and exits non-zero rather than interleaving), discover
    plugins, run the due ones, print one JSON summary.

    Exit codes: 0 the run happened, even if individual plugins failed,
    because a failed plugin is a recorded state and not a broken run;
    non-zero only when the run itself could not start (lock held, store
    unopenable, duplicate plugin name).
    """
    raise NotImplementedError(
        "TODO: parse, resolve_home, connect + ensure_schema, "
        "BEGIN IMMEDIATE, discover, loop run_plugin, print json summary"
    )


if __name__ == "__main__":
    raise SystemExit(main())
