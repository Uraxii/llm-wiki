"""The three admin routes: mint a token, list the table, revoke by
label.

They sit on one path, service wide, and not under `/kb/<name>/`. A
token reaches every kb in `[kbs]` by design, so minting one under a
kb's path would read as a grant scoped to that kb and would not be one.
No route ever puts a credential in a path or a query string, which is
why minting is a `POST` carrying a JSON body: an access log records the
request line.

Separate from `routes.py` on purpose. That module's docstring defines a
closed error set for four read routes that take no lock and write
nothing; these routes write the token table, run a different gate, and
carry the different closed set below.

Closed set of error codes this module emits, every one of them
`{"error": "<code>"}` and nothing else:

- `unauthorized` (401): absent, malformed, unknown, or revoked
  credential, or a role below `admin`. One body for all of them.
- `rate_limited` (429): the caller's failed-authentication budget for
  this window is spent. Carries `Retry-After`, no body field. The
  budget is `FailureLimiter`'s, shared with the read gate rather than
  copied: a caller who burns ten guesses on `search` has none left
  here. `SearchLimiter` does not apply, because minting costs one
  sqlite insert and no model call, and that limiter is a spend budget.
- `invalid_label` (400): the body is not a JSON object, or `label` is
  absent, is not a string, or fails `TokenStore.mint`'s label contract.
- `invalid_role` (400): `role` is absent or is not one of
  `tokens.ROLES`. A separate code from `invalid_label` because the
  caller is a person with curl, and one code for "your request was
  wrong somehow" makes them guess.
- `duplicate_label` (409): the label already names a row. Without this
  the unique constraint's `sqlite3.IntegrityError` would reach
  `app.py`'s `Exception` handler and answer `internal_error`. Not an
  enumeration oracle: every caller who reaches this code has already
  presented an admin credential and can read the whole table with
  `GET /admin/tokens`.
- `not_found` (404): a `DELETE` for a label that is unknown or already
  revoked. `TokenStore.revoke` cannot tell those apart and both mean
  the same thing to the caller: nothing changed. Chosen over an
  idempotent 204 because nothing in this design retries a request, so
  idempotence buys nothing and the informative answer wins.
- `method_not_allowed` (405) and `internal_error` (500) come from
  `app.py`, for a method a path does not accept and for any fault no
  handler here catches.

Logging. At most one line per request, on the `llmwiki_service` logger,
in one of these three formats and no others:

    admin mint label=%s role=%s id=%s by=%s
    admin revoke label=%s by=%s
    admin refused code=%s by=%s

No argument is ever the value `mint` returned, the `Authorization`
header, or the bootstrap value. The mint line names the `<id>` segment,
which is not secret. `by` is `principal.label` on the two success
lines, and on the refused line, where there may be no principal and
where a rejected label is unvalidated text, it is the client key the
limiter counts on: an address or a token id, never a credential. A
label cannot forge a second line, because a label that reaches a
success line is one `TokenStore.mint` accepted, and `mint` rejects any
label `llmwiki.core.flatten` would change.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from llmwiki_service import tokens
from llmwiki_service.auth import (
    FailureLimiter,
    Principal,
    Refusal,
    authorize_admin,
)
from llmwiki_service.tokens import TokenStore

logger = logging.getLogger("llmwiki_service")


def _refused(code: str, status: int, client: str) -> JSONResponse:
    """Every non-2xx this module answers, and the one place the refused
    log line is written, so no error path can answer without leaving a
    record and none can leave two."""
    logger.info("admin refused code=%s by=%s", code, client)
    return JSONResponse({"error": code}, status_code=status)


def _rate_limited(retry_after: int, client: str) -> JSONResponse:
    response = _refused("rate_limited", 429, client)
    response.headers["Retry-After"] = str(retry_after)
    return response


def _refusal_response(refusal: Refusal, client: str) -> JSONResponse:
    if refusal.reason == "rate_limited":
        return _rate_limited(refusal.retry_after, client)
    return _refused(refusal.reason, 401, client)


async def _json_object(request: Request) -> dict | None:
    """The request body as a JSON object, or `None` when the bytes are
    not UTF-8, are not JSON, or are a JSON value that is not an
    object."""
    try:
        payload = await request.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _minted_row(store: TokenStore, label: str) -> tokens.TokenRow:
    """The row `mint` just wrote. `mint` returns only the plaintext,
    and the 201 body owes the caller the id and the created timestamp.
    `label` is unique, so exactly one row carries it."""
    return next(row for row in store.list_tokens() if row.label == label)


def make_admin_routes(
    deployment: dict,
    store: TokenStore,
    failure_limiter: FailureLimiter,
    bootstrap: str | None,
) -> list[Route]:
    """The three `Route`s, closed over this process's deployment, token
    store, failure limiter, and bootstrap credential."""
    access = deployment.get("access", {})

    def _client_address(request: Request) -> str:
        client = request.client
        return client.host if client else "unknown"

    def _gate(request: Request, client: str) -> Principal | Refusal:
        header = request.headers.get("authorization")
        return authorize_admin(
            header, bootstrap, access, store, failure_limiter, client
        )

    async def mint_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _gate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate, client)

        payload = await _json_object(request)
        if payload is None:
            return _refused("invalid_label", 400, client)
        label, role = payload.get("label"), payload.get("role")
        if not isinstance(label, str):
            return _refused("invalid_label", 400, client)
        # Checked here so that `mint`'s ValueError below can only be a
        # label fault, which keeps the mapping from exception to error
        # code one line with no message parsing.
        if role not in tokens.ROLES:
            return _refused("invalid_role", 400, client)
        try:
            token = store.mint(label, role)
        except ValueError:
            return _refused("invalid_label", 400, client)
        except sqlite3.IntegrityError:
            return _refused("duplicate_label", 409, client)

        row = _minted_row(store, label)
        logger.info(
            "admin mint label=%s role=%s id=%s by=%s",
            row.label,
            row.role,
            row.id,
            gate.label,
        )
        # `token` appears here and in no other response, ever. The
        # store keeps a keyed hash, and `TokenRow` holds neither the
        # hash nor the plaintext, so the listing route cannot leak one.
        return JSONResponse(
            {
                "token": token,
                "id": row.id,
                "label": row.label,
                "role": row.role,
                "created_at": row.created_at,
            },
            status_code=201,
        )

    async def list_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _gate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate, client)
        # Every row, revoked ones included, oldest first, with no `n`
        # and no `after`: this table holds one row per time a human ran
        # a mint. A deployment that outgrows one response copies phase
        # 3's `after` and `n` pair rather than inventing another.
        rows = [asdict(row) for row in store.list_tokens()]
        return JSONResponse({"tokens": rows})

    async def revoke_route(request: Request) -> Response:
        client = _client_address(request)
        gate = _gate(request, client)
        if isinstance(gate, Refusal):
            return _refusal_response(gate, client)
        label = request.path_params["label"]
        if not store.revoke(label):
            return _refused("not_found", 404, client)
        logger.info("admin revoke label=%s by=%s", label, gate.label)
        # 204 with no body: the label was in the request, so the caller
        # already knows it.
        return Response(status_code=204)

    return [
        Route("/admin/tokens", mint_route, methods=["POST"]),
        Route("/admin/tokens", list_route, methods=["GET"]),
        Route("/admin/tokens/{label}", revoke_route, methods=["DELETE"]),
    ]
