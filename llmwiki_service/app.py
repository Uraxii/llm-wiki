"""The starlette app. Phase 2 adds `/health` and the forwarded-header
handling every route sits behind; phase 3 wires the four read routes
in `routes.py` behind the same middleware, and phase 6 the three admin
routes in `admin.py`. This module stays wiring only: a route that
decides anything belongs in one of those two instead.
"""
from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from llmwiki_service import admin, routes
from llmwiki_service.auth import FailureLimiter, SearchLimiter
from llmwiki_service.tokens import TokenStore

ASGIApp = Callable[..., Awaitable[None]]

logger = logging.getLogger("llmwiki_service")


async def health(request: Request) -> PlainTextResponse:
    """Liveness only. No kb name, no path, no version: the deployment
    this process serves is never in this response."""
    return PlainTextResponse("ok")


_ROUTER_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


async def _router_error(request: Request, exc: HTTPException) -> Response:
    """Starlette's router raises `HTTPException` itself, before any
    route in `routes.py` runs, for a path with no match (404) or a
    method the matched path does not accept (405). Its default body is
    plain text and names neither error code in `routes.py`'s closed
    set, so this puts both on the same set: the router's own 404 and
    405 answer JSON, with no path, method, or exception detail.

    A status this dict does not name is not one the router raises;
    re-raised rather than guessed at, so it surfaces as a real 500
    instead of a misreported 404.
    """
    code = _ROUTER_ERROR_CODES.get(exc.status_code)
    if code is None:
        raise exc
    return JSONResponse({"error": code}, status_code=exc.status_code)


async def _internal_error(request: Request, exc: Exception) -> Response:
    """Registered under the `Exception` key, so `build_middleware_stack`
    hands it to `ServerErrorMiddleware` as its `handler` instead of
    that middleware's own default, which answers plain text. This is the
    catch-all for a fault no route or `_router_error` handles itself,
    `sqlite3.OperationalError: database is locked` after a read route's
    5s busy timeout included, since read routes take no kb lock while a
    writer sweeps.

    `ServerErrorMiddleware` always re-raises `exc` once this returns
    (starlette/middleware/errors.py: "We always continue to raise the
    exception... allows servers to log the error"), so this only
    changes the response body; the traceback still reaches whatever
    wraps the ASGI call, uvicorn's own logger in production.
    """
    return JSONResponse({"error": "internal_error"}, status_code=500)


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


def build_app(
    trusted_proxy: IPAddress | None,
    deployment: dict,
    store: TokenStore,
    failure_limiter: FailureLimiter,
    search_limiter: SearchLimiter,
    bootstrap: str | None,
) -> ASGIApp:
    """The service's ASGI app: `/health`, the four routes
    `routes.make_routes` builds, and the three
    `admin.make_admin_routes` builds, all wrapped in the
    forwarded-header middleware, which is what the return type says.

    `trusted_proxy` gates forwarded-header trust for every route,
    `/health` included, per phase 2's rule that it is read only when a
    deployment names a proxy.

    `bootstrap` is the value of `LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN`, read
    by `deployment.bootstrap_admin_token`, and it authenticates the
    admin routes and nothing else. It has no default: a caller that
    omitted it would get a service whose admin routes no operator can
    reach, and no startup refusal covers that.

    The admin routes share one listener with the read routes and one
    `FailureLimiter`. A second listener would share this process, this
    store, and this limiter anyway, and would add a bind key, a startup
    refusal, and a second socket to protect a boundary the operator
    draws better with a firewall rule or a proxy that does not route
    `/admin` from outside.
    """
    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            *routes.make_routes(deployment, store, failure_limiter, search_limiter),
            *admin.make_admin_routes(
                deployment, store, failure_limiter, bootstrap
            ),
        ],
        exception_handlers={
            HTTPException: _router_error,
            Exception: _internal_error,
        },
    )
    return ForwardedHeaderMiddleware(app, trusted_proxy)
