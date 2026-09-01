"""`/health` and the forwarded-header gate. No web framework client
stands in for a running service here: `ForwardedHeaderMiddleware` is
exercised through a fake ASGI `scope`, which is what a running
uvicorn process hands it too, and `health` is called directly.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llmwiki_service import app, auth, deployment, tokens


PROXY = ipaddress.ip_address("10.0.0.1")


def _http_scope(
    headers: dict[bytes, bytes] | list[tuple[bytes, bytes]],
    client: tuple[str, int],
) -> dict:
    """An ASGI scope. `headers` is a list, not a mapping, so a test can
    send the same header name twice the way a chain of proxies does."""
    return {
        "type": "http",
        "scheme": "http",
        "client": client,
        "headers": list(headers.items() if isinstance(headers, dict) else headers),
    }


async def _call_asgi(
    asgi_app: app.ASGIApp,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict]:
    """Drive a real ASGI callable the way uvicorn would: a full scope,
    a `receive` that hands over an empty body once, and a `send` that
    records every message the app emits. `path` may carry a `?query`;
    it is split off into `query_string` the way a real request line
    would arrive already split by the time uvicorn builds a scope."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "scheme": "http",
        "method": method,
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": headers or [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await asgi_app(scope, receive, send)
    return messages


def _status(messages: list[dict]) -> int:
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


def _body(messages: list[dict]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )


def _headers(messages: list[dict]) -> dict[str, str]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


async def _dispatch(middleware: app.ForwardedHeaderMiddleware, scope: dict) -> dict:
    seen = {}

    async def inner_app(scope, receive, send):
        seen["scheme"] = scope.get("scheme")
        seen["client"] = scope.get("client")

    middleware.app = inner_app
    await middleware(scope, None, None)
    return seen


class HealthTest(unittest.TestCase):
    def test_answers_ok_with_no_deployment_detail(self) -> None:
        response = asyncio.run(app.health(None))
        self.assertEqual(response.status_code, 200)
        body = bytes(response.body)
        self.assertEqual(body, b"ok")
        for leak in (b"kb", b"version", b"/"):
            self.assertNotIn(leak, body)


class ForwardedHeaderMiddlewareTest(unittest.TestCase):
    def test_forged_headers_ignored_without_a_trusted_proxy(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=None)
        scope = _http_scope(
            {b"x-forwarded-proto": b"https", b"x-forwarded-for": b"6.6.6.6"},
            ("127.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "http")
        self.assertEqual(seen["client"], ("127.0.0.1", 12345))

    def test_the_rightmost_element_wins_not_the_client_supplied_left(self) -> None:
        # nginx's proxy_add_x_forwarded_for, and Caddy and Traefik, append
        # the peer they observed to whatever the client sent. So a client
        # sending "X-Forwarded-For: 6.6.6.6" arrives here as
        # "6.6.6.6, <real client>", and the left element is the forgery.
        # This test asserted "6.6.6.6" until the phase 2 review; it
        # encoded the bug.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope(
            {b"x-forwarded-proto": b"https", b"x-forwarded-for": b"6.6.6.6, 10.0.0.1"},
            ("10.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "https")
        self.assertEqual(seen["client"][0], "10.0.0.1")

    def test_a_rightmost_element_that_is_not_an_address_keeps_the_peer(self) -> None:
        # Never let header text become a client key: FailureLimiter tracks
        # MAX_TRACKED_CLIENTS of them, and a caller who can invent keys can
        # saturate that table and switch rate limiting off for everyone.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope(
            {b"x-forwarded-for": b"6.6.6.6, not-an-address"}, ("10.0.0.1", 12345)
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["client"], ("10.0.0.1", 12345))

    def test_repeated_headers_are_joined_not_collapsed_to_the_last(self) -> None:
        # Two header lines mean the same as one comma-joined line, so the
        # rightmost element of the last line is the answer. Reading the
        # scope's headers through dict() collapsed them instead, which is
        # a third selection rule a caller gets to steer.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope(
            [
                (b"x-forwarded-for", b"6.6.6.6"),
                (b"x-forwarded-for", b"7.7.7.7, 10.0.0.1"),
            ],
            ("10.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["client"][0], "10.0.0.1")

    def test_two_forged_lefts_from_behind_the_proxy_share_one_client(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        first = asyncio.run(_dispatch(middleware, _http_scope(
            {b"x-forwarded-for": b"1.1.1.1, 203.0.113.9"}, ("10.0.0.1", 1))))
        second = asyncio.run(_dispatch(middleware, _http_scope(
            {b"x-forwarded-for": b"2.2.2.2, 203.0.113.9"}, ("10.0.0.1", 2))))
        self.assertEqual(first["client"][0], second["client"][0])
        self.assertEqual(first["client"][0], "203.0.113.9")

    def test_a_header_byte_that_is_not_utf8_is_not_an_exception(self) -> None:
        # Header bytes are not UTF-8 by definition. A strict decode turned
        # one crafted byte into a 500 before any route ran.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope(
            {b"x-forwarded-for": b"\xff", b"x-forwarded-proto": b"\xff"},
            ("10.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["client"], ("10.0.0.1", 12345))
        self.assertEqual(seen["scheme"], "http")

    def test_a_scheme_that_is_not_http_or_https_is_ignored(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope({b"x-forwarded-proto": b"gopher"}, ("10.0.0.1", 12345))
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "http")

    def test_a_hostname_trusted_proxy_never_reaches_this_middleware(self) -> None:
        # deployment.trusted_proxy_address is the one predicate, and it
        # returns None for a name, so the middleware only ever sees an
        # address or None. Both directions are covered by the row 11
        # refusal in tests/test_service_deployment.py.
        self.assertIsNone(
            deployment.trusted_proxy_address(
                {"tls": {"trusted_proxy": "ingress.internal"}}
            )
        )

    def test_forged_headers_ignored_when_the_peer_is_not_the_trusted_proxy(self) -> None:
        # This is the reproduction from the phase 2 review: trusted_proxy
        # names 10.0.0.1, but the request actually arrives from 127.0.0.1.
        # A peer address check must reject it even though trusted_proxy is set.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope(
            {b"x-forwarded-proto": b"https", b"x-forwarded-for": b"6.6.6.6"},
            ("127.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "http")
        self.assertEqual(seen["client"], ("127.0.0.1", 12345))

    def test_absent_headers_leave_the_transport_values_alone(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope({}, ("10.0.0.1", 12345))
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "http")
        self.assertEqual(seen["client"], ("10.0.0.1", 12345))

    def test_two_forged_for_headers_from_an_untrusted_peer_collapse_to_one_client(
        self,
    ) -> None:
        # FailureLimiter (phase 1) keys its rate limit on this middleware's
        # resolved client. Two different forged X-Forwarded-For values from
        # the same untrusted peer must resolve to the same client, or the
        # limiter never accumulates failures in one bucket.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        first = asyncio.run(
            _dispatch(
                middleware,
                _http_scope(
                    {b"x-forwarded-for": b"1.1.1.1"}, ("127.0.0.1", 12345)
                ),
            )
        )
        second = asyncio.run(
            _dispatch(
                middleware,
                _http_scope(
                    {b"x-forwarded-for": b"2.2.2.2"}, ("127.0.0.1", 55555)
                ),
            )
        )
        self.assertEqual(first["client"][0], second["client"][0])
        self.assertEqual(first["client"][0], "127.0.0.1")


class BuildAppTest(unittest.TestCase):
    """`build_app` now also wires phase 3's four read routes, so every
    call site here needs a throwaway `TokenStore` and a pair of fresh
    limiters. None of it is exercised for behaviour in this file:
    `tests/test_service_routes.py` owns the routes themselves."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = tokens.TokenStore(
            Path(self.dir.name) / "tokens.sqlite",
            tokens.Peppers({1: b"pepper-one"}),
        )
        self.failure_limiter = auth.FailureLimiter()
        self.search_limiter = auth.SearchLimiter(30)

    def _build(self, trusted_proxy=None, deployment_dict=None):
        return app.build_app(
            trusted_proxy,
            deployment_dict or {},
            self.store,
            self.failure_limiter,
            self.search_limiter,
        )

    def test_wraps_the_starlette_app_in_the_forwarded_header_middleware(self) -> None:
        wrapped = self._build()
        self.assertIsInstance(wrapped, app.ForwardedHeaderMiddleware)

    def test_get_health_answers_ok_through_the_real_asgi_call(self) -> None:
        # Drives the actual ASGI callable build_app returns, the same
        # entry point uvicorn calls, so a broken route, method, or
        # endpoint wiring shows up here and not just in a unit test of
        # one function in isolation.
        wrapped = self._build()
        messages = asyncio.run(_call_asgi(wrapped, "GET", "/health"))
        self.assertEqual(_status(messages), 200)
        self.assertEqual(_body(messages), b"ok")

    def test_the_trusted_proxy_argument_reaches_the_middleware(self) -> None:
        wrapped = self._build(trusted_proxy=PROXY)
        self.assertEqual(wrapped.trusted_proxy, PROXY)

    def test_search_limiter_carries_the_limit_and_window_it_was_built_with(self) -> None:
        # Not route behaviour (test_service_routes.py owns that): just
        # confirming the fresh SearchLimiter this file hands to
        # build_app is the real thing and not a stand-in with unset
        # fields, since nothing else in this file inspects it directly.
        self.assertEqual(self.search_limiter.limit, 30)
        self.assertEqual(self.search_limiter.window_sec, auth.SEARCH_WINDOW_SEC)
        self.assertEqual(self.search_limiter._calls, {})

    def test_store_and_failure_limiter_reach_the_routes_not_a_stand_in(self) -> None:
        # A presented token, even a garbage one, makes `authorize` call
        # `failure_limiter.blocked` and then `store.verify` regardless
        # of `[access] read`. Either argument arriving as anything other
        # than the real object crashes instead of answering 401, so a
        # clean 401 here is the proof both reached routes.make_routes.
        deployment_dict = {"access": {"read": "open"}, "kbs": {}}
        wrapped = self._build(deployment_dict=deployment_dict)
        messages = asyncio.run(
            _call_asgi(
                wrapped, "GET", "/kb/nope/schema",
                headers=[(b"authorization", b"Bearer not-a-real-token")],
            )
        )
        self.assertEqual(_status(messages), 401)

    def test_an_unmatched_path_answers_json_not_found(self) -> None:
        # Starlette's router raises HTTPException(404) before any route
        # in routes.py runs; app._router_error must catch that and
        # answer the same not_found body the routes use, not the
        # router's own plain-text default.
        wrapped = self._build()
        messages = asyncio.run(_call_asgi(wrapped, "GET", "/no/such/route"))
        self.assertEqual(_status(messages), 404)
        self.assertEqual(_headers(messages)["content-type"], "application/json")
        self.assertEqual(json.loads(_body(messages)), {"error": "not_found"})

    def test_a_disallowed_method_answers_json_method_not_allowed(self) -> None:
        wrapped = self._build()
        messages = asyncio.run(_call_asgi(wrapped, "POST", "/health"))
        self.assertEqual(_status(messages), 405)
        self.assertEqual(_headers(messages)["content-type"], "application/json")
        self.assertEqual(
            json.loads(_body(messages)), {"error": "method_not_allowed"}
        )

    def test_search_limiter_argument_reaches_make_routes_unchanged(self) -> None:
        # A wiring-only proof, deliberately not routed through a real
        # request: driving search_limiter through the search route
        # would need to call SearchLimiter.check for real, which is
        # tests/test_service_routes.py's job (routes.py is outside the
        # mutation gate's mutated set, so a partial call path here would
        # only add noise for that class's own mutants without proving
        # anything build_app itself does not already guarantee: the
        # exact object reaches make_routes.
        with mock.patch.object(app.routes, "make_routes", return_value=[]) as made:
            self._build()
        made.assert_called_once_with(
            {}, self.store, self.failure_limiter, self.search_limiter
        )


class ForwardedElementsTest(unittest.TestCase):
    def test_a_comma_with_no_following_space_still_splits(self) -> None:
        # A proxy is not required to put a space after the comma; this
        # is exactly the wire format a chain of proxies produces.
        elements = app.forwarded_elements(
            [(b"x-forwarded-for", b"1.1.1.1,2.2.2.2,3.3.3.3")], b"x-forwarded-for"
        )
        self.assertEqual(elements, ["1.1.1.1", "2.2.2.2", "3.3.3.3"])


class MiddlewareInitTest(unittest.TestCase):
    def test_wraps_the_given_app_not_a_substitute(self) -> None:
        sentinel = object()
        middleware = app.ForwardedHeaderMiddleware(sentinel, trusted_proxy=None)
        self.assertIs(middleware.app, sentinel)


class PeerIsTrustedTest(unittest.TestCase):
    def test_a_missing_client_with_a_trusted_proxy_set_is_not_trusted(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        self.assertFalse(middleware._peer_is_trusted(None))

    def test_a_peer_address_that_fails_to_parse_is_not_trusted(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        self.assertFalse(middleware._peer_is_trusted(("not-an-address", 1)))


class MiddlewareCallForwardsArgumentsTest(unittest.TestCase):
    def test_receive_and_send_reach_the_wrapped_app_unchanged(self) -> None:
        seen = {}

        async def inner_app(scope, receive, send):
            seen["receive"] = receive
            seen["send"] = send

        middleware = app.ForwardedHeaderMiddleware(app=inner_app, trusted_proxy=None)
        receive_sentinel = object()
        send_sentinel = object()
        scope = _http_scope({}, ("10.0.0.1", 1))
        asyncio.run(middleware(scope, receive_sentinel, send_sentinel))
        self.assertIs(seen["receive"], receive_sentinel)
        self.assertIs(seen["send"], send_sentinel)


class ResolveLoggingTest(unittest.TestCase):
    def test_logs_exactly_the_scheme_and_client_it_settled_on(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy=PROXY)
        scope = _http_scope(
            {b"x-forwarded-proto": b"https", b"x-forwarded-for": b"10.0.0.1"},
            ("10.0.0.1", 12345),
        )
        with self.assertLogs("llmwiki_service", level="INFO") as caught:
            asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(
            caught.output,
            ["INFO:llmwiki_service:request scheme=https client=('10.0.0.1', 12345)"],
        )


if __name__ == "__main__":
    unittest.main()
