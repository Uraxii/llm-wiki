"""fetch: turn a url into stored bytes under the private-address guard,
then reduce html to text.
"""
from __future__ import annotations

import re
import sys
import threading
import time
import unittest
import unittest.mock
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llmwiki import fetch  # noqa: E402

PUBLIC_ADDRESS = "93.184.216.34"  # example.com's old address: is_global True, never dialled here


def _respond(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes = b"",
    content_type: str = "text/html",
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _TestServer:
    """A local http server for one test, routing GET by path through a
    table the test can grow after construction (needed when a
    redirector's route must point at a target route added afterward)."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.routes: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                route = outer.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                route(self)

            def log_message(self, fmt: str, *args) -> None:
                pass  # keep test output free of one line per request

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://{host}:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


HTML_DOC = (
    "<html><body>"
    "<nav>Nav junk that must not survive extraction.</nav>"
    "<script>var tracked = true;</script>"
    "<style>body { color: red; }</style>"
    "<article><p>The real article text, long enough to win.</p></article>"
    "<footer>Footer junk that must not survive extraction.</footer>"
    "</body></html>"
)


class FetchTest(unittest.TestCase):
    def _serve(self, host: str = "127.0.0.1") -> _TestServer:
        server = _TestServer(host)
        self.addCleanup(server.close)
        return server

    def _public(self):
        """The guard refuses a private address and every test server
        binds to loopback, so DNS is faked to a public address; scheme,
        host and is_global logic in `_require_public` all still run for
        real."""
        return unittest.mock.patch.object(
            fetch, "_resolved_addresses", return_value=[PUBLIC_ADDRESS]
        )

    # 1-3: _require_public, no server involved.

    def test_require_public_refuses_a_loopback_address(self) -> None:
        with self.assertRaises(fetch.FetchError) as ctx:
            fetch._require_public("http://127.0.0.1/")
        self.assertIn("private or local address", str(ctx.exception))

    def test_require_public_refuses_a_non_http_scheme_and_a_missing_host(self) -> None:
        with self.assertRaises(fetch.FetchError):
            fetch._require_public("file:///etc/passwd")
        with self.assertRaises(fetch.FetchError):
            fetch._require_public("http:///nopath")

    def test_require_public_reports_a_host_that_does_not_resolve(self) -> None:
        with unittest.mock.patch.object(
            fetch, "_resolved_addresses", side_effect=OSError("boom")
        ):
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch._require_public("http://example.invalid/")
        self.assertIn("cannot resolve", str(ctx.exception))

    # 4-12: fetch() and extract() against a local server.

    def test_fetch_and_extract_a_plain_html_page(self) -> None:
        server = self._serve()
        server.routes["/"] = lambda h: _respond(h, 200, HTML_DOC.encode(), "text/html")

        with self._public():
            final_url, content_type, data = fetch.fetch(server.url + "/")
        content_type, data = fetch.extract(content_type, data)

        self.assertEqual(final_url, server.url + "/")
        self.assertEqual(content_type, "text/markdown")
        text = data.decode("utf-8")
        self.assertIn("real article text", text)
        self.assertNotIn("Nav junk", text)
        self.assertNotIn("tracked", text)
        self.assertNotIn("color: red", text)
        self.assertNotIn("Footer junk", text)

    def test_one_redirect_hop_is_followed_and_reports_the_second_url(self) -> None:
        server = self._serve()
        server.routes["/start"] = lambda h: _respond(
            h, 302, b"", headers={"Location": server.url + "/end"}
        )
        server.routes["/end"] = lambda h: _respond(
            h, 200, b"<html><body><main>Landed here.</main></body></html>", "text/html"
        )

        with self._public():
            final_url, _content_type, _data = fetch.fetch(server.url + "/start")

        self.assertEqual(final_url, server.url + "/end")

    def test_redirect_to_a_private_address_on_the_second_hop_is_refused(self) -> None:
        # Two distinct loopback addresses stand in for two distinct
        # hosts, so the DNS-faking mock can tell the hops apart by the
        # host string _require_public actually asks it about.
        second = self._serve(host="127.0.0.2")
        second.routes["/end"] = lambda h: _respond(h, 200, b"landed", "text/plain")
        first = self._serve(host="127.0.0.1")
        first.routes["/start"] = lambda h: _respond(
            h, 302, b"", headers={"Location": second.url + "/end"}
        )

        def resolved(host: str) -> list[str]:
            return [PUBLIC_ADDRESS] if host == "127.0.0.1" else ["127.0.0.1"]

        with unittest.mock.patch.object(
            fetch, "_resolved_addresses", side_effect=resolved
        ) as mock_resolve:
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch.fetch(first.url + "/start")

        self.assertEqual(mock_resolve.call_count, 2)
        self.assertIn("private or local address", str(ctx.exception))

    def test_redirect_chain_longer_than_the_cap_is_refused(self) -> None:
        server = self._serve()
        server.routes["/loop"] = lambda h: _respond(
            h, 302, b"", headers={"Location": server.url + "/loop"}
        )

        with self._public():
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch.fetch(server.url + "/loop")

        self.assertIn("redirect", str(ctx.exception))

    def test_body_over_the_cap_is_refused(self) -> None:
        server = self._serve()
        server.routes["/"] = lambda h: _respond(h, 200, b"x" * 5000, "text/plain")

        with self._public(), unittest.mock.patch.object(fetch, "MAX_BYTES", 64):
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch.fetch(server.url + "/")

        self.assertIn("64", str(ctx.exception))

    def test_disallowed_content_type_is_refused(self) -> None:
        server = self._serve()
        server.routes["/"] = lambda h: _respond(h, 200, b"\x89PNG", "image/png")

        with self._public():
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch.fetch(server.url + "/")

        self.assertIn("image/png", str(ctx.exception))

    def test_pdf_is_refused_by_type_and_by_sniffed_body(self) -> None:
        server = self._serve()
        server.routes["/typed"] = lambda h: _respond(
            h, 200, b"%PDF-1.4 fake pdf bytes", "application/pdf"
        )
        server.routes["/sneaky"] = lambda h: _respond(
            h, 200, b"%PDF-1.4 fake pdf bytes", "text/plain"
        )

        with self._public():
            with self.assertRaises(fetch.FetchError):
                fetch.fetch(server.url + "/typed")
            with self.assertRaises(fetch.FetchError) as ctx:
                fetch.fetch(server.url + "/sneaky")

        self.assertIn("pdf", str(ctx.exception))

    def test_a_read_timeout_is_refused(self) -> None:
        def slow(h: BaseHTTPRequestHandler) -> None:
            time.sleep(0.5)
            _respond(h, 200, b"hello", "text/plain")

        server = self._serve()
        server.routes["/"] = slow

        with self._public(), unittest.mock.patch.object(fetch, "TIMEOUT_SEC", 0.2):
            with self.assertRaises(fetch.FetchError):
                fetch.fetch(server.url + "/")

    def test_a_404_is_refused(self) -> None:
        server = self._serve()

        with self._public():
            with self.assertRaises(fetch.FetchError):
                fetch.fetch(server.url + "/missing")

    # 13: clean_url.

    def test_clean_url_strips_tracking_and_fragment_keeps_the_rest(self) -> None:
        dirty = "https://example.com/page?a=1&utm_source=x&fbclid=abc&b=2#frag"
        self.assertEqual(fetch.clean_url(dirty), "https://example.com/page?a=1&b=2")

    def test_clean_url_leaves_a_clean_url_unchanged(self) -> None:
        clean = "https://example.com/path/"
        self.assertEqual(fetch.clean_url(clean), clean)

    def test_clean_url_drops_uppercase_tracking_keys(self) -> None:
        dirty = "https://example.com/page?FBCLID=x&REF=hn&GCLID=1&k=1"
        self.assertEqual(fetch.clean_url(dirty), "https://example.com/page?k=1")

    def test_clean_url_rewrites_a_github_blob_to_raw(self) -> None:
        blob = "https://github.com/owner/repo/blob/main/path/file.py"
        self.assertEqual(
            fetch.clean_url(blob),
            "https://raw.githubusercontent.com/owner/repo/main/path/file.py",
        )

    # 14: densest_text.

    def test_densest_text_picks_the_longer_article(self) -> None:
        html = (
            "<article>Short one.</article>"
            "<article>" + ("Long article text. " * 20) + "</article>"
        )
        result = fetch.densest_text(html)
        self.assertIn("Long article text.", result)
        self.assertNotIn("Short one.", result)

    def test_densest_text_falls_back_to_the_whole_document(self) -> None:
        html = "<html><body><p>Just a paragraph, no article or main.</p></body></html>"
        self.assertIn("Just a paragraph", fetch.densest_text(html))

    def test_densest_text_returns_empty_for_markup_with_no_text(self) -> None:
        html = "<html><body><img src='x.png'/><br/></body></html>"
        self.assertEqual(fetch.densest_text(html), "")

    # 15: extract.

    def test_extract_returns_input_unchanged_when_nothing_is_extracted(self) -> None:
        html = b"<html><body><img src='x.png'/></body></html>"
        result = fetch.extract("text/html", html)
        self.assertEqual(result, ("text/html", html))

    def test_extract_returns_markdown_when_text_is_extracted(self) -> None:
        html = b"<html><body><article>Real content here.</article></body></html>"
        content_type, data = fetch.extract("text/html; charset=utf-8", html)
        self.assertEqual(content_type, "text/markdown")
        self.assertIn(b"Real content here.", data)

    def test_extract_falls_back_to_utf8_for_an_unknown_charset(self) -> None:
        html = b"<html><body><article><p>body text here</p></article></body></html>"
        content_type, data = fetch.extract(
            "text/html; charset=x-no-such-codec-phase11", html
        )
        self.assertEqual(content_type, "text/markdown")
        self.assertIn(b"body text here", data)

    # 16: is_url.

    def test_is_url(self) -> None:
        self.assertTrue(fetch.is_url("http://example.com"))
        self.assertTrue(fetch.is_url("https://example.com"))
        self.assertFalse(fetch.is_url("widget.txt"))
        self.assertFalse(fetch.is_url("/abs/path/widget.txt"))
        self.assertFalse(fetch.is_url("-"))
        self.assertFalse(fetch.is_url("file:///tmp/x"))

    # 17-20: regression coverage for findings 1-4.

    def test_a_control_character_in_the_content_type_header_is_flattened(
        self,
    ) -> None:
        server = self._serve()
        server.routes["/"] = lambda h: _respond(
            h, 200, b"hello", "text/plain; charset=utf-8; x=\x7f"
        )

        with self._public():
            _final_url, content_type, _data = fetch.fetch(server.url + "/")

        self.assertFalse(re.search(r"[\x00-\x1f\x7f]", content_type))

    def test_an_unparseable_content_type_header_is_not_propagated(self) -> None:
        server = self._serve()
        server.routes["/"] = lambda h: _respond(h, 200, b"hello", "banana")

        with self._public():
            _final_url, content_type, data = fetch.fetch(server.url + "/")

        self.assertEqual(content_type, "text/plain")
        self.assertEqual(data, b"hello")

    def test_extract_recovers_the_article_after_an_unclosed_furniture_tag(
        self,
    ) -> None:
        page = (
            b"<html><body><p>Skip to content</p><nav><ul><li>menu</li></ul>"
            b"<article>THE ENTIRE REAL BODY OF THE PAGE</article>"
            b"</body></html>"
        )

        content_type, data = fetch.extract("text/html", page)

        self.assertEqual(content_type, "text/markdown")
        self.assertIn(b"THE ENTIRE REAL BODY OF THE PAGE", data)
        self.assertNotIn(b"Skip to content", data)

    def test_require_public_reports_a_malformed_ipv6_url(self) -> None:
        with self.assertRaises(fetch.FetchError) as ctx:
            fetch._require_public("http://[::1")
        self.assertIn("malformed url", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
