"""feeds: turn an RSS, Atom, or JSON Feed document into a list of item
urls. `parse` is pure; `load` is the one seam that talks to `fetch`.
"""
from __future__ import annotations

import sys
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "llm-wiki"))
from llmwiki import feeds, fetch  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "feeds"
PUBLIC_ADDRESS = "93.184.216.34"  # example.com's old address: is_global True


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class ParseRssTest(unittest.TestCase):
    def test_yields_items_with_url_and_title(self) -> None:
        items = feeds.parse(_read("rss.xml"))
        self.assertEqual(
            items,
            [
                feeds.Item("https://example.test/rss/first", "First Post"),
                feeds.Item("https://example.test/rss/second", "Second Post"),
            ],
        )

    def test_namespaced_document_still_dispatches_by_local_name(self) -> None:
        items = feeds.parse(_read("rss-namespaced.xml"))
        self.assertEqual(
            items, [feeds.Item("https://example.test/rss-ns/first", "Namespaced Post")]
        )


class ParseAtomTest(unittest.TestCase):
    def test_namespaced_document_dispatches_by_local_name(self) -> None:
        items = feeds.parse(_read("atom.xml"))
        self.assertEqual(len(items), 2)

    def test_picks_the_alternate_link_among_several(self) -> None:
        items = feeds.parse(_read("atom.xml"))
        first = items[0]
        self.assertEqual(first.url, "https://example.test/atom/first")
        self.assertEqual(first.title, "First Entry")

    def test_a_link_with_no_rel_counts_as_alternate(self) -> None:
        items = feeds.parse(_read("atom.xml"))
        second = items[1]
        self.assertEqual(second.url, "https://example.test/atom/second")


class ParseJsonFeedTest(unittest.TestCase):
    def test_yields_items_with_url_and_title(self) -> None:
        items = feeds.parse(_read("jsonfeed.json"))
        self.assertEqual(
            items,
            [
                feeds.Item("https://example.test/jsonfeed/first", "First Item"),
                feeds.Item("https://example.test/jsonfeed/second", "Second Item"),
            ],
        )

    def test_an_item_with_no_url_is_skipped_not_an_error(self) -> None:
        data = (
            b'{"items": ['
            b'{"title": "No url here"},'
            b'{"url": "https://example.test/only", "title": "Only One"}'
            b"]}"
        )
        items = feeds.parse(data)
        self.assertEqual(items, [feeds.Item("https://example.test/only", "Only One")])

    def test_a_missing_title_is_the_empty_string(self) -> None:
        data = b'{"items": [{"url": "https://example.test/untitled"}]}'
        items = feeds.parse(data)
        self.assertEqual(items, [feeds.Item("https://example.test/untitled", "")])

    def test_a_non_string_url_is_skipped_not_an_error(self) -> None:
        data = (
            b'{"items": ['
            b'{"url": 12345, "title": "numeric url"},'
            b'{"url": {"a": 1}, "title": "object url"},'
            b'{"url": "https://example.test/good", "title": "Good"}'
            b"]}"
        )
        items = feeds.parse(data)
        self.assertEqual(items, [feeds.Item("https://example.test/good", "Good")])

    def test_a_non_string_title_is_the_empty_string(self) -> None:
        data = b'{"items": [{"url": "https://example.test/untitled", "title": 42}]}'
        items = feeds.parse(data)
        self.assertEqual(items, [feeds.Item("https://example.test/untitled", "")])


class ParseErrorTest(unittest.TestCase):
    def test_unknown_root_element_names_the_reason(self) -> None:
        with self.assertRaises(feeds.FeedError) as ctx:
            feeds.parse(_read("unknown-root.xml"))
        self.assertIn("urlset", str(ctx.exception))

    def test_zero_items_raises_feed_error(self) -> None:
        with self.assertRaises(feeds.FeedError) as ctx:
            feeds.parse(_read("zero-items.xml"))
        self.assertIn("zero items", str(ctx.exception))

    def test_zero_items_from_a_json_feed_also_raises(self) -> None:
        with self.assertRaises(feeds.FeedError) as ctx:
            feeds.parse(b'{"items": []}')
        self.assertIn("zero items", str(ctx.exception))

    def test_a_doctype_is_refused_before_reaching_the_xml_parser(self) -> None:
        with unittest.mock.patch.object(
            feeds.ElementTree, "fromstring"
        ) as mock_fromstring:
            with self.assertRaises(feeds.FeedError) as ctx:
                feeds.parse(_read("doctype.xml"))
        mock_fromstring.assert_not_called()
        self.assertIn("DOCTYPE", str(ctx.exception))

    def test_malformed_xml_names_the_reason(self) -> None:
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"<rss><channel><item>")

    def test_a_json_feed_that_is_not_an_object_is_a_feed_error(self) -> None:
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"[1, 2, 3]")


class _FeedServer:
    """A local http server for one test, copying test_fetch.py's harness
    since fake_endpoint.py only answers JSON on POST."""

    def __init__(self, body: bytes, content_type: str) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}/feed"
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


class LoadTest(unittest.TestCase):
    def test_load_fetches_under_feed_types_then_parses(self) -> None:
        server = _FeedServer(_read("rss.xml"), "application/rss+xml")
        self.addCleanup(server.close)

        with unittest.mock.patch.object(
            fetch, "_resolved_addresses", return_value=[PUBLIC_ADDRESS]
        ):
            items = feeds.load(server.url)

        self.assertEqual(
            items,
            [
                feeds.Item("https://example.test/rss/first", "First Post"),
                feeds.Item("https://example.test/rss/second", "Second Post"),
            ],
        )

    def test_load_lets_a_fetch_error_out(self) -> None:
        with self.assertRaises(fetch.FetchError):
            feeds.load("http://127.0.0.1/blocked")


if __name__ == "__main__":
    unittest.main()
