"""Local test double for a wiki service, built like `fake_endpoint.py`.

Serves phase 3's read routes on a background thread bound to port 0 (the
OS picks a free port) and records every request it received, so a test
can point `llmwiki.remotes` at a real socket without a real network
call.

    def respond(request: Request) -> Reply:
        return Reply(body=b'{"hits": []}')

    with FakeWiki(respond) as wiki:
        remote = Remote("homelab", wiki.url + "/kb/homelab", None)
        search_one(remote, "cold", 10, None)
        wiki.requests[0].path     # "/kb/homelab/search"
        wiki.requests[0].query    # {"q": ["cold"], "n": ["10"]}
        wiki.requests[0].headers  # email.message.Message of request headers

`Reply` carries the status, the body bytes, and any extra headers, so a
test writes a 302 with a `Location`, a `text/html` page, or a
`Content-Length` that understates the body it actually sends. Requests
are served one thread per connection, so a handler may block inside a
`threading.Event` to hold one request open past a client timeout while
other requests arrive.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit


class Request(NamedTuple):
    """One request `FakeWiki` received."""

    path: str
    query: dict[str, list[str]]
    headers: object


class Reply(NamedTuple):
    """One response `FakeWiki` sends. `headers` override the defaults,
    `Content-Length` included, so a test can understate the body.
    `length_header=False` sends no `Content-Length` at all, leaving the
    connection close to delimit the body, which is how a client meets a
    body larger than any header promised."""

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: Mapping[str, str] = MappingProxyType({})
    length_header: bool = True


class FakeWiki:
    """A local wiki service double."""

    def __init__(self, respond: Callable[[Request], Reply]) -> None:
        self.requests: list[Request] = []
        wiki = self
        requests_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API name)
                parts = urlsplit(self.path)
                request = Request(
                    parts.path, parse_qs(parts.query), self.headers
                )
                with requests_lock:
                    wiki.requests.append(request)
                reply = respond(request)
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                given = reply.headers
                if reply.length_header and "Content-Length" not in given:
                    self.send_header("Content-Length", str(len(reply.body)))
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(reply.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # a client that stopped reading, e.g. at a cap

            def log_message(self, format: str, *args) -> None:
                pass  # keep test output free of one line per request

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the server and join its thread. Safe to register with
        `addCleanup`; a leaked thread across the suite is a defect."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeWiki":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
