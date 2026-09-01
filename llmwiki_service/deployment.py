"""The deployment file and the startup refusals that gate it.

Phase 2 of docs/plans/02-llmwiki-service/phase-02-tls.md defines both the
deployment file's keys and its 13-row refusal table in one place. This
module is that table made runnable: a later unit calls `startup_refusals`
once, before it binds a socket, and gets back every reason this
deployment must not start.

The first three refusals fire before the file is parsed at all (no
argument, the path does not exist, the path exists and cannot be read),
so there is no parsed dict yet for a check function to read. They are
handled by `pre_parse_refusal`, not by the registry below. The other ten
are a registry of small, named checks over the parsed dict: an ordered
table instead of a 13-branch function, so a reviewer can compare it
against the spec's table row by row.

None of the ten registry rows needs the service actually running: every
one is checkable from the deployment file, the filesystem, and the
environment alone.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import sqlite3
import ssl
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from llmwiki.core import load_config
from llmwiki_service.auth import OPEN
from llmwiki_service.tokens import peppers_from_env

TOKEN_DB_NAME = "tokens.sqlite"  # sits under [server] state, per phase 2

# Phase 1 names LLM_WIKI_PEPPER but never names the bootstrap admin token's
# variable, though it promises one exists ("the operator supplies two
# secrets at startup ... the pepper, and the first admin token"). Named
# here, following LLM_WIKI_PEPPER's shape, because row 5 below has to
# check for it and phase 2 is where every deployment-facing name lives.
BOOTSTRAP_ADMIN_TOKEN_ENV_VAR = "LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN"

USAGE = "usage: python -m llmwiki_service <deployment-file>"


def pre_parse_refusal(argv_path: str | None) -> str | None:
    """One of the three refusals that fire before the file is parsed, or
    `None` when `argv_path` is a plain, readable file ready for
    `tomllib`.

    A path that exists but is a directory, or a file with no read
    permission, both fall under "exists and cannot be read": the table
    names three pre-parse conditions, not four, and both faults are the
    same fault from the caller's side, a file that cannot be opened.
    """
    if not argv_path:
        return f"no deployment file given: {USAGE}"
    path = Path(argv_path)
    if not path.exists():
        return f"deployment file not found: {argv_path}"
    if not path.is_file() or not os.access(path, os.R_OK):
        return f"deployment file exists and cannot be read: {argv_path}"
    return None


def load_deployment(path: str) -> dict:
    """The deployment file at `path`, as the plain dict `tomllib`
    returns. No dataclass: `config.toml` is handled as a plain dict
    throughout this repo, and this file follows the same convention.

    Call `pre_parse_refusal` first. A file that parses to invalid TOML
    is not one of the three pre-parse refusals; this raises `ValueError`
    naming the path, matching `llmwiki.core.load_config`.
    """
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"malformed deployment file: {path}: {exc}") from exc


def _writable_dir(path_str: str) -> bool:
    path = Path(path_str)
    return path.is_dir() and os.access(path, os.W_OK)


def _token_db_path(deployment: dict) -> Path | None:
    state = deployment.get("server", {}).get("state")
    return Path(state) / TOKEN_DB_NAME if state else None


def _admin_row_exists(deployment: dict) -> bool:
    """Whether the token database already has a live admin row. A
    missing database reads as no, never as an error: a deployment
    starting for the first time has not created it yet."""
    db_path = _token_db_path(deployment)
    if db_path is None or not db_path.is_file():
        return False
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ) as conn:
            row = conn.execute(
                "SELECT 1 FROM tokens WHERE role = 'admin' "
                "AND revoked_at IS NULL LIMIT 1"
            ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _bind_is_private(bind: str) -> bool:
    """True when `bind`'s host is loopback or private, and is not the
    unspecified "every interface" address.

    `ipaddress` marks both `0.0.0.0` and `::` as `is_private`, which
    would let the classic "expose to the whole network" bind pass this
    check unnoticed. `is_unspecified` is what actually distinguishes
    "only this interface" from "every interface", so it gates the
    result rather than `is_private` alone.
    """
    host, sep, _port = bind.rpartition(":")
    host = host.strip("[]") if sep else bind
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_loopback or ip.is_private) and not ip.is_unspecified


def _check_pepper(deployment: dict, environ: Mapping[str, str]) -> str | None:
    """Row 4: no pepper supplied. Every token would otherwise fail to
    verify, silently, at request time instead of at boot."""
    try:
        peppers_from_env(environ)
    except ValueError as exc:
        return str(exc)
    return None


def _check_admin_bootstrap(deployment: dict, environ: Mapping[str, str]) -> str | None:
    """Row 5: no bootstrap admin token and no existing admin row.
    Minting requires admin, so a deployment with neither can never
    administer itself."""
    if environ.get(BOOTSTRAP_ADMIN_TOKEN_ENV_VAR):
        return None
    if _admin_row_exists(deployment):
        return None
    return (
        f"no admin access: set {BOOTSTRAP_ADMIN_TOKEN_ENV_VAR}, or this "
        "deployment has no existing admin token to mint one with"
    )


def _check_write_open(deployment: dict, _environ: Mapping[str, str]) -> str | None:
    """Row 6: `[access] write = "open"`. There is no deployment where
    this is intended."""
    if deployment.get("access", {}).get("write") == OPEN:
        return '[access] write = "open" is refused; write is always "token"'
    return None


def _check_cert_files(deployment: dict, _environ: Mapping[str, str]) -> str | None:
    """Row 7: `mode = "terminate"` and the certificate or key is missing
    or unreadable. Checked before the socket binds, so the failure
    surfaces at boot instead of on the first request."""
    tls = deployment.get("tls", {})
    if tls.get("mode") != "terminate":
        return None
    for setting in ("cert", "key"):
        path = tls.get(setting)
        if not path:
            return f"[tls] {setting} is not set"
        if not Path(path).is_file() or not os.access(path, os.R_OK):
            return f"[tls] {setting} is missing or unreadable: {path}"
    return None


def _check_cert_expiry(deployment: dict, _environ: Mapping[str, str]) -> str | None:
    """Row 8: certificate is expired at boot.

    Reading a certificate's expiry date requires decoding it, and the
    table has no separate row for a cert file that exists and is
    readable but does not decode as a certificate at all, so that
    failure is refused here too rather than dropped.

    No X.509 library is a dependency of this project yet, and the
    standard library exposes no public way to decode a certificate file
    that is not the peer of a live TLS connection. `ssl._ssl` is a
    private module, but `_test_decode_cert` is the same decoder
    CPython's own test suite uses for exactly this, and has shipped
    unchanged since Python 2.7.
    """
    tls = deployment.get("tls", {})
    if tls.get("mode") != "terminate":
        return None
    cert = tls.get("cert")
    if not cert or not Path(cert).is_file():
        return None  # _check_cert_files already refuses a missing cert
    try:
        info = ssl._ssl._test_decode_cert(cert)
        expires_at = ssl.cert_time_to_seconds(info["notAfter"])
    except (ssl.SSLError, KeyError, ValueError) as exc:
        return f"[tls] cert does not decode as a certificate: {cert} ({exc})"
    if expires_at <= time.time():
        return f"[tls] cert is expired: {cert}"
    return None


def _check_upstream_bind(deployment: dict, _environ: Mapping[str, str]) -> str | None:
    """Row 9: `mode = "upstream"` and no listener address is bound to a
    private interface. Prevents accidentally exposing the plaintext
    port to the network."""
    if deployment.get("tls", {}).get("mode") != "upstream":
        return None
    bind = deployment.get("server", {}).get("bind")
    if not bind:
        return "[server] bind is not set"
    if not _bind_is_private(bind):
        return f"[server] bind is not a private interface: {bind}"
    return None


def _check_open_read_needs_proxy(
    deployment: dict, _environ: Mapping[str, str]
) -> str | None:
    """Row 10: `[access] read = "open"` and `mode = "upstream"` and
    `trusted_proxy` is unset. Every caller then arrives from the proxy's
    own address, so `[limits]` collapses into one shared bucket."""
    tls = deployment.get("tls", {})
    access = deployment.get("access", {})
    if (
        access.get("read") == OPEN
        and tls.get("mode") == "upstream"
        and not tls.get("trusted_proxy")
    ):
        return (
            '[access] read = "open" needs [tls] trusted_proxy set under '
            'mode = "upstream", or every caller shares one rate limit'
        )
    return None


def _check_state_writable(deployment: dict, _environ: Mapping[str, str]) -> str | None:
    """Row 11: `[server] state` missing, or not writable by the running
    user. An unwritable state directory silently recreates the token
    database empty, and every token minted before the restart reads as
    unknown."""
    state = deployment.get("server", {}).get("state")
    if not state:
        return "[server] state is not set"
    if not _writable_dir(state):
        return f"[server] state is missing or not writable: {state}"
    return None


def _check_kbs_writable(deployment: dict, _environ: Mapping[str, str]) -> str | None:
    """Row 12: any `[kbs.<name>] path` not writable by the running
    user. The first search against a never-embedded kb fails on
    `mkdir`, and nothing else in the deployment reports it."""
    for name, table in deployment.get("kbs", {}).items():
        path = table.get("path") if isinstance(table, dict) else None
        if not path or not _writable_dir(path):
            return f"[kbs.{name}] path is missing or not writable: {path}"
    return None


def _check_kbs_legacy_endpoint(
    deployment: dict, _environ: Mapping[str, str]
) -> str | None:
    """Row 13: a kb in `[kbs]` whose `config.toml` still holds a legacy
    `[endpoint]` section. That kb would keep working against whatever
    model the ambient environment names, and a summary written by the
    wrong model is a valid summary.

    Reuses `llmwiki.core.load_config`, the kb's one config parser. A
    malformed `config.toml` raises `ValueError` from that call, the same
    as it does for the CLI, rather than folding into this refusal list.
    """
    for name, table in deployment.get("kbs", {}).items():
        path = table.get("path") if isinstance(table, dict) else None
        if not path:
            continue
        if "endpoint" in load_config(Path(path)):
            return (
                f"[kbs.{name}] config.toml still has a legacy [endpoint] "
                f"section: {path}"
            )
    return None


@dataclass(frozen=True)
class Check:
    """One row of the phase 2 refusal table, numbered as it appears
    there, paired with the function that decides whether it fires."""

    row: int
    condition: str
    run: Callable[[dict, Mapping[str, str]], str | None]


# The ten refusals checkable against an already-parsed deployment,
# in the table's own order, each named after that row's "Condition"
# column so a reviewer can walk this list beside the spec.
REGISTRY: tuple[Check, ...] = (
    Check(4, "no pepper supplied", _check_pepper),
    Check(
        5,
        "no bootstrap admin token and no existing admin row",
        _check_admin_bootstrap,
    ),
    Check(6, '[access] write = "open"', _check_write_open),
    Check(
        7,
        'mode = "terminate" and the certificate or key is missing or '
        "unreadable",
        _check_cert_files,
    ),
    Check(8, "certificate is expired at boot", _check_cert_expiry),
    Check(
        9,
        'mode = "upstream" and no listener address is bound to a '
        "private interface",
        _check_upstream_bind,
    ),
    Check(
        10,
        '[access] read = "open" and mode = "upstream" and trusted_proxy '
        "is unset",
        _check_open_read_needs_proxy,
    ),
    Check(
        11,
        "[server] state missing, or not writable by the running user",
        _check_state_writable,
    ),
    Check(
        12,
        "any [kbs.<name>] path not writable by the running user",
        _check_kbs_writable,
    ),
    Check(
        13,
        "a kb in [kbs] whose config.toml still holds a legacy "
        "[endpoint] section",
        _check_kbs_legacy_endpoint,
    ),
)


def check_deployment(deployment: dict, environ: Mapping[str, str]) -> list[str]:
    """Every refusal from `REGISTRY` that applies to `deployment`, in
    table order. Never stops at the first one: a deployment with
    several faults is reported with all of them, in one pass."""
    return [
        message
        for check in REGISTRY
        if (message := check.run(deployment, environ)) is not None
    ]


def startup_refusals(argv_path: str | None, environ: Mapping[str, str]) -> list[str]:
    """Every reason this deployment must not start, checked before a
    socket is bound. An empty list means clear to proceed.

    The three pre-parse refusals short-circuit: with no parsed
    deployment there is nothing left in `REGISTRY` to check.
    """
    refused = pre_parse_refusal(argv_path)
    if refused is not None:
        return [refused]
    deployment = load_deployment(argv_path)
    return check_deployment(deployment, environ)
