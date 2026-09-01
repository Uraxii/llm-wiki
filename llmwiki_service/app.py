"""The starlette app. Phase 2 adds exactly one route, `/health`, plus
the forwarded-header handling every later route will sit behind.

Phase 3's read routes are out of scope here; see
docs/plans/02-llmwiki-service/phase-02-tls.md and FORBIDDEN in this
phase's brief. Nothing in this module names a kb.
"""
from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

ASGIApp = Callable[..., Awaitable[None]]

logger = logging.getLogger("llmwiki_service")


async def health(request: Request) -> PlainTextResponse:
    """Liveness only. No kb name, no path, no version: the deployment
    this process serves is never in this response."""
    return PlainTextResponse("ok")


class ForwardedHeaderMiddleware:
    """Trusts `X-Forwarded-Proto` and `X-Forwarded-For` only when
    `trusted_proxy` is set AND the request's real peer address is
    `trusted_proxy`, per phase 2: a client can forge both headers, and
    only the transport, not a header, says who actually connected.

    Plain ASGI middleware, not `BaseHTTPMiddleware`: this rewrites two
    scope fields and does not need to buffer a response to do it.
    """

    def __init__(self, app: ASGIApp, trusted_proxy: str | None) -> None:
        self.app = app
        self.trusted_proxy = trusted_proxy
        # Parsed once. None means "never trust": trusted_proxy is unset
        # or is not a parseable address, and the failure direction is
        # closed, never open.
        self._trusted_addr = self._parse_address(trusted_proxy)

    @staticmethod
    def _parse_address(
        value: str | None,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        if not value:
            return None
        try:
            return ipaddress.ip_address(value)
        except ValueError:
            return None

    def _peer_is_trusted(self, client: tuple[str, int] | None) -> bool:
        if self._trusted_addr is None or client is None:
            return False
        try:
            return ipaddress.ip_address(client[0]) == self._trusted_addr
        except ValueError:
            return False

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http":
            self._resolve(scope)
        await self.app(scope, receive, send)

    def _resolve(self, scope: dict) -> None:
        # The real peer, read before anything below can overwrite it.
        real_client = scope.get("client")
        if self._peer_is_trusted(real_client):
            headers = dict(scope.get("headers") or ())
            proto = headers.get(b"x-forwarded-proto")
            forwarded_for = headers.get(b"x-forwarded-for")
            if proto:
                scope["scheme"] = proto.decode().strip()
            if forwarded_for:
                host = forwarded_for.decode().split(",")[0].strip()
                port = real_client[1] if real_client else 0
                scope["client"] = (host, port)
        # Logged either way: this is the one place a reader can observe
        # whether a forged header was believed or ignored, on a request
        # the transport actually carried.
        logger.info(
            "request scheme=%s client=%s", scope.get("scheme"), scope.get("client")
        )


def build_app(trusted_proxy: str | None) -> Starlette:
    """The service's ASGI app. `trusted_proxy` gates forwarded-header
    trust for every route, `/health` included, per phase 2's rule that
    it is read only when a deployment names a proxy."""
    app = Starlette(routes=[Route("/health", health, methods=["GET"])])
    return ForwardedHeaderMiddleware(app, trusted_proxy)
