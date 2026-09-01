"""The four phase 3 read routes, and `auth.SearchLimiter`.

Driven through the real ASGI app `app.build_app` returns, the same way
`test_service_app.py`'s `BuildAppTest` does: a full scope, a `receive`
that hands over an empty body once, and a `send` that records every
message. Never `starlette.testclient.TestClient`, per the phase 3
brief: a live subprocess hand-drive is a separate proof this file does
not attempt.

`search`'s 200 and 502 paths are tested by patching
`llmwiki_service.routes.rank` directly rather than driving a real model
call, per this repo's hermetic-suite rule: no test reaches the real
network. Patching at that seam is also the acceptance proof phase 3
asks for: that the route reaches ranking through `llmwiki.vectors.rank`
and never reimplements it or shells out to the CLI.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llmwiki.core import Kb, render_frontmatter  # noqa: E402
from llmwiki.model import ModelError  # noqa: E402
from llmwiki.vectors import Hit, NoEmbedModel, Ranking, StaleVectors  # noqa: E402
from llmwiki_service import app, auth, routes, tokens  # noqa: E402
from fake_endpoint import FakeEndpoint  # noqa: E402

PEPPERS = tokens.Peppers({1: b"pepper-one"})
OPEN_ACCESS = {"read": "open", "write": "token", "admin": "token"}
TOKEN_ACCESS = {"read": "token", "write": "token", "admin": "token"}


def _write_kb(root: Path, *, embed_model: str | None = None, endpoint_url: str | None = None) -> Path:
    """Lays out an empty kb, or, called again on the same `root` (as
    the search fault tests do, to change `config.toml` after `setUp`
    already built one), just rewrites `config.toml`."""
    for sub in ("wiki", "sources", "vectors"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    lines = []
    if embed_model is not None:
        lines.append(f'[models]\nembed = "{embed_model}"\n')
    if endpoint_url is not None:
        lines.append(f'[endpoint]\nurl = "{endpoint_url}"\n')
    (root / "config.toml").write_text("\n".join(lines))
    return root


def _write_page(
    root: Path,
    filename: str,
    *,
    kind: str = "topic",
    title: str = "Title",
    body: str = "Body text.\n",
) -> Path:
    path = root / "wiki" / filename
    path.write_text(render_frontmatter({"kind": kind, "title": title}, body))
    return path


async def _call(
    asgi_app: app.ASGIApp,
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("203.0.113.5", 1),
) -> tuple[int, bytes, dict[str, str]]:
    """Drive `asgi_app` the way uvicorn does. Returns (status, body,
    response headers lower-cased)."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "scheme": "http",
        "method": method,
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": headers or [],
        "client": client,
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await asgi_app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    resp_headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], body, resp_headers


def _bearer(token: str) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())]


class RouteTestCase(unittest.IsolatedAsyncioTestCase):
    """Fixture kb, token store, and fresh limiters per test. `self.kb`
    is the kb root; `self.store` mints tokens against a tempdir
    sqlite file that no other test shares."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.kb = _write_kb(self.tmp / "kb")
        self.store = tokens.TokenStore(self.tmp / "tokens.sqlite", PEPPERS)
        self.failure_limiter = auth.FailureLimiter()
        self.search_limiter = auth.SearchLimiter(30)

    def deployment(self, access: dict = OPEN_ACCESS, kb: Path | None = None) -> dict:
        # `[limits]` is not read here: `routes.make_routes` only reads
        # `access` and `kbs` from the deployment dict. The search budget
        # is `search_limiter`'s own `limit`, built in __main__._serve
        # from `[limits] search_per_minute`; a test that wants a tight
        # budget passes `search_limiter=auth.SearchLimiter(n)` directly.
        return {"access": access, "kbs": {"demo": {"path": str(kb or self.kb)}}}

    def build(self, **kw) -> app.ASGIApp:
        search_limiter = kw.pop("search_limiter", self.search_limiter)
        return app.build_app(
            None, self.deployment(**kw), self.store, self.failure_limiter, search_limiter
        )

    def reader_token(self) -> str:
        return self.store.mint("reader-key", "reader")


class TokenBeforeKbNameOrderTest(RouteTestCase):
    """Phase 3's own rule: under `read = "open"` an unknown kb name is
    just a 404. Under `read = "token"` the token check runs first, so
    a caller with no token gets the identical 401 whatever kb name it
    wrote, and only a valid token ever reaches the kb lookup at all."""

    async def test_unknown_kb_404s_under_open_read(self) -> None:
        wrapped = self.build(access=OPEN_ACCESS)
        status, body, _ = await _call(wrapped, "GET", "/kb/nope/schema")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not_found"})

    async def test_unknown_and_known_kb_answer_identically_with_no_token(self) -> None:
        wrapped = self.build(access=TOKEN_ACCESS)
        unknown = await _call(wrapped, "GET", "/kb/nope/schema")
        known = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(unknown[:2], known[:2])
        self.assertEqual(unknown[0], 401)
        self.assertEqual(json.loads(unknown[1]), {"error": "unauthorized"})


class UnauthorizedBodyTest(RouteTestCase):
    """Every authentication failure this gate can produce answers with
    the identical 401 body, so a caller cannot separate "you are
    nobody" from "you are somebody with no access here"."""

    async def test_absent_malformed_unknown_and_revoked_tokens_match(self) -> None:
        wrapped = self.build(access=TOKEN_ACCESS)
        revoked = self.store.mint("revoked-key", "reader")
        self.store.revoke("revoked-key")
        _unknown_id, unknown_token = tokens.new_token()  # well formed, never minted

        cases = {
            "absent": [],
            "malformed": _bearer("not-a-real-token"),
            "unknown_id": _bearer(unknown_token),
            "revoked": _bearer(revoked),
        }
        results = {}
        for name, headers in cases.items():
            status, body, _ = await _call(wrapped, "GET", "/kb/demo/schema", headers=headers)
            results[name] = (status, json.loads(body))
        for name, (status, payload) in results.items():
            self.assertEqual(status, 401, name)
            self.assertEqual(payload, {"error": "unauthorized"}, name)


class ReaderTokenAcceptedTest(RouteTestCase):
    async def test_all_four_routes_answer_200_for_a_reader_token(self) -> None:
        _write_page(self.kb, "alpha.md")
        (self.kb / "SCHEMA.md").write_text("# schema\n")
        wrapped = self.build(access=TOKEN_ACCESS)
        headers = _bearer(self.reader_token())

        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ):
            status, _, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x", headers=headers)
            self.assertEqual(status, 200)

        status, _, _ = await _call(wrapped, "GET", "/kb/demo/page/alpha.md", headers=headers)
        self.assertEqual(status, 200)
        status, _, _ = await _call(wrapped, "GET", "/kb/demo/list", headers=headers)
        self.assertEqual(status, 200)
        status, _, _ = await _call(wrapped, "GET", "/kb/demo/schema", headers=headers)
        self.assertEqual(status, 200)


class PageRouteTest(RouteTestCase):
    async def test_bytes_are_identical_including_a_non_utf8_byte(self) -> None:
        raw = b"---\ntitle: x\n---\n\nbody with \xff\xfe bad bytes\n"
        (self.kb / "wiki" / "raw.md").write_bytes(raw)
        wrapped = self.build()
        status, body, headers = await _call(wrapped, "GET", "/kb/demo/page/raw.md")
        self.assertEqual(status, 200)
        self.assertEqual(body, raw)
        self.assertEqual(headers["content-type"], "text/markdown")
        self.assertNotIn("charset", headers["content-type"])

    async def test_resolve_page_rejects_a_separator_a_nul_byte_and_a_wrong_suffix(self) -> None:
        # Direct proof of the parent-equality defense itself, independent
        # of any particular router's regex. Empirically, the installed
        # starlette (1.6.0) never hands page_route a "/" at all: {page}
        # compiles to `[^/]+`, so both a literal separator and a decoded
        # %2F fail the route's own match and 404 from the router before
        # _resolve_page runs (verified below). The spec still requires
        # _resolve_page to reject a "/" on its own terms, in case a
        # future router, or {page:path}, ever lets one through.
        kb = Kb(self.kb)
        self.assertIsNone(routes._resolve_page(kb, "sub/escaped.md"))  # is_relative_to would pass this
        self.assertIsNone(routes._resolve_page(kb, "bad\x00name.md"))
        _write_page(self.kb, "wrong-suffix.txt")
        self.assertIsNone(routes._resolve_page(kb, "wrong-suffix.txt"))
        _write_page(self.kb, "ok.md")
        self.assertIsNotNone(routes._resolve_page(kb, "ok.md"))

    async def test_a_literal_or_decoded_separator_404s_from_the_router_itself(self) -> None:
        wrapped = self.build()
        for path in ("/kb/demo/page/../config.toml", "/kb/demo/page/sub/page.md"):
            status, _, _ = await _call(wrapped, "GET", path)
            self.assertEqual(status, 404, path)

    async def test_an_embedded_nul_and_a_symlink_escape_404_through_the_real_route(self) -> None:
        real_root = self.tmp / "real-kb"
        _write_kb(real_root)
        _write_page(real_root, "legit.md")
        outside = self.tmp / "outside.md"
        outside.write_text("secret\n")
        os.symlink(outside, real_root / "wiki" / "escape.md")

        symlinked_root = self.tmp / "symlinked-kb"
        os.symlink(real_root, symlinked_root)
        wrapped = self.build(kb=symlinked_root)

        for name in ("bad\x00name.md", "escape.md"):
            status, body, _ = await _call(wrapped, "GET", f"/kb/demo/page/{name}")
            self.assertEqual(status, 404, name)
            self.assertEqual(json.loads(body), {"error": "not_found"}, name)

    async def test_a_legitimate_page_on_the_same_symlinked_deployment_answers_200(self) -> None:
        real_root = self.tmp / "real-kb"
        _write_kb(real_root)
        _write_page(real_root, "legit.md", title="Legit")
        symlinked_root = self.tmp / "symlinked-kb"
        os.symlink(real_root, symlinked_root)
        wrapped = self.build(kb=symlinked_root)

        status, body, headers = await _call(wrapped, "GET", "/kb/demo/page/legit.md")
        self.assertEqual(status, 200)
        self.assertIn(b"Legit", body)
        self.assertEqual(headers["content-type"], "text/markdown")


class ListRouteTest(RouteTestCase):
    NAMES = [f"page-{i:02d}.md" for i in range(30)]

    def _fill(self) -> None:
        for name in self.NAMES:
            _write_page(self.kb, name, title=name)

    async def test_paging_with_after_yields_every_name_once(self) -> None:
        self._fill()
        wrapped = self.build()
        seen: list[str] = []
        after = None
        for _ in range(len(self.NAMES) + 1):
            path = "/kb/demo/list?n=7"
            if after is not None:
                path += f"&after={after}"
            status, body, _ = await _call(wrapped, "GET", path)
            self.assertEqual(status, 200)
            payload = json.loads(body)
            seen.extend(page["name"] for page in payload["pages"])
            after = payload["next"]
            if after is None:
                break
        self.assertEqual(sorted(seen), sorted(self.NAMES))
        self.assertEqual(len(seen), len(set(seen)))

    async def test_n_bounds_are_enforced(self) -> None:
        self.assertEqual(routes._parse_n("1001", routes.DEFAULT_LIST_N), routes.MAX_N)
        self._fill()
        wrapped = self.build()
        status, _, _ = await _call(wrapped, "GET", "/kb/demo/list?n=1001")
        self.assertEqual(status, 200)
        for bad in ("0", "-1", "abc"):
            status, body, _ = await _call(wrapped, "GET", f"/kb/demo/list?n={bad}")
            self.assertEqual(status, 400, bad)
            self.assertEqual(json.loads(body), {"error": "invalid_n"})

    async def test_malformed_frontmatter_lists_with_null_kind_and_title(self) -> None:
        (self.kb / "wiki" / "broken.md").write_text("not a frontmatter block at all\n")
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/list")
        self.assertEqual(status, 200)
        entries = {e["name"]: e for e in json.loads(body)["pages"]}
        self.assertIsNone(entries["broken.md"]["kind"])
        self.assertIsNone(entries["broken.md"]["title"])

    async def test_kind_filter_returns_only_that_kind(self) -> None:
        _write_page(self.kb, "a.md", kind="topic")
        _write_page(self.kb, "b.md", kind="story")
        _write_page(self.kb, "c.md", kind="summary")
        _write_page(self.kb, "d.md", kind="story")
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/list?kind=story")
        self.assertEqual(status, 200)
        pages = json.loads(body)["pages"]
        self.assertEqual({p["name"] for p in pages}, {"b.md", "d.md"})


class SchemaRouteTest(RouteTestCase):
    async def test_missing_schema_404s_but_other_routes_still_answer(self) -> None:
        _write_page(self.kb, "a.md")
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not_found"})
        status, _, _ = await _call(wrapped, "GET", "/kb/demo/list")
        self.assertEqual(status, 200)
        status, _, _ = await _call(wrapped, "GET", "/kb/demo/page/a.md")
        self.assertEqual(status, 200)

    async def test_unreadable_schema_answers_500_not_404(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        schema = self.kb / "SCHEMA.md"
        schema.write_text("# schema\n")
        schema.chmod(0o000)
        self.addCleanup(schema.chmod, 0o644)
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "internal_error"})

    async def test_content_type_has_no_charset(self) -> None:
        (self.kb / "SCHEMA.md").write_text("# schema\n")
        wrapped = self.build()
        _, _, headers = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(headers["content-type"], "text/markdown")


class SearchRouteTest(RouteTestCase):
    async def test_missing_empty_and_whitespace_query_400_without_calling_rank(self) -> None:
        wrapped = self.build()
        with mock.patch(
            "llmwiki_service.routes.rank",
            side_effect=AssertionError("rank must not be called"),
        ):
            for query in ("", "?q=", "?q=%20%20"):
                status, body, _ = await _call(wrapped, "GET", f"/kb/demo/search{query}")
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "invalid_query"})

    async def test_n_validation_matches_list(self) -> None:
        wrapped = self.build()
        with mock.patch(
            "llmwiki_service.routes.rank",
            side_effect=AssertionError("rank must not be called"),
        ):
            for bad in ("0", "-1", "abc"):
                status, body, _ = await _call(wrapped, "GET", f"/kb/demo/search?q=x&n={bad}")
                self.assertEqual(status, 400, bad)
                self.assertEqual(json.loads(body), {"error": "invalid_n"})
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ) as ranker:
            status, _, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x&n=1001")
            self.assertEqual(status, 200)
            self.assertEqual(ranker.call_args.args[2], routes.MAX_N)

    async def test_stale_vectors_answers_503_with_missing_count_and_no_embed_call(self) -> None:
        _write_kb(self.kb, embed_model="test-embed")
        _write_page(self.kb, "never-embedded.md")

        def respond(_path: str, _body: dict) -> dict:
            raise AssertionError("no embedding call should have been made")

        with FakeEndpoint(respond) as fake:
            _write_kb(self.kb, embed_model="test-embed", endpoint_url=fake.url)
            wrapped = self.build()
            status, body, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body), {"error": "index_stale", "missing": 1})
            self.assertEqual(len(fake.requests), 0)

    async def test_no_embed_model_answers_501(self) -> None:
        _write_kb(self.kb)  # no [models] embed at all
        _write_page(self.kb, "a.md")
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x")
        self.assertEqual(status, 501)
        self.assertEqual(json.loads(body), {"error": "no_embed_model"})

    async def test_model_error_answers_502(self) -> None:
        wrapped = self.build()
        with mock.patch(
            "llmwiki_service.routes.rank", side_effect=ModelError("endpoint failed")
        ):
            status, body, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "model_error"})

    async def test_a_canned_ranking_returns_200_with_hits_in_order_and_exact_fields(self) -> None:
        wrapped = self.build()
        hits = [
            Hit(score=0.91, name="a.md", title="A", updated="2026-01-01T00:00:00Z", size=10),
            Hit(score=0.42, name="b.md", title="B", updated="2026-01-02T00:00:00Z", size=20),
        ]
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=hits, unsummarized=0),
        ):
            status, body, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            payload,
            {
                "hits": [
                    {"score": 0.91, "name": "a.md", "title": "A",
                     "updated": "2026-01-01T00:00:00Z", "size": 10},
                    {"score": 0.42, "name": "b.md", "title": "B",
                     "updated": "2026-01-02T00:00:00Z", "size": 20},
                ]
            },
        )

    async def test_error_bodies_never_leak_the_kb_path_or_an_exception_message(self) -> None:
        with mock.patch(
            "llmwiki_service.routes.rank", side_effect=ModelError(str(self.kb))
        ):
            wrapped = self.build()
            status, body, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x")
        self.assertEqual(status, 502)
        self.assertNotIn(str(self.kb).encode(), body)
        self.assertNotIn(b"demo", body)


class SearchLimiterUnitTest(unittest.TestCase):
    """Direct tests of `auth.SearchLimiter`, independent of any route."""

    def test_budget_refills_after_the_window(self) -> None:
        limiter = auth.SearchLimiter(2, window_sec=10)
        self.assertIsNone(limiter.check("a", now=0.0))
        self.assertIsNone(limiter.check("a", now=1.0))
        self.assertIsNotNone(limiter.check("a", now=2.0))
        self.assertIsNone(limiter.check("a", now=11.0))  # first call now expired

    def test_one_caller_cannot_exhaust_another_callers_budget(self) -> None:
        limiter = auth.SearchLimiter(1, window_sec=10)
        self.assertIsNone(limiter.check("a", now=0.0))
        self.assertIsNotNone(limiter.check("a", now=1.0))
        self.assertIsNone(limiter.check("b", now=1.0))

    def test_retry_after_is_a_positive_whole_second_count(self) -> None:
        limiter = auth.SearchLimiter(1, window_sec=10)
        limiter.check("a", now=0.0)
        retry_after = limiter.check("a", now=3.5)
        self.assertIsInstance(retry_after, int)
        self.assertGreater(retry_after, 0)

    def test_sixty_requests_across_a_simulated_minute_boundary_trip_429(self) -> None:
        # A fixed window resets its whole budget the instant the clock
        # crosses a boundary; a sliding window does not. Sixty calls in
        # two seconds straddling t=60 must trip the limit somewhere in
        # here, which a fixed window keyed on floor(now / 60) would not.
        limiter = auth.SearchLimiter(30, window_sec=60)
        blocked = False
        for i in range(60):
            moment = 59.0 + (i / 30.0)  # 59.0 .. 60.97, spanning the boundary
            if limiter.check("caller", now=moment) is not None:
                blocked = True
                break
        self.assertTrue(blocked)


class SearchLimiterThroughRouteTest(RouteTestCase):
    async def test_token_a_hits_429_token_b_unaffected_under_token_read(self) -> None:
        wrapped = self.build(access=TOKEN_ACCESS, search_limiter=auth.SearchLimiter(1))
        token_a = self.store.mint("a-key", "reader")
        token_b = self.store.mint("b-key", "reader")
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ):
            first = await _call(wrapped, "GET", "/kb/demo/search?q=x", headers=_bearer(token_a))
            self.assertEqual(first[0], 200)
            second = await _call(wrapped, "GET", "/kb/demo/search?q=x", headers=_bearer(token_a))
            self.assertEqual(second[0], 429)
            self.assertEqual(json.loads(second[1]), {"error": "rate_limited"})
            retry_after = int(second[2]["retry-after"])
            self.assertGreater(retry_after, 0)

            other = await _call(wrapped, "GET", "/kb/demo/search?q=x", headers=_bearer(token_b))
            self.assertEqual(other[0], 200)

    async def test_source_address_is_the_key_under_open_read(self) -> None:
        wrapped = self.build(access=OPEN_ACCESS, search_limiter=auth.SearchLimiter(1))
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ):
            first = await _call(wrapped, "GET", "/kb/demo/search?q=x", client=("198.51.100.1", 1))
            self.assertEqual(first[0], 200)
            second = await _call(wrapped, "GET", "/kb/demo/search?q=x", client=("198.51.100.1", 1))
            self.assertEqual(second[0], 429)
            other_client = await _call(wrapped, "GET", "/kb/demo/search?q=x", client=("198.51.100.2", 1))
            self.assertEqual(other_client[0], 200)

    async def test_a_429_makes_no_call_to_rank_at_all(self) -> None:
        wrapped = self.build(access=OPEN_ACCESS, search_limiter=auth.SearchLimiter(1))
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ) as ranker:
            await _call(wrapped, "GET", "/kb/demo/search?q=x", client=("198.51.100.9", 1))
            status, _, _ = await _call(wrapped, "GET", "/kb/demo/search?q=x", client=("198.51.100.9", 1))
        self.assertEqual(status, 429)
        self.assertEqual(ranker.call_count, 1)


class DeploymentDefaultsTest(RouteTestCase):
    """`make_routes` reads `access` and `kbs` out of the deployment with
    a `{}` default each, so a deployment missing either key still
    answers a request instead of raising on `None.get`."""

    def _build_from(self, deployment: dict) -> app.ASGIApp:
        return app.build_app(
            None,
            deployment,
            self.store,
            self.failure_limiter,
            self.search_limiter,
        )

    async def test_a_deployment_with_no_access_key_refuses_with_401(self) -> None:
        wrapped = self._build_from({"kbs": {"demo": {"path": str(self.kb)}}})
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})

    async def test_a_deployment_with_no_kbs_key_404s(self) -> None:
        wrapped = self._build_from({"access": OPEN_ACCESS})
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not_found"})


class UnknownKbNameTest(RouteTestCase):
    """All four routes answer an unknown kb name with the identical 404
    body, not just `schema`, and `search` decides it before it would
    ever reach ranking."""

    async def test_all_four_routes_404_on_an_unknown_kb_name(self) -> None:
        wrapped = self.build(access=OPEN_ACCESS)
        paths = (
            "/kb/nope/search?q=x",
            "/kb/nope/page/a.md",
            "/kb/nope/list",
            "/kb/nope/schema",
        )
        with mock.patch(
            "llmwiki_service.routes.rank",
            side_effect=AssertionError("rank must not be called"),
        ):
            for path in paths:
                with self.subTest(path=path):
                    status, body, _ = await _call(wrapped, "GET", path)
                    self.assertEqual(status, 404)
                    self.assertEqual(json.loads(body), {"error": "not_found"})


class PageReadFailureTest(RouteTestCase):
    """A page name that resolves inside the wiki but cannot be read is
    the third 404 in `page_route`, distinct from the unknown kb and the
    rejected name."""

    async def test_an_unreadable_page_404s_rather_than_500ing(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        page = _write_page(self.kb, "locked.md")
        page.chmod(0o000)
        self.addCleanup(page.chmod, 0o644)
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/page/locked.md")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not_found"})


class RefusalPerRouteTest(RouteTestCase):
    """The refusal branch of every route: the 401 body, the 429 the
    failure limiter produces once a caller has spent its budget, and the
    fact that one caller's failures never spend another caller's."""

    PATHS = (
        "/kb/demo/search?q=x",
        "/kb/demo/page/a.md",
        "/kb/demo/list",
        "/kb/demo/schema",
    )

    def _populate(self) -> None:
        _write_page(self.kb, "a.md")
        (self.kb / "SCHEMA.md").write_text("# schema\n")

    async def test_every_route_401s_for_a_bad_token_under_token_read(self) -> None:
        self._populate()
        wrapped = self.build(access=TOKEN_ACCESS)
        for path in self.PATHS:
            with self.subTest(path=path):
                status, body, _ = await _call(
                    wrapped, "GET", path, headers=_bearer("bad-token")
                )
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body), {"error": "unauthorized"})

    async def test_a_blocked_caller_429s_and_another_caller_is_untouched(self) -> None:
        self._populate()
        for path in self.PATHS:
            with self.subTest(path=path):
                self.failure_limiter = auth.FailureLimiter(1)
                wrapped = self.build(access=TOKEN_ACCESS)
                spender = ("198.51.100.7", 1)
                first = await _call(
                    wrapped, "GET", path, headers=_bearer("bad"), client=spender
                )
                self.assertEqual(first[0], 401)
                blocked = await _call(
                    wrapped, "GET", path, headers=_bearer("bad"), client=spender
                )
                self.assertEqual(blocked[0], 429)
                self.assertEqual(json.loads(blocked[1]), {"error": "rate_limited"})
                other = await _call(
                    wrapped,
                    "GET",
                    path,
                    headers=_bearer("bad"),
                    client=("198.51.100.8", 1),
                )
                self.assertEqual(other[0], 401)
                self.assertEqual(json.loads(other[1]), {"error": "unauthorized"})


class ClientlessScopeTest(RouteTestCase):
    """uvicorn hands over a scope with no `client` for a unix socket
    connection. The gate still needs a key, and that key is the literal
    string the limiter then holds."""

    async def test_a_scope_with_no_client_is_keyed_as_unknown(self) -> None:
        wrapped = self.build(access=TOKEN_ACCESS)
        status, body, _ = await _call(
            wrapped, "GET", "/kb/demo/schema", headers=_bearer("bad"), client=None
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "unauthorized"})
        self.assertEqual(self.failure_limiter.tracked(), ["unknown"])


class SearchRankArgumentsTest(RouteTestCase):
    async def test_rank_receives_the_kb_query_n_and_kind_verbatim(self) -> None:
        wrapped = self.build()
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ) as ranker:
            status, _, _ = await _call(
                wrapped, "GET", "/kb/demo/search?q=widgets&n=5&kind=story"
            )
        self.assertEqual(status, 200)
        kb_arg, q_arg, n_arg, kind_arg = ranker.call_args.args
        self.assertEqual(kb_arg.root, self.kb)
        self.assertEqual(q_arg, "widgets")
        self.assertEqual(n_arg, 5)
        self.assertEqual(kind_arg, "story")

    async def test_an_empty_or_absent_kind_reaches_rank_as_none(self) -> None:
        wrapped = self.build()
        with mock.patch(
            "llmwiki_service.routes.rank",
            return_value=Ranking(hits=[], unsummarized=0),
        ) as ranker:
            for query in ("/kb/demo/search?q=x", "/kb/demo/search?q=x&kind="):
                with self.subTest(query=query):
                    await _call(wrapped, "GET", query)
                    self.assertIsNone(ranker.call_args.args[3])


class ListEntryShapeTest(RouteTestCase):
    MTIME = 1_767_225_600  # 2026-01-01T00:00:00Z

    async def test_one_entry_matches_field_for_field(self) -> None:
        page = _write_page(self.kb, "solo.md", kind="topic", title="Solo Title")
        os.utime(page, (self.MTIME, self.MTIME))
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/list")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "pages": [
                    {
                        "name": "solo.md",
                        "kind": "topic",
                        "title": "Solo Title",
                        "updated": "2026-01-01T00:00:00Z",
                        "size": page.stat().st_size,
                    }
                ],
                "next": None,
            },
        )

    async def test_next_is_null_when_the_page_count_equals_n_exactly(self) -> None:
        for name in ("a.md", "b.md", "c.md"):
            _write_page(self.kb, name)
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/list?n=3")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([p["name"] for p in payload["pages"]], ["a.md", "b.md", "c.md"])
        self.assertIsNone(payload["next"])

    async def test_n_of_one_is_accepted_and_returns_one_page(self) -> None:
        self.assertEqual(routes._parse_n("1", routes.DEFAULT_LIST_N), 1)
        for name in ("a.md", "b.md"):
            _write_page(self.kb, name)
        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/list?n=1")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([p["name"] for p in payload["pages"]], ["a.md"])
        self.assertEqual(payload["next"], "a.md")

    async def test_updated_is_utc_even_under_a_non_utc_local_zone(self) -> None:
        # `_updated` passes tz=timezone.utc; dropping it would silently
        # fall back to local time, which is invisible on a UTC machine.
        # "XYZ7" is a POSIX TZ string, so this needs no zoneinfo database.
        page = _write_page(self.kb, "stamped.md")
        os.utime(page, (self.MTIME, self.MTIME))
        previous = os.environ.get("TZ")

        def restore() -> None:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "XYZ7"
        time.tzset()
        self.assertNotEqual(
            time.strftime("%H", time.localtime(self.MTIME)), "00"
        )  # the zone really did take effect

        wrapped = self.build()
        status, body, _ = await _call(wrapped, "GET", "/kb/demo/list")
        self.assertEqual(status, 200)
        entry = json.loads(body)["pages"][0]
        self.assertEqual(entry["updated"], "2026-01-01T00:00:00Z")


class ListSkipsAVanishedPageTest(RouteTestCase):
    async def test_a_page_unlinked_mid_scan_is_skipped_not_a_stop(self) -> None:
        for name in ("a.md", "b.md", "c.md"):
            _write_page(self.kb, name)
        real_read = routes.read_page_text

        def read(path: Path) -> str:
            if path.name == "b.md":
                raise FileNotFoundError(str(path))
            return real_read(path)

        wrapped = self.build()
        with mock.patch("llmwiki_service.routes.read_page_text", side_effect=read):
            status, body, _ = await _call(wrapped, "GET", "/kb/demo/list")
        self.assertEqual(status, 200)
        self.assertEqual(
            [p["name"] for p in json.loads(body)["pages"]], ["a.md", "c.md"]
        )


class SchemaBodyTest(RouteTestCase):
    async def test_the_schema_bytes_are_returned_verbatim(self) -> None:
        raw = b"# schema\n\nrules \xff\xfe here\n"
        (self.kb / "SCHEMA.md").write_bytes(raw)
        wrapped = self.build()
        status, body, headers = await _call(wrapped, "GET", "/kb/demo/schema")
        self.assertEqual(status, 200)
        self.assertEqual(body, raw)
        self.assertEqual(headers["content-type"], "text/markdown")


class MethodNotAllowedTest(RouteTestCase):
    """Every route is registered GET-only, so a POST to a path that
    otherwise matches never reaches the handler."""

    async def test_post_to_every_route_is_405(self) -> None:
        _write_page(self.kb, "a.md")
        (self.kb / "SCHEMA.md").write_text("# schema\n")
        wrapped = self.build()
        paths = (
            "/kb/demo/search?q=x",
            "/kb/demo/page/a.md",
            "/kb/demo/list",
            "/kb/demo/schema",
        )
        with mock.patch(
            "llmwiki_service.routes.rank",
            side_effect=AssertionError("rank must not be called"),
        ):
            for path in paths:
                with self.subTest(path=path):
                    status, body, _ = await _call(wrapped, "POST", path)
                    self.assertEqual(status, 405)
                    self.assertEqual(
                        json.loads(body), {"error": "method_not_allowed"}
                    )


if __name__ == "__main__":
    unittest.main()
