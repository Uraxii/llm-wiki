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


SCHEMES = ("http", "https")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def forwarded_elements(headers: list[tuple[bytes, bytes]], name: bytes) -> list[str]:
    """Every element of forwarded header `name`, left to right.

    Repeated header lines are joined rather than collapsed to the last
    one. A list-valued header sent as several lines means the same as
    one line holding them comma-joined, which is exactly how a chain of
    proxies each appending its own line reads, and picking one line out
    of several is a third rule an attacker gets to steer.

    Decoded as latin-1, the header wire format. Strict UTF-8 turned a
    crafted byte into a 500 before any route ran.
    """
    return [
        element.strip()
        for key, value in headers
        if key == name
        for element in value.decode("latin-1").split(",")
    ]


def rightmost_address(elements: list[str]) -> str | None:
    """The last element of `elements` when it is a literal IP address,
    normalized; `None` otherwise.

    Rightmost, not leftmost. The mainstream proxy configuration appends
    the peer it observed to whatever the client sent, so with one
    trusted proxy the last element is the only one the deployment has
    grounds to believe and every element left of it is client-supplied
    text. `None` for anything that is not an address, so the caller
    keeps the transport peer and no forged string becomes the client
    key the rate limiter counts on.
    """
    if not elements:
        return None
    try:
        return str(ipaddress.ip_address(elements[-1]))
    except ValueError:
        return None


class ForwardedHeaderMiddleware:
    """Trusts `X-Forwarded-Proto` and `X-Forwarded-For` only when
    `trusted_proxy` is set AND the request's real peer address is
    `trusted_proxy`, per phase 2: a client can forge both headers, and
    only the transport, not a header, says who actually connected.

    The peer check alone is not enough. A client behind a correctly
    configured proxy still writes the left of `X-Forwarded-For`, so the
    element believed here is the rightmost one and it must parse as an
    address.

    Plain ASGI middleware, not `BaseHTTPMiddleware`: this rewrites two
    scope fields and does not need to buffer a response to do it.
    """

    def __init__(self, app: ASGIApp, trusted_proxy: IPAddress | None) -> None:
        self.app = app
        # Already parsed, by `deployment.trusted_proxy_address`, the one
        # predicate the startup refusal uses too. `None` means never
        # trust, and the failure direction is closed, never open.
        self.trusted_proxy = trusted_proxy

    def _peer_is_trusted(self, client: tuple[str, int] | None) -> bool:
        if self.trusted_proxy is None or client is None:
            return False
        try:
            return ipaddress.ip_address(client[0]) == self.trusted_proxy
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
            headers = list(scope.get("headers") or ())
            proto = forwarded_elements(headers, b"x-forwarded-proto")
            if proto and proto[-1].lower() in SCHEMES:
                scope["scheme"] = proto[-1].lower()
            host = rightmost_address(
                forwarded_elements(headers, b"x-forwarded-for")
            )
            if host is not None:
                scope["client"] = (host, real_client[1])
        # Logged either way: this is the one place a reader can observe
        # whether a forged header was believed or ignored, on a request
        # the transport actually carried.
        logger.info(
            "request scheme=%s client=%s", scope.get("scheme"), scope.get("client")
        )


def build_app(trusted_proxy: IPAddress | None) -> ASGIApp:
    """The service's ASGI app: the starlette router wrapped in the
    forwarded-header middleware, which is what the return type says.

    `trusted_proxy` gates forwarded-header trust for every route,
    `/health` included, per phase 2's rule that it is read only when a
    deployment names a proxy.
    """
    app = Starlette(routes=[Route("/health", health, methods=["GET"])])
    return ForwardedHeaderMiddleware(app, trusted_proxy)
