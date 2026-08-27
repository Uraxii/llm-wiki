"""What a plugin is, what it receives, and how it is found.

A plugin is a MODULE, not a class and not a config file. It declares
seven module-level constants and one module-level `collect` generator.
That is the entire contract, and the rest of the stack is unchanged by
whatever a plugin does inside it.

A plugin declares LOGICAL credential names only: `SECRETS` says what it
needs, never where a value lives or which variable backs it. Resolution
happens in the parent process, and the plugin receives masked `Secret`
handles through its context. `SECRETS = ()` is a complete declaration
for a plugin against a public source. See `agent_kb.secrets`.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from .row import Row
from .row import KINDS, PROVENANCES
from .secrets import Secret

__all__ = [
    "AuthExpired",
    "CollectContext",
    "Declaration",
    "Plugin",
    "discover",
    "read_declaration",
]

LOGGER = logging.getLogger(__name__)


class AuthExpired(Exception):
    """A credential was accepted once and is no longer being accepted.

    Raised by a vendor's shared `_client` on HTTP 401, and by nothing
    else. The caller re-resolves that one secret ONCE, bypassing any
    cached value so a rotation lands, and retries that ONE call. A
    second 401 is a plugin failure for this tick: the run continues,
    `last_ok_at` is not advanced, and the next tick is the retry.

    There is no OAuth refresh persistence: no refresh token is stored,
    nothing is written to disk, and re-resolution is the whole recovery.

    The message names the connection and the logical secret. It never
    carries a value, a header, or a URL with a token in its query
    string.
    """


@dataclass(frozen=True)
class Declaration:
    """A plugin's module-level constants, read once at discovery.

    Every field maps to a module-level name of the same, upper-cased,
    spelling: `NAME`, `CADENCE_SEC`, and so on.
    """

    name: str
    """Stable plugin name. It composes with a connection id as
    `<plugin_name>/<connection_id>` for `Row.source` and `source_state`.
    Unique across plugins. May not contain `/`, the source separator."""

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

    secrets: tuple[str, ...]
    """LOGICAL names of the credentials this plugin needs, such as
    `("api_token",)`. Logical means the plugin never learns where a
    value lives or which variable backed it. Each one is resolved in the
    parent before the plugin runs; one that resolves to neither
    injection form is a loud SKIP, not a failure, and never a partial
    run. `()` is a first-class, fully valid declaration."""

    settings: tuple[str, ...]
    """Logical names of the plain settings this plugin needs from its
    connection, such as `("account_id",)`. Plain means loggable and
    git-safe, and is why settings live in `connections.toml` while
    secrets do not. A name here may never also appear in `secrets`."""

    module: types.ModuleType
    """The imported plugin module, whose `collect` the runner calls."""


@dataclass(frozen=True)
class CollectContext:
    """What the runner hands a plugin. Read-only, no store access.

    A plugin CANNOT reach the store. It emits rows and the write path
    decides what happens to them, which is what keeps the mutator
    single.

    ONE connection's worth of context and no more: a plugin run sees the
    account it was pointed at, and has no way to name another.

    Settings and secrets stay SEPARATE, and are never flattened into one
    mapping. A settings value is a plain `str`, safe to log; a secret is
    a masked `Secret` handle that must be revealed at the point of use.
    Keeping them apart is what makes "never log a secret" a property of
    the types instead of a rule people remember.

    Members:
        secret: accessor defined in stage 2. Contract: returns the
            `Secret` handle for one declared logical name. Returns a
            `Secret`, NEVER a `str`, so a caller cannot get a value
            without saying `reveal`. Raises loudly, and does not skip,
            when `name` is not in `declaration.secrets`: an undeclared
            secret is a plugin bug, and a plugin reaching for a
            credential it never declared must not be quietly handed one.
        setting: accessor defined in stage 2. Contract: returns the
            plain `str` for one declared logical name from
            `declaration.settings`, and raises loudly on an undeclared
            name for the same reason.
    """

    run_started_at: str
    """ISO 8601 UTC, the run's start. Used as `observed_at` fallback so
    every row in one run agrees on "now"."""

    declaration: Declaration
    """This plugin's own declaration, so a plugin need not repeat its
    constants when building rows."""

    connection_id: str
    """Connection id composed with `declaration.name` into Row.source."""

    secrets: Mapping[str, Secret]
    """Masked handles by logical name, exactly the names in
    `declaration.secrets`. Empty for a plugin that declares none. Held
    as handles rather than values, so nothing is read until a `reveal`
    asks and a rotation between run start and call is free."""

    settings: Mapping[str, str]
    """Plain values by logical name, exactly the names in
    `declaration.settings`, taken from this connection."""

    secret: ClassVar[Callable[[CollectContext, str], Secret]]
    setting: ClassVar[Callable[[CollectContext, str], str]]


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
    SECRETS: tuple[str, ...]
    SETTINGS: tuple[str, ...]

    def collect(self, ctx: CollectContext) -> Iterator[Row]:
        """Yield every row this source can currently reach.

        A GENERATOR, not a list. The write path consumes it lazily and
        commits incrementally, so a connector that dies on page 9 still
        leaves pages 1 to 8 durably stored, and the run that follows
        re-pages from the first page for free.

        Paging is this function's private business. No cursor is
        persisted between runs.

        Preconditions:
            - Every name in `SECRETS` resolved, so `ctx.secret(name)`
              returns a handle for each of them.
            - Every name in `SETTINGS` is present on the connection.

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
            `CADENCE_SEC` is not positive, or a name appears in both
            `SECRETS` and `SETTINGS`.
    """
    name = _required(module, "NAME", str)
    cadence_sec = _required(module, "CADENCE_SEC", int)
    half_life_days = _required(module, "HALF_LIFE_DAYS", (float, int))
    kind = _required(module, "KIND", str)
    provenance = _required(module, "PROVENANCE", str)
    secrets = _required(module, "SECRETS", tuple)
    settings = _required(module, "SETTINGS", tuple)
    collect = getattr(module, "collect", None)
    if not callable(collect):
        raise ValueError("collect must be callable")
    if not name or "/" in name:
        raise ValueError("NAME must be non-empty and may not contain /")
    if cadence_sec <= 0:
        raise ValueError("CADENCE_SEC must be positive")
    if float(half_life_days) < 0:
        raise ValueError("HALF_LIFE_DAYS must be non-negative")
    if kind not in KINDS:
        raise ValueError("KIND is invalid")
    if provenance not in PROVENANCES:
        raise ValueError("PROVENANCE is invalid")
    if any(not isinstance(item, str) or not item for item in secrets):
        raise ValueError("SECRETS must contain non-empty strings")
    if any(not isinstance(item, str) or not item for item in settings):
        raise ValueError("SETTINGS must contain non-empty strings")
    # Settings riding the same AGENT_KB_<CONN>_<NAME> env template as
    # secrets is the CURRENT RECOMMENDATION for getting connection
    # settings to the child (see secrets.ENV_VAR_TEMPLATE), not a
    # settled three-way choice against re-reading connections.toml or
    # argv. This guard is what that template requires: the two name
    # spaces must stay disjoint or a setting could shadow a credential.
    # If the reviewer picks a different option, this guard comes out
    # along with the shared template.
    if set(secrets) & set(settings):
        raise ValueError("SECRETS and SETTINGS may not share a name")
    return Declaration(
        name=name,
        cadence_sec=cadence_sec,
        half_life_days=float(half_life_days),
        kind=kind,
        provenance=provenance,
        secrets=secrets,
        settings=settings,
        module=module,
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
    root = importlib.import_module(package)
    declarations: list[Declaration] = []
    seen: dict[str, str] = {}
    for module_info in pkgutil.walk_packages(root.__path__, f"{package}."):
        basename = module_info.name.rsplit(".", 1)[-1]
        if basename.startswith("_") or module_info.ispkg:
            continue
        try:
            module = importlib.import_module(module_info.name)
            declaration = read_declaration(module)
        except Exception as error:
            LOGGER.warning("skipping plugin %s: %s", module_info.name, error)
            continue
        if declaration.name in seen:
            raise ValueError(
                "duplicate plugin NAME "
                f"{declaration.name!r}: {seen[declaration.name]} and "
                f"{module_info.name}"
            )
        seen[declaration.name] = module_info.name
        declarations.append(declaration)
    return declarations


def _required(
    module: types.ModuleType,
    name: str,
    expected_type: type | tuple[type, ...],
) -> object:
    try:
        value = getattr(module, name)
    except AttributeError as error:
        raise ValueError(f"{name} is required") from error
    if not isinstance(value, expected_type):
        raise ValueError(f"{name} has wrong type")
    return value
