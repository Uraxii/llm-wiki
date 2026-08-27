"""What `connections.toml` parses into.

A CONNECTION is one account, tenant or endpoint that a plugin can be
pointed at. One plugin plus one connection is one unit of work, and
`<plugin>/<connection_id>` is the `Row.source` that keeps two accounts
on the same plugin from colliding on the `(source, object_id)` index.

The file is parsed with stdlib `tomllib`. TOML because it is in the
standard library, unlike YAML, and because it takes comments, unlike
JSON. Nothing in this module parses anything.

The file is SAFE TO COMMIT. It carries no secret value in any form:
only plain settings, the kind of value that is fine in a log line, in a
diff and in a code review. A setting whose value should come from the
environment is written `${VAR}` and interpolated at load. Credentials
never appear here, not even as a path or a variable name; a plugin
declares them logically and they arrive through `agent_kb.secrets`.

Connection ids are the KEYS of the file's connection tables, so TOML
makes them unique for free, and that free uniqueness is exactly what
lets two connections on one plugin carry different credentials under
`secrets.ENV_VAR_TEMPLATE`.

A connection id must ALSO match `secrets.ID_CHARSET_PATTERN`
(`^[a-z0-9]+$`), which is the authority for this rule: lowercase
letters and digits only, no `-`, no `_`, no `.`. TOML itself is happy
to parse `[prod-eu]` or `[prod_eu]` as a table key, but this contract
is NOT, because `ENV_VAR_TEMPLATE` must stay injective across the
connection id and the logical name it is joined with. A deployer who
wants a "prod-eu" connection must write `[prodeu]`.

A connection naming a plugin that does not exist is a LOUD STARTUP
ERROR, not a skip. A typo that silently collects nothing is worse than
a run that refuses to start.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["CONNECTIONS_FILENAME", "ENV_REF_PATTERN", "Connection"]

# Sits beside the store root. One file, every connection.
CONNECTIONS_FILENAME = "connections.toml"

# `${VAR}` in a setting value, interpolated from the parent environment
# at load. Settings only: a secret is never expressed this way.
ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Connection:
    """One plugin pointed at one account, as configured on disk.

    Plain data throughout, and deliberately loggable in full: if a value
    in here would be dangerous to print, it belongs in a secret and not
    in this file.
    """

    plugin: str
    """`Declaration.name` of the plugin this connection configures. A
    name with no matching plugin is a startup error."""

    connection_id: str
    """This connection's id, unique across the whole file. Composes with
    `plugin` into `Row.source` as `<plugin>/<connection_id>`, and into
    every environment variable name this connection owns.

    MUST match `secrets.ID_CHARSET_PATTERN` (`^[a-z0-9]+$`): lowercase
    letters and digits only. TOML would accept `prod-eu` or `prod_eu` as
    a key; this contract does not, because the env var template needs
    the id to be injective. Write `prodeu` instead."""

    settings: Mapping[str, str] = field(default_factory=dict)
    """The plugin's declared `SETTINGS`, by logical name, with any
    `${VAR}` already interpolated. Values are strings so a setting read
    from the file and a setting read from the environment have one
    shape. Secrets are NEVER in here: they are a separate lookup that
    returns a `Secret` handle, never a `str`, and the two are never
    flattened into one mapping."""
