"""What a plugin is, what it receives, and how it is found.

A plugin is a MODULE, not a class and not a config file. It declares six
module-level constants and one module-level `collect` generator. That is
the entire contract, and the rest of the stack is unchanged by whatever a
plugin does inside it.

Credentials are environment variables. There is no secret store: the
connector CLIs already read their own credentials from the environment,
and the collector inherits `os.environ` into the subprocess so the
connector resolves them itself. No credential is ever read into a Row, a
log line, or the store.
"""
from __future__ import annotations

import types
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .row import Row

__all__ = [
    "CONNECTOR_TIMEOUT_SEC",
    "CollectContext",
    "Declaration",
    "Plugin",
    "discover",
    "read_declaration",
]

# One connector invocation may not hang a whole run. A page that cannot be
# fetched inside this budget is a plugin failure, and the next scheduled
# run is the retry.
CONNECTOR_TIMEOUT_SEC = 120.0

# Where connector executables live. Resolved from AGENT_KB_CONNECTORS.
CONNECTOR_DIR_ENV = "AGENT_KB_CONNECTORS"


@dataclass(frozen=True)
class Declaration:
    """A plugin's module-level constants, read once at discovery.

    Every field maps to a module-level name of the same, upper-cased,
    spelling: `NAME`, `CADENCE_SEC`, and so on.
    """

    name: str
    """Stable source name. Becomes `Row.source` and the `source_state`
    key. Unique across plugins: a duplicate is a hard error, because two
    plugins sharing a name would collide on (source, object_id)."""

    cadence_sec: int
    """Minimum seconds between successful runs. The timer interval is the
    floor; this is the plugin's own freshness requirement."""

    half_life_days: float
    """Stamped on every row this plugin emits. Declared per source
    because a DNS record and a vulnerability finding age differently."""

    kind: str
    """One of `row.KINDS`, applied to every row this plugin emits."""

    provenance: str
    """One of `row.PROVENANCES`, applied to every row this plugin
    emits."""

    required_env: tuple[str, ...]
    """Environment variable names the connector needs. Checked for
    presence and non-emptiness before the plugin runs; a missing one is a
    loud SKIP, not a failure, and never a partial run."""

    connector: str
    """Bare name of the connector executable, resolved under
    `AGENT_KB_CONNECTORS`. Never an absolute path, so the same plugin
    works on a machine that keeps its connectors elsewhere."""

    module: types.ModuleType
    """The imported plugin module, whose `collect` the runner calls."""


@dataclass(frozen=True)
class CollectContext:
    """What the runner hands a plugin. Read-only, no store access.

    A plugin CANNOT reach the store. It emits rows and the write path
    decides what happens to them, which is what keeps the mutator
    single.
    """

    run_started_at: str
    """ISO 8601 UTC, the run's start. Used as `observed_at` fallback so
    every row in one run agrees on "now"."""

    connector_dir: Path
    """Directory holding connector executables."""

    declaration: Declaration
    """This plugin's own declaration, so a plugin need not repeat its
    constants when building rows."""

    def run_connector(self, args: Sequence[str]) -> dict | list:
        """Invoke this plugin's connector once and return parsed JSON.

        The ONE place the collector shells out. `shell=False` with an
        argument list, never a command string. `os.environ` is inherited
        so the connector resolves its own credentials.

        Args:
            args: connector arguments, excluding the executable itself.
                Must request the connector's machine-readable output
                mode, because human-readable mode writes paging hints to
                stderr where they cannot be parsed reliably.

        Returns:
            The decoded stdout payload.

        Raises:
            FileNotFoundError: the connector is not under `connector_dir`.
            RuntimeError: non-zero exit, or stdout that is not JSON. Both
                are plugin failures, both mean the next run retries.
            subprocess.TimeoutExpired: exceeded CONNECTOR_TIMEOUT_SEC.

        Postcondition: nothing is written anywhere. This is a pure read.
        """
        raise NotImplementedError(
            "TODO: subprocess.run([exe, *args], capture_output=True, "
            "text=True, timeout=CONNECTOR_TIMEOUT_SEC), check exit, "
            "json.loads(stdout)"
        )


@runtime_checkable
class Plugin(Protocol):
    """Structural contract every plugin module satisfies.

    Declared as a Protocol over a MODULE: a plugin is a module with these
    module-level names, so `isinstance(module, Plugin)` is the whole
    admission check. Nothing subclasses anything.
    """

    NAME: str
    CADENCE_SEC: int
    HALF_LIFE_DAYS: float
    KIND: str
    PROVENANCE: str
    REQUIRED_ENV: tuple[str, ...]
    CONNECTOR: str

    def collect(self, ctx: CollectContext) -> Iterator[Row]:
        """Yield every row this source can currently reach.

        A GENERATOR, not a list. The write path consumes it lazily and
        commits incrementally, so a connector that dies on page 9 still
        leaves pages 1 to 8 durably stored, and the run that follows
        re-pages from the first page for free.

        Paging is this function's private business. No cursor is
        persisted between runs.

        Preconditions:
            - Every name in `REQUIRED_ENV` is set and non-empty.
            - `ctx.connector_dir` exists.

        Postconditions:
            - Every yielded Row carries `ids` and `raw` VERBATIM from the
              source. No normalisation of any join key.
            - `object_id` is stable across runs for the same object.
            - No credential appears in any yielded field.
            - Nothing is written anywhere by this function.

        Raises:
            Anything. The runner isolates it per plugin and leaves
            `last_ok_at` unadvanced so the next tick retries.
        """
        ...


def read_declaration(module: types.ModuleType) -> Declaration:
    """Validate one imported module's constants into a `Declaration`.

    Raises:
        ValueError: a constant is missing, has the wrong type, `KIND` is
            not in `row.KINDS`, `PROVENANCE` is not in `row.PROVENANCES`,
            `CADENCE_SEC` is not positive, or `CONNECTOR` looks like a
            path rather than a bare name.
    """
    raise NotImplementedError(
        "TODO: getattr each constant, type + domain check"
    )


def discover(package: str = "agent_kb.plugins") -> list[Declaration]:
    """Import every plugin module in `package` and return declarations.

    Modules whose name starts with `_` are skipped. A module that fails
    to import, or whose declaration is invalid, is logged and skipped:
    one broken plugin never stops the others from running.

    Raises:
        ValueError: two plugins declare the same `NAME`. This one IS
            fatal, because a duplicate source name silently merges two
            sources into one primary-key space.
    """
    raise NotImplementedError(
        "TODO: pkgutil.iter_modules + importlib.import_module, "
        "read_declaration each, reject duplicate names"
    )
