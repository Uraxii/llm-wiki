"""The four read routes: search, page, list, schema.

Owns kb resolution, the token-before-name check order, the page name
check, the per-caller search limit, the list bound, and every response
shape and error body this phase defines. `app.py` stays the wiring and
middleware module; this file is where a route actually decides.

Closed set of error codes this module emits, every one of them
`{"error": "<code>"}` and nothing else, except the 503 which also
carries `missing`:

- `unauthorized` (401): absent token, malformed token, unknown or
  revoked token id, or a role below `reader`. One body for all of them,
  per phase 3's own rule that a caller cannot tell "you are nobody"
  from "you are somebody with no access here".
- `rate_limited` (429): too many failed authentications from the caller
  in the current window, or the caller's `search` budget for this
  window is spent. Every 429 carries a `Retry-After` header; none
  carries a body field.
- `not_found` (404): reused for every "there is nothing here" case an
  attacker could otherwise use to enumerate the deployment: an unknown
  kb name, a rejected or missing page name, and a kb with no
  `SCHEMA.md`. The spec asks for the unknown-kb and missing-schema
  cases to collide on purpose; folding the rejected-page-name case into
  the same code keeps the set closed instead of growing a code per
  rejection reason. Also the code `app.py` answers for a path with no
  matching route at all: starlette's router raises before any function
  in this file runs, so `app._router_error` emits it instead.
- `method_not_allowed` (405): a path matches a route in this file but
  not with the request's HTTP method (`POST /kb/<name>/list`, say).
  Raised and answered the same way as the router's 404, in
  `app._router_error`.
- `invalid_query` (400): `q` absent, empty, or whitespace only.
- `invalid_n` (400): `n` not a positive integer.
- `index_stale` (503): one or more pages lack a current vector.
  Carries `missing`, the only error body besides `Retry-After` allowed
  to name a count.
- `no_embed_model` (501): the kb has no `[models] embed`.
- `model_error` (502): the embedding endpoint failed.
- `internal_error` (500): `SCHEMA.md` exists but a read failed for a
  reason other than "missing" (a permissions fault, say), or any other
  unhandled fault a route raises. `schema_route` emits the first case
  itself; `app.py`'s `Exception` handler emits the second for every
  route, so one bad page or one dropped database lock never answers
  plain text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from llmwiki.core import Kb, TIMESTAMP_FORMAT, parse_frontmatter, read_page_text
from llmwiki.model import ModelError
from llmwiki.vectors import TOP_K, NoEmbedModel, StaleVectors, rank
from llmwiki_service.auth import FailureLimiter, Principal, Refusal, SearchLimiter, authorize
from llmwiki_service.tokens import TokenStore

MAX_N = 1000
DEFAULT_LIST_N = 200


def _error(status: int, code: str, **extra) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=status)


def _rate_limited(retry_after: int) -> JSONResponse:
    response = _error(429, "rate_limited")
    response.headers["Retry-After"] = str(retry_after)
    return response


def _refusal_response(refusal: Refusal) -> JSONResponse:
    if refusal.reason == "rate_limited":
        return _rate_limited(refusal.retry_after)
    return _error(401, refusal.reason)


def _parse_n(raw: str | None, default: int) -> int | None:
    """`default` when `raw` is absent. Capped at `MAX_N` when over, NOT
    rejected: the spec's own test says n=1001 is capped at 1000, while
    n=0, n=-1, and n=abc each return 400. `None` here means "400"."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, MAX_N)


def _updated(stat) -> str:
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
        TIMESTAMP_FORMAT
    )


def _resolve_page(kb: Kb, page: str) -> Path | None:
    """The page's resolved path, or `None` per the page-name check:
    parent must equal `kb.wiki.resolve()` (equality, not
    `is_relative_to`, which would pass a page inside a subdirectory),
    suffix must be `.md`. Both sides resolved, because a symlinked or
    bind-mounted kb root is the normal deployment (phase 2's own
    example path). An embedded NUL makes `Path.resolve` raise
    `ValueError`; caught here so it 404s instead of 500ing. Never
    repairs a rejected name."""
    try:
        candidate = (kb.wiki / page).resolve()
    except ValueError:
        return None
    if candidate.parent != kb.wiki.resolve() or candidate.suffix != ".md":
        return None
    return candidate


def make_routes(
    deployment: dict,
    store: TokenStore,
    failure_limiter: FailureLimiter,
    search_limiter: SearchLimiter,
) -> list[Route]:
    """The four `Route`s, closed over this process's deployment, token
    store, and limiters. A fresh `Kb` is built per request from the
    deployment's `[kbs.<name>] path` (cheap: one small config.toml
    parse), never cached, so a kb path is always read fresh."""
    access = deployment.get("access", {})
    kbs = deployment.get("kbs", {})

    def _client_address(request: Request) -> str:
        client = request.client
        return client.host if client else "unknown"

    def _authenticate(request: Request, client: str) -> Principal | Refusal:
        header = request.headers.get("authorization")
        return authorize(header, "read", access, store, failure_limiter, client)

    def _kb_or_none(name: str) -> Kb | None:
        table = kbs.get(name)
        path = table.get("path") if isinstance(table, dict) else None
        return Kb(Path(path)) if path else None

    async def search_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _authenticate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate)
        principal = gate

        kb = _kb_or_none(request.path_params["name"])
        if kb is None:
            return _error(404, "not_found")

        q = (request.query_params.get("q") or "").strip()
        if not q:
            return _error(400, "invalid_query")

        n = _parse_n(request.query_params.get("n"), TOP_K)
        if n is None:
            return _error(400, "invalid_n")

        kind = request.query_params.get("kind") or None

        # Keyed on the auth result, not a second read of `[access]
        # read`: `principal.token_id` is "" for `auth.ANONYMOUS`, so
        # open read falls through to the address by construction and
        # a token read never shares the address bucket with another
        # token, whatever `[access] read` is spelled or left unset.
        limiter_key = principal.token_id or client
        retry_after = search_limiter.check(limiter_key)
        if retry_after is not None:
            return _rate_limited(retry_after)

        try:
            ranking = rank(kb, q, n, kind)
        except StaleVectors as exc:
            return _error(503, "index_stale", missing=exc.missing)
        except NoEmbedModel:
            return _error(501, "no_embed_model")
        except ModelError:
            return _error(502, "model_error")

        hits = [
            {
                "score": hit.score,
                "name": hit.name,
                "title": hit.title,
                "updated": hit.updated,
                "size": hit.size,
            }
            for hit in ranking.hits
        ]
        return JSONResponse({"hits": hits})

    async def page_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _authenticate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate)

        kb = _kb_or_none(request.path_params["name"])
        if kb is None:
            return _error(404, "not_found")

        candidate = _resolve_page(kb, request.path_params["page"])
        if candidate is None:
            return _error(404, "not_found")
        try:
            body = candidate.read_bytes()
        except OSError:
            # Deliberately 404, not 500, unlike schema_route's split of
            # "missing" from "exists but unreadable": a page name is
            # caller-chosen, so a distinct status here would let an
            # attacker enumerate which names exist but are locked down,
            # the same oracle the token-before-kb-name check order
            # already closes. Pinned by
            # test_an_unreadable_page_404s_rather_than_500ing.
            return _error(404, "not_found")
        # No charset: passing headers directly bypasses Starlette's
        # automatic "; charset=utf-8" append for any text/* media type
        # (see starlette.responses.Response.init_headers). The spec
        # requires text/markdown with NO charset parameter, verbatim.
        return Response(body, headers={"content-type": "text/markdown"})

    async def list_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _authenticate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate)

        kb = _kb_or_none(request.path_params["name"])
        if kb is None:
            return _error(404, "not_found")

        n = _parse_n(request.query_params.get("n"), DEFAULT_LIST_N)
        if n is None:
            return _error(400, "invalid_n")
        kind = request.query_params.get("kind") or None
        after = request.query_params.get("after") or None

        names = sorted(p.name for p in kb.wiki.glob("*.md"))
        if after is not None:
            names = [name for name in names if name > after]
        window = names[:n]

        entries = []
        for filename in window:
            path = kb.wiki / filename
            try:
                stat = path.stat()
            except OSError:
                continue  # unlinked between the glob and this stat
            try:
                text = read_page_text(path)
            except FileNotFoundError:
                continue  # unlinked between the stat and this read
            except OSError:
                # The name exists but its bytes do not: a directory
                # named "*.md" (wiki/ is the user's agent's to
                # populate, and the spec lets it hold a subdirectory)
                # or a permissions fault. Listed with kind and title
                # both null, same as unparseable frontmatter, per the
                # spec: "A page the wiki holds and the listing hides
                # is worse than a page the listing admits it cannot
                # read" (phase-03-read-endpoints.md).
                page_kind = None
                title = None
            else:
                parsed = parse_frontmatter(text)
                page_kind = parsed[0].get("kind") if parsed else None
                title = parsed[0].get("title") if parsed else None
            if kind is not None and page_kind != kind:
                continue
            entries.append(
                {
                    "name": filename,
                    "kind": page_kind,
                    "title": title,
                    "updated": _updated(stat),
                    "size": stat.st_size,
                }
            )
        next_name = window[-1] if len(names) > n else None
        return JSONResponse({"pages": entries, "next": next_name})

    async def schema_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _authenticate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate)

        kb = _kb_or_none(request.path_params["name"])
        if kb is None:
            return _error(404, "not_found")

        schema_path = kb.root / "SCHEMA.md"
        if not schema_path.is_file():
            return _error(404, "not_found")
        try:
            body = schema_path.read_bytes()
        except OSError:
            return _error(500, "internal_error")
        return Response(body, headers={"content-type": "text/markdown"})

    return [
        Route("/kb/{name}/search", search_route, methods=["GET"]),
        Route("/kb/{name}/page/{page}", page_route, methods=["GET"]),
        Route("/kb/{name}/list", list_route, methods=["GET"]),
        Route("/kb/{name}/schema", schema_route, methods=["GET"]),
    ]
