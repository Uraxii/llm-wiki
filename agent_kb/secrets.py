"""The credential contract: masked handles, and how a value gets in.

Secrets management belongs to the DEPLOYER. This package ships no secret
store, integrates with none, and names none. It defines only how an
injected value reaches one plugin run, and how that value is kept out of
everything else.

INJECTION. Two forms, first hit wins:

    <VAR>        the value itself, in the environment.
    <VAR>_FILE   a path to a file holding the value. The file is read in
                 the PARENT only, and EXACTLY ONE trailing newline is
                 stripped, so an editor that adds a final newline does
                 not change the credential.

Neither present means that plugin is SKIPPED loudly for this tick and
the run continues. There is no third form.

A `<VAR>_FILE` path that resolves to a file readable by group or other
is WARNED about, never failed on: the run is not the deployer's file
permission fence, but a leaky credential file is worth a loud line in
the log. Stage 2 work; no enforcement here.

NAMING. `<VAR>` is derived from the CONNECTION id and the plugin's own
logical name for the value, never from the plugin name:

    AGENT_KB_<CONNECTION_ID>_<LOGICAL_NAME>, upper cased

`connections.toml` key uniqueness only makes `CONNECTION_ID` unique; it
says nothing about `LOGICAL_NAME`, which a plugin author picks freely.
The template is injective only because BOTH segments are constrained by
`ID_CHARSET_PATTERN` below, which excludes the `_` separator and excludes
uppercase entirely. Excluding the separator stops one segment's
characters from bleeding into the other (`prod_a`+`token` colliding with
`prod`+`a_token`); excluding uppercase stops two differently-cased
connection ids (`prod`, `PROD`) from folding onto the same variable, since
`.upper()` cannot be undone. A plugin never learns which variable backed
its secret. The mapping function is not defined here.

ISOLATION. The parent resolves values and hands the child process a
HAND-BUILT environment: `RUNTIME_ENV_ALLOWLIST` plus that one
connection's own variables. `os.environ` is NEVER inherited. A resolved
value is never an argv element, never a Row field, never a log line,
never part of an exception message, and is never written to disk.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

__all__ = [
    "ENV_VAR_TEMPLATE",
    "FILE_SUFFIX",
    "FORBIDDEN_NAME_SUFFIX",
    "ID_CHARSET_PATTERN",
    "RUNTIME_ENV_ALLOWLIST",
    "SECRET_MASK",
    "Secret",
    "SecretUnavailable",
]

# What a Secret renders as, everywhere, always. Fixed and content-free:
# it names no plugin, no connection, no variable and no value, so a
# leaked render tells a reader nothing but that a secret was there.
SECRET_MASK = "<secret>"

# Appended to a variable name to name the FILE form instead of the value
# form. Precedent: Prometheus `*_file`, Grafana `GF_*__FILE`, and the
# official postgres and mysql images.
FILE_SUFFIX = "_FILE"

# Every `connection_id` and every logical SECRETS/SETTINGS name MUST
# match this, full-string. Lowercase letters and digits only: no `_`, no
# `-`, no `.`, no uppercase. Enforcement (`re.fullmatch`) is stage 2;
# this is the stated rule.
#
# Why this charset and not a fancier separator: excluding `_` from both
# segments is what makes `ENV_VAR_TEMPLATE` injective (neither segment
# can contain the joining character, so the join can't be re-split two
# ways), and excluding uppercase entirely is what makes `.upper()`
# lossless, since two connection ids that only differ by case cannot
# both exist. A stricter separator scheme would need the same charset
# work anyway, so this is the lazier rule that is also the correct one.
ID_CHARSET_PATTERN = r"^[a-z0-9]+$"

# A logical SECRETS/SETTINGS name ending in this suffix (any case) is
# REJECTED at declaration time (stage 2). Defence in depth, not a live
# constraint today: `ID_CHARSET_PATTERN` already forbids `_`, so no name
# matching it can end in `_FILE` in the first place. This guard only
# becomes load-bearing if the charset rule is ever relaxed to permit
# underscores, at which point a name like "token_file" would make a
# file PATH indistinguishable from a direct VALUE for the name "token".
# Enforcement is stage 2; this is the stated rule.
FORBIDDEN_NAME_SUFFIX = FILE_SUFFIX

# The one environment variable naming convention, used for a plugin's
# declared secrets and its declared settings alike. Both fields are
# upper cased. A name declared in SECRETS may never also appear in
# SETTINGS, which is what keeps one template sufficient.
#
# This is the CURRENT RECOMMENDATION for how connection settings reach
# the child (option (b) of three: (a) child re-reads `connections.toml`
# and the parent passes only secrets, (b) this shared template, (c)
# settings on argv), not a settled three-way choice presented to the
# reviewer as still open. The disjointness guard in
# `plugin.read_declaration` is what this template requires, and it is
# already in place. If the reviewer picks (a) or (c) instead, both the
# guard and this shared template come back out.
ENV_VAR_TEMPLATE = "AGENT_KB_{connection_id}_{name}"

# The ONLY variables a child process inherits from the parent, on top of
# its own connection's variables. Everything else in the parent's
# environment stays in the parent. Proxy and CA vars are included so a
# deployment behind a corporate proxy or a private CA has a remedy;
# without them egress silently breaks with no way for the deployer to
# fix it short of patching this tuple.
RUNTIME_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "PYTHONPATH",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE",
)


class SecretUnavailable(Exception):
    """Neither injection form was present for a declared secret.

    A SKIP, not a failure: the plugin does not run this tick, the reason
    is logged loudly, `last_ok_at` is left unadvanced, and every other
    plugin in the run continues. The message names the logical secret
    and the connection. It never names, and never contains, a value.
    """


@dataclass(frozen=True, repr=False, eq=False)
class Secret:
    """A masked, late-resolving handle to one injected credential.

    NOT a `str` and never a subclass of one, so nothing that formats,
    joins, logs or serialises a string can move a credential by
    accident. `__repr__` and `__str__` both render `SECRET_MASK` by
    construction, which is what makes the HANDLE safe by default, rather
    than safe by review, in an f-string, a log line, `vars()`, `asdict()`
    and an exception message built from the handle itself.

    This does NOT make a live traceback frame safe: `reveal` (stage 2)
    returns a plain unmasked `str`, and any frame between the resolver
    and the outbound call that holds that `str` in a local is visible to
    `--showlocals`, a debugger, or a crash reporter that dumps locals.
    Stage 2 constraint: a resolver MUST NOT cache the resolved value; if
    something must hold onto it, that holder needs its own masked
    `__repr__`, because `_resolve` itself is reachable via `vars()`,
    `asdict()`, `astuple()` and `__closure__` on this frozen dataclass.

    `__eq__` and `__hash__` are identity based (`eq=False`), so no
    comparison can ever touch a value, and `__format__` inherits
    `object.__format__`, which defers to the masked `__str__` for an
    empty format spec and raises for any other. There is deliberately no
    way to widen this: an unmask is one named call, or it does not
    happen.

    LATE RESOLVING. The handle holds a resolver, not a value. Nothing is
    read until someone asks, so a credential rotated between the run
    start and the call is picked up for free, and a handle that is
    created but never used never touches the environment at all.

    Members:
        name: the plugin's own LOGICAL name for this credential, as
            declared in its `SECRETS`. Safe to log. It says what the
            credential is for, never where it lives or what it is.
        _resolve: zero-argument resolver supplied by the parent. Private
            by name because `reveal` is the one supported way through.
        reveal: THE single unmask accessor, defined in stage 2. Contract:
            calls the resolver and returns the credential as `str`.
            Raises `SecretUnavailable` when neither injection form is
            present. The returned `str` is live and unmasked, so it may
            go only into an outbound request; it may never be stored,
            logged, put in argv, put on a Row, or included in an
            exception message. Callers re-call rather than caching, and
            an `AuthExpired` retry re-calls exactly once.
    """

    name: str
    _resolve: Callable[[], str]

    reveal: ClassVar[Callable[[Secret], str]]

    def __repr__(self) -> str:
        return SECRET_MASK

    __str__ = __repr__
