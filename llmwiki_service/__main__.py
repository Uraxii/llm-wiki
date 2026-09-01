"""`python -m llmwiki_service <deployment-file>`.

The startup order is the contract phase 2 names: resolve the argument,
run every startup refusal (`deployment.startup_refusals`, which covers
the three pre-parse refusals and the eleven-row registry in one call),
and only once that list is empty does anything build a TLS context or
bind a socket. `main` below reads top to bottom in that order; nothing
after the refusal check can run before it, and it serves the parsed
deployment that call handed back rather than reopening the file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable

import uvicorn

from llmwiki_service import app as app_module
from llmwiki_service import auth, deployment, tls, tokens

logger = logging.getLogger("llmwiki_service")


def _parse_bind(bind: str | None) -> tuple[str, int]:
    """`"host:port"` split the same way `deployment._bind_is_private`
    reads it: the last colon separates the port, and an IPv6 host
    keeps the brackets `[server] bind` was written with until they are
    stripped here.

    Raises `ValueError` naming `[server] bind`. No refusal row covers a
    missing or malformed bind under `mode = "terminate"`, so the fault
    lands on `main`'s error boundary, which needs a sentence rather
    than a `KeyError`.
    """
    if not bind:
        raise ValueError("[server] bind is not set")
    host, sep, port = bind.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"[server] bind is not host:port: {bind!r}")
    return host.strip("[]"), int(port)


def _install_sighup(reload_in_background: Callable[[], None]) -> None:
    """Wire `SIGHUP` to `reload_in_background`, in both modes.

    Installed for `mode = "upstream"` too. Registering it only in
    terminate mode left SIGHUP with its default disposition there,
    which kills the process, and the phase teaches SIGHUP as the way to
    force a reload, so an operator sweeping a host would take the
    service down with it.
    """
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGHUP, reload_in_background
    )


def _terminate_mode_config(
    tls_cfg: dict,
    host: str,
    port: int,
    trusted_proxy: app_module.IPAddress | None,
    stop_event: threading.Event,
    depl: dict,
    store: tokens.TokenStore,
    failure_limiter: auth.FailureLimiter,
    search_limiter: auth.SearchLimiter,
) -> tuple[uvicorn.Config, threading.Thread]:
    """The `uvicorn.Config` for `mode = "terminate"`, and the watcher
    thread it started.

    Builds the holder, wires `SIGHUP` and the file watcher to the same
    `ContextHolder.reload`, and hands uvicorn a context whose identity
    never changes (`tls.server_context`), so the reload path has one
    place that publishes and the serving path has one place that reads.

    `SIGHUP` runs the reload on a thread, not inline. A signal handler
    added to the loop runs on the loop thread, and `reload` reads and
    parses two files, which would stall every in-flight request for as
    long as that takes. The watcher already does this work off the loop.
    """
    cert, key = tls_cfg["cert"], tls_cfg["key"]
    min_version = tls_cfg.get("min_version")
    holder = tls.ContextHolder(tls.build_context(cert, key, min_version))
    server_ctx = tls.server_context(holder)

    _install_sighup(
        lambda: threading.Thread(
            target=holder.reload,
            args=(cert, key, min_version, logger),
            daemon=True,
        ).start()
    )

    watcher = threading.Thread(
        target=tls.watch_certificates,
        args=(holder, cert, key, min_version, logger, stop_event),
        daemon=True,
    )
    watcher.start()

    config = uvicorn.Config(
        app_module.build_app(
            trusted_proxy, depl, store, failure_limiter, search_limiter
        ),
        host=host,
        port=port,
        proxy_headers=False,  # ForwardedHeaderMiddleware owns this, gated on trusted_proxy
        ssl_context_factory=lambda _config, _default: server_ctx,
    )
    return config, watcher


def _build_auth_state(
    depl: dict,
) -> tuple[tokens.TokenStore, auth.FailureLimiter, auth.SearchLimiter]:
    """The `TokenStore` and both limiters, built once per process and
    threaded through both `_serve` mode branches: a fresh instance per
    branch would mean requests served by one code path never see the
    state requests on the other path accumulated, which is exactly the
    split-brain a single deployment file exists to prevent."""
    store = tokens.TokenStore(
        deployment._token_db_path(depl), tokens.peppers_from_env(os.environ)
    )
    failure_limiter = auth.FailureLimiter()
    search_limit = depl.get("limits", {}).get(
        "search_per_minute", auth.DEFAULT_SEARCH_PER_MINUTE
    )
    return store, failure_limiter, auth.SearchLimiter(search_limit)


async def _serve(depl: dict) -> int:
    host, port = _parse_bind(depl.get("server", {}).get("bind"))
    tls_cfg = depl.get("tls", {})
    trusted_proxy = deployment.trusted_proxy_address(depl)
    stop_event = threading.Event()
    watcher: threading.Thread | None = None
    mode = deployment.tls_mode(depl)
    store, failure_limiter, search_limiter = _build_auth_state(depl)

    if mode == deployment.TERMINATE:
        config, watcher = _terminate_mode_config(
            tls_cfg,
            host,
            port,
            trusted_proxy,
            stop_event,
            depl,
            store,
            failure_limiter,
            search_limiter,
        )
    elif mode == deployment.UPSTREAM:
        # Row 10 already required `bind` to be a private interface, so
        # this plaintext listener is the reverse proxy's backend, never
        # a port exposed to the network directly.
        _install_sighup(
            lambda: logger.info(
                'SIGHUP: no in-process certificate to reload under mode = "upstream"'
            )
        )
        config = uvicorn.Config(
            app_module.build_app(
                trusted_proxy, depl, store, failure_limiter, search_limiter
            ),
            host=host,
            port=port,
            proxy_headers=False,  # ForwardedHeaderMiddleware owns this, gated on trusted_proxy
        )
    else:
        # Unreachable: row 7 refuses every other mode before anything
        # gets here. Explicit anyway, because the branch that used to
        # be `else` served plaintext for a mode nobody recognised.
        raise ValueError(deployment.tls_mode_refusal(depl))

    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        stop_event.set()
        if watcher is not None:
            watcher.join(timeout=2)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    arg = argv[1] if len(argv) > 1 else None

    try:
        refusals, depl = deployment.startup_refusals(arg, os.environ)
        if refusals:
            for message in refusals:
                print(message, file=sys.stderr)
            return 1

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return asyncio.run(_serve(depl))
    except ValueError as exc:
        # The refusal table's blind spots land here instead of on the
        # operator as a traceback: a malformed file, an unknown
        # `min_version`, a missing bind, a key that does not match its
        # certificate. Each already carries a sentence naming the
        # setting or the path, and each still exits nonzero with
        # nothing bound.
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
