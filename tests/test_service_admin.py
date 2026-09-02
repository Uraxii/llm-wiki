"""The three phase 6 admin routes: mint, list, revoke.

Driven through the real ASGI app `app.build_app` returns, the same way
`tests/test_service_routes.py` does: a full scope, a `receive` that
hands over the request body once, and a `send` that records every
message. Never `starlette.testclient.TestClient`, which bypasses the
middleware stack uvicorn actually runs.

Nothing here needs a network or a paid credential: no admin route calls
a model, so the whole phase is testable against a temporary state
directory and a fixture kb.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmwiki.core import render_frontmatter  # noqa: E402
from llmwiki_service import admin, app, auth, tokens  # noqa: E402

PEPPERS = tokens.Peppers({1: b"pepper-one"})
BOOTSTRAP = "bootstrap-secret-value"
TOKEN_ACCESS = {"read": "token", "write": "token", "admin": "token"}
CALLER = ("203.0.113.5", 1)


async def _call(
    asgi_app: app.ASGIApp,
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    client: tuple[str, int] | None = CALLER,
) -> tuple[int, bytes, dict[str, str]]:
    """Drive `asgi_app` the way uvicorn does. Returns (status, body,
    response headers lower-cased)."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "scheme": "http",
        "method": method,
        # uvicorn percent-decodes the target into "path" and keeps
        # "raw_path" as the wire bytes, so a %0A in a label arrives at
        # the route as a real newline.
        "path": unquote(raw_path),
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": headers or [],
        "client": client,
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await asgi_app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    resp_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    payload = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return start["status"], payload, resp_headers


def _bearer(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


def _json_body(label: object, role: object) -> bytes:
    return json.dumps({"label": label, "role": role}).encode()


class AdminTestCase(unittest.IsolatedAsyncioTestCase):
    """Fixture kb, token store, and a fresh limiter per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.kb = self.tmp / "kb"
        for sub in ("wiki", "sources", "vectors"):
            (self.kb / sub).mkdir(parents=True)
        (self.kb / "config.toml").write_text("")
        (self.kb / "wiki" / "page.md").write_text(
            render_frontmatter({"kind": "topic", "title": "T"}, "Body.\n")
        )
        self.db = self.tmp / "tokens.sqlite"
        self.store = tokens.TokenStore(self.db, PEPPERS)
        self.failure_limiter = auth.FailureLimiter()
        self.search_limiter = auth.SearchLimiter(30)

    def build(self, bootstrap: str | None = BOOTSTRAP, access=None) -> app.ASGIApp:
        deployment = {
            "access": access or TOKEN_ACCESS,
            "kbs": {"demo": {"path": str(self.kb)}},
        }
        return app.build_app(
            None,
            deployment,
            self.store,
            self.failure_limiter,
            self.search_limiter,
            bootstrap,
        )

    async def mint(self, app_, label="laptop", role="reader", headers=None):
        return await _call(
            app_,
            "POST",
            "/admin/tokens",
            headers=_bearer(BOOTSTRAP) if headers is None else headers,
            body=_json_body(label, role),
        )

    def labels(self) -> list[str]:
        return [row.label for row in self.store.list_tokens()]


class MintRouteTest(AdminTestCase):
    async def test_the_bootstrap_credential_mints(self) -> None:
        status, body, _ = await self.mint(self.build())
        self.assertEqual(status, 201)
        payload = json.loads(body)
        self.assertEqual(payload["label"], "laptop")
        self.assertEqual(payload["role"], "reader")
        self.assertEqual(payload["id"], tokens.token_id(payload["token"]))
        self.assertEqual(
            payload["created_at"], self.store.list_tokens()[0].created_at
        )
        self.assertEqual(
            sorted(payload), ["created_at", "id", "label", "role", "token"]
        )

    async def test_the_minted_token_authenticates_a_read_route(self) -> None:
        """The whole point of the phase, and the first thing to break if
        the gate is wired wrong."""
        wrapped = self.build()
        _status, body, _ = await self.mint(wrapped)
        token = json.loads(body)["token"]

        status, listing, _ = await _call(
            wrapped, "GET", "/kb/demo/list", headers=_bearer(token)
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [page["name"] for page in json.loads(listing)["pages"]], ["page.md"]
        )

    async def test_a_minted_admin_token_also_mints(self) -> None:
        wrapped = self.build()
        _status, body, _ = await self.mint(wrapped, label="ops", role="admin")
        admin_token = json.loads(body)["token"]

        status, _body, _ = await self.mint(
            wrapped, label="second", headers=_bearer(admin_token)
        )
        self.assertEqual(status, 201)

    async def test_the_bootstrap_credential_is_not_the_only_key(self) -> None:
        """A deployment that unset the variable and restarted still
        administers itself through a minted admin token."""
        admin_token = self.store.mint("ops", "admin")
        wrapped = self.build(bootstrap=None)
        status, _body, _ = await self.mint(
            wrapped, headers=_bearer(admin_token)
        )
        self.assertEqual(status, 201)

    async def test_a_writer_and_a_reader_get_the_absent_credential_body(self) -> None:
        wrapped = self.build()
        absent = await _call(
            wrapped, "POST", "/admin/tokens", body=_json_body("x", "reader")
        )
        self.assertEqual(absent[0], 401)
        for role in ("writer", "reader"):
            with self.subTest(role=role):
                token = self.store.mint(f"{role}-key", role)
                status, body, _ = await self.mint(
                    wrapped, headers=_bearer(token)
                )
                self.assertEqual(status, 401)
                self.assertEqual(body, absent[1])
        self.assertEqual(self.labels(), ["reader-key", "writer-key"])

    async def test_a_credential_one_byte_off_is_refused(self) -> None:
        status, body, _ = await self.mint(
            self.build(), headers=_bearer(BOOTSTRAP + "x")
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})

    async def test_a_non_ascii_credential_is_401_and_not_500(self) -> None:
        """`secrets.compare_digest` raises `TypeError` on a non-ASCII
        string, so a comparison on strings would 500 here."""
        for junk in ("café" * 9, "\U0001f511" * 10):
            with self.subTest(junk=junk):
                status, body, _ = await self.mint(
                    self.build(), headers=_bearer(junk)
                )
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body), {"error": "unauthorized"})

    async def test_the_eleventh_failure_is_rate_limited(self) -> None:
        wrapped = self.build()
        for _ in range(auth.MAX_FAILURES_PER_WINDOW):
            status, _body, _ = await self.mint(
                wrapped, headers=_bearer(BOOTSTRAP + "x")
            )
            self.assertEqual(status, 401)
        status, body, headers = await self.mint(
            wrapped, headers=_bearer(BOOTSTRAP + "x")
        )
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body), {"error": "rate_limited"})
        self.assertGreater(int(headers["retry-after"]), 0)

    async def test_the_admin_gate_and_the_read_gate_share_one_budget(self) -> None:
        wrapped = self.build()
        for _ in range(auth.MAX_FAILURES_PER_WINDOW + 1):
            await self.mint(wrapped, headers=_bearer(BOOTSTRAP + "x"))
        status, _body, headers = await _call(
            wrapped, "GET", "/kb/demo/search?q=x", headers=_bearer("rubbish")
        )
        self.assertEqual(status, 429)
        self.assertIn("retry-after", headers)

    async def test_a_duplicate_label_is_409_and_leaves_one_row(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        status, body, _ = await self.mint(wrapped, role="writer")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "duplicate_label"})
        self.assertEqual(self.labels(), ["laptop"])
        self.assertEqual(self.store.list_tokens()[0].role, "reader")

    async def test_a_bad_label_is_400_and_writes_no_row(self) -> None:
        wrapped = self.build()
        cases = {
            "over the cap": "a" * (tokens.MAX_LABEL_CHARS + 1),
            "whitespace only": "  ",
            "trailing space": "ops ",
            "line separator": "ops\u2028here",
            "newline": "ops\nhere",
            "not a string": 7,
            "absent": None,
        }
        for name, label in cases.items():
            with self.subTest(case=name):
                status, body, _ = await self.mint(wrapped, label=label)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "invalid_label"})
        self.assertEqual(self.labels(), [])

    async def test_a_label_at_the_cap_is_accepted(self) -> None:
        status, _body, _ = await self.mint(
            self.build(), label="a" * tokens.MAX_LABEL_CHARS
        )
        self.assertEqual(status, 201)

    async def test_a_bad_role_is_400_invalid_role(self) -> None:
        wrapped = self.build()
        for role in ("owner", None, "", 7, ["admin"]):
            with self.subTest(role=role):
                status, body, _ = await self.mint(wrapped, role=role)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "invalid_role"})
        self.assertEqual(self.labels(), [])

    async def test_every_declared_role_can_be_minted(self) -> None:
        wrapped = self.build()
        for role in tokens.ROLES:
            with self.subTest(role=role):
                status, _body, _ = await self.mint(
                    wrapped, label=f"{role}-key", role=role
                )
                self.assertEqual(status, 201)

    async def test_a_body_that_is_not_a_json_object_is_400_invalid_label(self) -> None:
        wrapped = self.build()
        for name, body in {
            "a list": b'["laptop", "reader"]',
            "a string": b'"laptop"',
            "a number": b"7",
            "null": b"null",
            "not json": b"laptop=reader",
            "empty": b"",
            "not utf-8": b'{"label": "\xff", "role": "reader"}',
        }.items():
            with self.subTest(case=name):
                status, payload, _ = await _call(
                    wrapped,
                    "POST",
                    "/admin/tokens",
                    headers=_bearer(BOOTSTRAP),
                    body=body,
                )
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(payload), {"error": "invalid_label"})
        self.assertEqual(self.labels(), [])

    async def test_an_unauthenticated_bad_body_never_reaches_the_body(self) -> None:
        """Token before body, the same order `routes.py` puts the token
        before the kb name: an unauthenticated caller learns nothing
        about which requests are well formed."""
        status, body, _ = await _call(
            self.build(), "POST", "/admin/tokens", body=b"not json"
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})


class SecretDisclosureTest(AdminTestCase):
    async def test_the_secret_is_in_the_201_body_exactly_once(self) -> None:
        _status, body, _ = await self.mint(self.build())
        token = json.loads(body)["token"]
        self.assertEqual(body.decode().count(token), 1)

    async def test_no_log_line_from_a_mint_holds_the_secret(self) -> None:
        """Phase 1's open verification bullet, driven: every record the
        `llmwiki_service` logger emits during a mint is searched for the
        minted secret and holds it zero times."""
        wrapped = self.build()
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            _status, body, _ = await self.mint(wrapped)
        token = json.loads(body)["token"]
        for record in captured.records:
            with self.subTest(message=record.getMessage()):
                self.assertNotIn(token, record.getMessage())
                self.assertNotIn(token, str(record.args))
                self.assertNotIn(BOOTSTRAP, record.getMessage())
                self.assertNotIn(BOOTSTRAP, str(record.args))

    async def test_a_minted_admin_is_named_by_its_own_label(self) -> None:
        wrapped = self.build()
        _status, body, _ = await self.mint(wrapped, label="ops", role="admin")
        admin_token = json.loads(body)["token"]
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            await self.mint(wrapped, label="second", headers=_bearer(admin_token))
        self.assertIn("by=ops", captured.output[-1])

    async def test_a_refused_line_names_the_client_key_never_the_label(self) -> None:
        """A rejected label is unvalidated text: it must not reach a log
        line, where a newline in it would forge a second one."""
        wrapped = self.build()
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            await self.mint(wrapped, label="forged\nadmin mint label=ops")
        self.assertIn(
            f"admin refused code=invalid_label by={CALLER[0]}", captured.output[-1]
        )
        self.assertNotIn("forged", "\n".join(captured.output))

    async def test_a_listing_holds_neither_the_plaintext_nor_the_hash(self) -> None:
        wrapped = self.build()
        _status, body, _ = await self.mint(wrapped)
        token = json.loads(body)["token"]
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            (stored_hash,) = conn.execute(
                "SELECT token_hash FROM tokens"
            ).fetchone()

        status, listing, _ = await _call(
            wrapped, "GET", "/admin/tokens", headers=_bearer(BOOTSTRAP)
        )
        self.assertEqual(status, 200)
        self.assertNotIn(token, listing.decode())
        self.assertNotIn(stored_hash, listing.decode())

    async def test_the_store_never_holds_the_plaintext(self) -> None:
        _status, body, _ = await self.mint(self.build())
        token = json.loads(body)["token"]
        self.assertNotIn(token, self.db.read_bytes().decode("latin-1"))


class LogContractTest(AdminTestCase):
    """The three log formats, asserted whole rather than by substring.
    A substring assertion passes on a message with text added around
    it, and it passes on `by=None`, so it would not notice the client
    key going missing from the one line that names the caller."""

    async def one_line(self, expected: str, awaitable) -> object:
        """Run `awaitable` and assert it wrote exactly one line naming
        an admin operation, byte for byte `expected`."""
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            result = await awaitable
        lines = [
            record.getMessage()
            for record in captured.records
            if "admin" in record.getMessage()
        ]
        self.assertEqual(lines, [expected])
        return result

    async def test_the_mint_line_names_the_id_the_label_and_the_caller(self) -> None:
        wrapped = self.build()
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            _status, body, _ = await self.mint(wrapped)
        payload = json.loads(body)
        lines = [
            record.getMessage()
            for record in captured.records
            if "admin" in record.getMessage()
        ]
        self.assertEqual(
            lines,
            [
                f"admin mint label=laptop role=reader id={payload['id']} "
                "by=bootstrap"
            ],
        )

    async def test_the_revoke_line_is_exact(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        await self.one_line(
            "admin revoke label=laptop by=bootstrap",
            _call(
                wrapped,
                "DELETE",
                "/admin/tokens/laptop",
                headers=_bearer(BOOTSTRAP),
            ),
        )

    async def test_every_refusal_line_is_exact(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped, label="taken")
        cases = [
            (
                "unauthorized",
                self.mint(wrapped, label="a", headers=_bearer("rubbish")),
            ),
            (
                "invalid_label",
                _call(
                    wrapped,
                    "POST",
                    "/admin/tokens",
                    headers=_bearer(BOOTSTRAP),
                    body=b"not json",
                ),
            ),
            ("invalid_label", self.mint(wrapped, label=7)),
            ("invalid_label", self.mint(wrapped, label="ops ")),
            ("invalid_role", self.mint(wrapped, label="a", role="owner")),
            ("duplicate_label", self.mint(wrapped, label="taken")),
            (
                "not_found",
                _call(
                    wrapped,
                    "DELETE",
                    "/admin/tokens/never-minted",
                    headers=_bearer(BOOTSTRAP),
                ),
            ),
        ]
        for code, awaitable in cases:
            with self.subTest(code=code):
                await self.one_line(
                    f"admin refused code={code} by={CALLER[0]}", awaitable
                )

    async def test_a_refused_listing_and_revoke_name_the_caller_too(self) -> None:
        """The gate refusal on all three routes, not just the mint."""
        wrapped = self.build()
        token = self.store.mint("reader-key", "reader")
        expected = f"admin refused code=unauthorized by={CALLER[0]}"
        await self.one_line(
            expected,
            _call(wrapped, "GET", "/admin/tokens", headers=_bearer(token)),
        )
        await self.one_line(
            expected,
            _call(
                wrapped,
                "DELETE",
                "/admin/tokens/reader-key",
                headers=_bearer(token),
            ),
        )

    async def test_the_rate_limited_line_is_exact(self) -> None:
        wrapped = self.build()
        for _ in range(auth.MAX_FAILURES_PER_WINDOW):
            await self.mint(wrapped, headers=_bearer("rubbish"))
        await self.one_line(
            f"admin refused code=rate_limited by={CALLER[0]}",
            self.mint(wrapped, headers=_bearer("rubbish")),
        )

    async def test_a_caller_with_no_transport_peer_is_named_unknown(self) -> None:
        """`request.client` is `None` on a connection with no peer, and
        the refused line still has to name something."""
        wrapped = self.build()
        await self.one_line(
            "admin refused code=unauthorized by=unknown",
            _call(
                wrapped,
                "POST",
                "/admin/tokens",
                headers=_bearer("rubbish"),
                body=_json_body("laptop", "reader"),
                client=None,
            ),
        )

    async def test_a_refused_listing_is_counted_against_the_caller(self) -> None:
        """The client key the limiter counts on is the caller's address,
        not one key shared by every caller of this route."""
        wrapped = self.build()
        await _call(wrapped, "GET", "/admin/tokens", headers=_bearer("rubbish"))
        self.assertEqual(self.failure_limiter.tracked(), [CALLER[0]])

    async def test_a_refused_revoke_is_counted_against_the_caller(self) -> None:
        wrapped = self.build()
        await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer("rubbish")
        )
        self.assertEqual(self.failure_limiter.tracked(), [CALLER[0]])


class ListRouteTest(AdminTestCase):
    async def test_the_listing_shape_is_the_disclosable_columns(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        _status, listing, _ = await _call(
            wrapped, "GET", "/admin/tokens", headers=_bearer(BOOTSTRAP)
        )
        (row,) = json.loads(listing)["tokens"]
        self.assertEqual(
            sorted(row),
            ["created_at", "id", "label", "last_used_at", "revoked_at", "role"],
        )
        self.assertEqual(row["label"], "laptop")
        self.assertIsNone(row["last_used_at"])
        self.assertIsNone(row["revoked_at"])

    async def test_every_row_is_returned_oldest_first(self) -> None:
        wrapped = self.build()
        for label in ("one", "two", "three"):
            await self.mint(wrapped, label=label)
        _status, listing, _ = await _call(
            wrapped, "GET", "/admin/tokens", headers=_bearer(BOOTSTRAP)
        )
        self.assertEqual(
            [row["label"] for row in json.loads(listing)["tokens"]],
            [row.label for row in self.store.list_tokens()],
        )

    async def test_an_empty_table_lists_nothing_rather_than_failing(self) -> None:
        status, listing, _ = await _call(
            self.build(), "GET", "/admin/tokens", headers=_bearer(BOOTSTRAP)
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(listing), {"tokens": []})

    async def test_a_reader_token_cannot_list(self) -> None:
        token = self.store.mint("reader-key", "reader")
        status, body, _ = await _call(
            self.build(), "GET", "/admin/tokens", headers=_bearer(token)
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})

    async def test_a_successful_listing_logs_no_admin_line(self) -> None:
        """At most one line per request, and a listing has none of the
        three formats to write."""
        wrapped = self.build()
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            await _call(
                wrapped, "GET", "/admin/tokens", headers=_bearer(BOOTSTRAP)
            )
        self.assertNotIn("admin ", "\n".join(captured.output))


class RevokeRouteTest(AdminTestCase):
    async def test_revoke_answers_204_with_no_body(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        status, body, _ = await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(BOOTSTRAP)
        )
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    async def test_a_revoked_token_stops_working_on_a_read_route(self) -> None:
        wrapped = self.build()
        _status, body, _ = await self.mint(wrapped)
        token = json.loads(body)["token"]
        before, _listing, _ = await _call(
            wrapped, "GET", "/kb/demo/list", headers=_bearer(token)
        )
        self.assertEqual(before, 200)

        await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(BOOTSTRAP)
        )
        after, refused, _ = await _call(
            wrapped, "GET", "/kb/demo/list", headers=_bearer(token)
        )
        self.assertEqual(after, 401)
        self.assertEqual(json.loads(refused), {"error": "unauthorized"})

    async def test_a_revoked_row_still_lists_with_a_revoked_at(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(BOOTSTRAP)
        )
        _status, listing, _ = await _call(
            wrapped, "GET", "/admin/tokens", headers=_bearer(BOOTSTRAP)
        )
        (row,) = json.loads(listing)["tokens"]
        self.assertEqual(row["label"], "laptop")
        self.assertIsNotNone(row["revoked_at"])

    async def test_a_second_revoke_and_an_unknown_label_both_404(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(BOOTSTRAP)
        )
        second = await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(BOOTSTRAP)
        )
        unknown = await _call(
            wrapped, "DELETE", "/admin/tokens/never-minted", headers=_bearer(BOOTSTRAP)
        )
        self.assertEqual(second[0], 404)
        self.assertEqual(unknown[0], 404)
        self.assertEqual(second[1], unknown[1])
        self.assertEqual(json.loads(second[1]), {"error": "not_found"})

    async def test_a_reader_token_cannot_revoke(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        token = self.store.mint("reader-key", "reader")
        status, _body, _ = await _call(
            wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(token)
        )
        self.assertEqual(status, 401)
        self.assertIsNone(self.store.list_tokens()[0].revoked_at)

    async def test_the_revoke_line_names_the_label_and_the_caller(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            await _call(
                wrapped, "DELETE", "/admin/tokens/laptop", headers=_bearer(BOOTSTRAP)
            )
        self.assertIn("admin revoke label=laptop by=bootstrap", captured.output[-1])

    async def test_a_label_that_could_forge_a_line_never_reaches_one(self) -> None:
        """No row can carry such a label, because `mint` refuses it, so
        the revoke never succeeds and only the refused line is written,
        which names the client key instead."""
        wrapped = self.build()
        with self.assertLogs("llmwiki_service", level="INFO") as captured:
            status, _body, _ = await _call(
                wrapped,
                "DELETE",
                "/admin/tokens/ops%0Aadmin revoke label=forged",
                headers=_bearer(BOOTSTRAP),
            )
        self.assertEqual(status, 404)
        self.assertNotIn("forged", "\n".join(captured.output))


class RouterTest(AdminTestCase):
    """What the router answers for a path or method no admin route
    defines. Both come from `app._router_error`, on the closed set."""

    async def test_there_is_no_route_that_returns_a_token_by_id(self) -> None:
        wrapped = self.build()
        await self.mint(wrapped)
        for method in ("GET", "POST", "PUT", "PATCH"):
            with self.subTest(method=method):
                status, body, _ = await _call(
                    wrapped,
                    method,
                    "/admin/tokens/laptop",
                    headers=_bearer(BOOTSTRAP),
                )
                # 405, not 404: the DELETE route claims this path, so
                # starlette's router partial-matches it and raises
                # method-not-allowed before any handler runs. The phase
                # 6 spec predicts 404 for GET, which is wrong against
                # starlette 1.6.0; what it asks for is that no route
                # returns a token by id, and no method here does.
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body), {"error": "method_not_allowed"})

    async def test_an_unknown_admin_path_is_404_on_the_closed_set(self) -> None:
        status, body, _ = await _call(
            self.build(), "GET", "/admin/whatever", headers=_bearer(BOOTSTRAP)
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not_found"})

    async def test_the_collection_path_takes_get_and_post_only(self) -> None:
        wrapped = self.build()
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, body, _ = await _call(
                    wrapped, method, "/admin/tokens", headers=_bearer(BOOTSTRAP)
                )
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body), {"error": "method_not_allowed"})


class WiringTest(AdminTestCase):
    async def test_build_app_serves_all_three_admin_routes(self) -> None:
        paths = {
            (route.path, tuple(sorted(route.methods - {"HEAD"})))
            for route in admin.make_admin_routes({}, self.store, self.failure_limiter, None)
        }
        self.assertEqual(
            paths,
            {
                ("/admin/tokens", ("POST",)),
                ("/admin/tokens", ("GET",)),
                ("/admin/tokens/{label}", ("DELETE",)),
            },
        )

    async def test_access_admin_open_does_not_open_the_routes(self) -> None:
        """Startup refusal row 14 refuses this file, and the gate ignores
        the value even if one slipped past."""
        wrapped = self.build(
            bootstrap=None, access={"read": "open", "write": "open", "admin": "open"}
        )
        status, body, _ = await _call(
            wrapped, "POST", "/admin/tokens", body=_json_body("x", "reader")
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertEqual(self.labels(), [])

    async def test_health_still_answers_beside_the_admin_routes(self) -> None:
        status, body, _ = await _call(self.build(), "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")


if __name__ == "__main__":
    unittest.main()
