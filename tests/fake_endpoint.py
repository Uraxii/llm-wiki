"""Local test double for the configured API endpoint. Runs `http.server`
on a background thread bound to port 0 (the OS picks a free port), so a
test can point `llmwiki.model` at a real socket without a real network
call. Reused by every later phase's tests that need a model.

    def respond(path: str, body: dict) -> dict:
        return {"choices": [{"message": {"content": "hi"}}]}

    with FakeEndpoint(respond) as fake:
        config = {"endpoint": {"url": fake.url}, ...}
        chat(config, "summarize", "hello")
        fake.requests[0].path      # "/chat/completions"
        fake.requests[0].method    # "POST"
        fake.requests[0].body      # parsed JSON request body
        fake.requests[0].headers   # email.message.Message of request headers

`status` (default 200) is fixed for the life of one `FakeEndpoint`; a test
that needs a failing endpoint constructs a second instance with
`status=500`. `headers` (default none) are extra response headers merged
into every response, e.g. `headers={"Location": other.url}` to act as a
redirector in front of a second `FakeEndpoint`.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer


class Request:
    """One request `FakeEndpoint` received."""

    def __init__(self, path: str, method: str, body: dict, headers) -> None:
        self.path = path
        self.method = method
        self.body = body
        self.headers = headers


class FakeEndpoint:
    """A local OpenAI-compatible endpoint double."""

    def __init__(
        self,
        respond: Callable[[str, dict], dict],
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[Request] = []
        self.url = ""
        fake = self
        extra_headers = headers or {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server API name)
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                body = json.loads(raw) if raw else {}
                fake.requests.append(
                    Request(self.path, self.command, body, self.headers)
                )
                payload = json.dumps(respond(self.path, body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in extra_headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:
                pass  # keep test output free of one line per request

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
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

    def __enter__(self) -> "FakeEndpoint":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
