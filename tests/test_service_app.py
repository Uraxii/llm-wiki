"""`/health` and the forwarded-header gate. No web framework client
stands in for a running service here: `ForwardedHeaderMiddleware` is
exercised through a fake ASGI `scope`, which is what a running
uvicorn process hands it too, and `health` is called directly.
"""
from __future__ import annotations

import asyncio
import unittest

from llmwiki_service import app


def _http_scope(headers: dict[bytes, bytes], client: tuple[str, int]) -> dict:
    return {
        "type": "http",
        "scheme": "http",
        "client": client,
        "headers": list(headers.items()),
    }


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

    def test_headers_trusted_when_the_peer_is_the_trusted_proxy(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy="10.0.0.1")
        scope = _http_scope(
            {b"x-forwarded-proto": b"https", b"x-forwarded-for": b"6.6.6.6, 10.0.0.1"},
            ("10.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "https")
        self.assertEqual(seen["client"][0], "6.6.6.6")

    def test_forged_headers_ignored_when_the_peer_is_not_the_trusted_proxy(self) -> None:
        # This is the reproduction from the phase 2 review: trusted_proxy
        # names 10.0.0.1, but the request actually arrives from 127.0.0.1.
        # A peer address check must reject it even though trusted_proxy is set.
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy="10.0.0.1")
        scope = _http_scope(
            {b"x-forwarded-proto": b"https", b"x-forwarded-for": b"6.6.6.6"},
            ("127.0.0.1", 12345),
        )
        seen = asyncio.run(_dispatch(middleware, scope))
        self.assertEqual(seen["scheme"], "http")
        self.assertEqual(seen["client"], ("127.0.0.1", 12345))

    def test_absent_headers_leave_the_transport_values_alone(self) -> None:
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy="10.0.0.1")
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
        middleware = app.ForwardedHeaderMiddleware(app=None, trusted_proxy="10.0.0.1")
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
    def test_wraps_the_starlette_app_in_the_forwarded_header_middleware(self) -> None:
        wrapped = app.build_app(trusted_proxy=None)
        self.assertIsInstance(wrapped, app.ForwardedHeaderMiddleware)


if __name__ == "__main__":
    unittest.main()
