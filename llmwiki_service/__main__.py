"""`python -m llmwiki_service <deployment-file>`.

The startup order is the contract phase 2 names: resolve the argument,
run every startup refusal (`deployment.startup_refusals`, which covers
the three pre-parse refusals and the ten-row registry in one call),
and only once that list is empty does anything build a TLS context or
bind a socket. `main` below reads top to bottom in that order; nothing
after the refusal check can run before it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading

import uvicorn

from llmwiki_service import app as app_module
from llmwiki_service import deployment, tls

logger = logging.getLogger("llmwiki_service")


def _parse_bind(bind: str) -> tuple[str, int]:
    """`"host:port"` split the same way `deployment._bind_is_private`
    reads it: the last colon separates the port, and an IPv6 host
    keeps the brackets `[server] bind` was written with until they are
    stripped here."""
    host, sep, port = bind.rpartition(":")
    if not sep:
        raise ValueError(f"[server] bind is not host:port: {bind!r}")
    return host.strip("[]"), int(port)


def _terminate_mode_config(
    tls_cfg: dict, host: str, port: int, stop_event: threading.Event
) -> tuple[uvicorn.Config, threading.Thread]:
    """The `uvicorn.Config` for `mode = "terminate"`, and the watcher
    thread it started.

    Builds the holder, wires `SIGHUP` and the file watcher to the same
    `ContextHolder.reload`, and hands uvicorn a context whose identity
    never changes (`tls.server_context`), so the reload path has one
    place that publishes and the serving path has one place that reads.
    """
    cert, key = tls_cfg["cert"], tls_cfg["key"]
    min_version = tls_cfg.get("min_version")
    holder = tls.ContextHolder(tls.build_context(cert, key, min_version))
    server_ctx = tls.server_context(holder)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGHUP, holder.reload, cert, key, min_version, logger
    )

    watcher = threading.Thread(
        target=tls.watch_certificates,
        args=(holder, cert, key, min_version, logger, stop_event),
        daemon=True,
    )
    watcher.start()

    config = uvicorn.Config(
        app_module.build_app(tls_cfg.get("trusted_proxy")),
        host=host,
        port=port,
        proxy_headers=False,  # ForwardedHeaderMiddleware owns this, gated on trusted_proxy
        ssl_context_factory=lambda _config, _default: server_ctx,
    )
    return config, watcher


async def _serve(depl: dict) -> int:
    host, port = _parse_bind(depl["server"]["bind"])
    tls_cfg = depl.get("tls", {})
    stop_event = threading.Event()
    watcher: threading.Thread | None = None

    if tls_cfg.get("mode") == "terminate":
        config, watcher = _terminate_mode_config(tls_cfg, host, port, stop_event)
    else:
        # mode == "upstream": the deployment's own startup refusal
        # (row 9) already required `bind` to be a private interface,
        # so a plaintext listener here is the reverse proxy's backend,
        # never a port exposed to the network directly.
        config = uvicorn.Config(
            app_module.build_app(tls_cfg.get("trusted_proxy")),
            host=host,
            port=port,
            proxy_headers=False,  # ForwardedHeaderMiddleware owns this, gated on trusted_proxy
        )

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

    refusals = deployment.startup_refusals(arg, os.environ)
    if refusals:
        for message in refusals:
            print(message, file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    depl = deployment.load_deployment(arg)
    return asyncio.run(_serve(depl))


if __name__ == "__main__":
    raise SystemExit(main())
