"""Client for wikis that live on another machine.

Parses the `[remotes]` table out of the kb's own `config.toml`, calls
phase 3's read routes over HTTPS, and hands back one ranked list per
wiki. `model.py` owns a different endpoint, with a different credential
and a different error set, so the two clients stay apart.

The rule this module exists to hold: a similarity score is comparable
only inside one embedding model's distribution, so two wikis' answers
are never concatenated and never re-sorted. `RemoteHit` carries `rank`,
a position inside one wiki's own list, and no score, so a later reader
has nothing to sort a merge on.
"""
from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit

from llmwiki.core import flatten
from llmwiki.model import OPENER

# Unmeasured (phase 4): no p99 of a real GET /search against a
# populated kb has been taken. It sits below model.REQUEST_TIMEOUT_SEC
# so a remote whose own model call is slow reads as `timeout` here,
# which an interactive search wants. Replace it with a measurement.
REMOTE_TIMEOUT_SEC = 20

MAX_RESPONSE_BYTES = 4 * 1024 * 1024

MARKDOWN_MEDIA_TYPE = "text/markdown"

LOCAL_LABEL = "local"  # reserved: the kb on disk answers under it

REMOTE_KEYS = ("url", "token_env", "mode")

STATUS_CODES = {
    401: "unauthorized",
    403: "unauthorized",
    404: "not_found",
    429: "rate_limited",
    501: "no_embed_model",
    502: "upstream_model_failed",
    503: "index_stale",
}


class Remote(NamedTuple):
    """One pointer out of `[remotes]`. `mode` is validated at the parse
    and stored nowhere: only the server decides what a caller may do."""

    name: str
    url: str
    token_env: str | None


class RemoteHit(NamedTuple):
    """One hit, ranked WITHIN one wiki's answer. No score field, by
    design: see the module docstring."""

    rank: int
    name: str
    title: str
    updated: str
    size: int


class RemoteRanking(NamedTuple):
    remote: str
    hits: tuple[RemoteHit, ...]


class RemoteFailure(NamedTuple):
    remote: str
    code: str


Answer = RemoteRanking | RemoteFailure


class RemoteError(Exception):
    """Carries `code` from the closed set in `STATUS_CODES` plus
    `no_token`, `http_error`, `timeout`, `unreachable`, and
    `bad_response`. Never carries the url, the response body, a header,
    or the token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_remotes(config: dict) -> dict[str, Remote]:
    """The `[remotes]` table, checked. Raises ValueError naming the
    remote and the offending key. Opens no socket."""
    table = config.get("remotes", {})
    if not isinstance(table, dict):
        raise ValueError("[remotes] is not a table")
    return {name: _remote(name, value) for name, value in table.items()}


def _remote(name: str, value: object) -> Remote:
    _check_label(name)
    if not isinstance(value, dict):
        raise ValueError(f"remote {name}: not a table")
    for key in value:
        if key not in REMOTE_KEYS:
            raise ValueError(f"remote {name}: unknown key {key!r}")
    mode = value.get("mode", "read")
    if mode != "read":
        raise ValueError(f"remote {name}: mode: only 'read' is accepted")
    token_env = value.get("token_env")
    if token_env is not None and not isinstance(token_env, str):
        raise ValueError(f"remote {name}: token_env: not a string")
    return Remote(name, _check_url(name, value.get("url")), token_env)


def _check_label(name: str) -> None:
    if name == LOCAL_LABEL:
        raise ValueError(f"remote {name}: label is reserved for the local kb")
    if name != flatten(name) or " " in name:
        raise ValueError(
            f"remote {name!r}: label holds a space or a control character"
        )


def _check_url(name: str, url: object) -> str:
    if url is None:
        raise ValueError(f"remote {name}: url: missing")
    if not isinstance(url, str):
        raise ValueError(f"remote {name}: url: not a string")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"remote {name}: url: scheme is not https")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"remote {name}: url: carries userinfo")
    if not parts.hostname:
        raise ValueError(f"remote {name}: url: no host")
    if parts.query:
        raise ValueError(f"remote {name}: url: carries a query")
    if parts.fragment:
        raise ValueError(f"remote {name}: url: carries a fragment")
    return url.rstrip("/")


def _request(
    remote: Remote, path: str, params: dict[str, str] | None
) -> urllib.request.Request:
    """A GET for `remote`, carrying the token named by `token_env` and
    read fresh from the environment on every call. The token reaches no
    module state and no message."""
    url = remote.url + path
    if params:
        url += "?" + urlencode(params)
    headers = {}
    if remote.token_env is not None:
        token = os.environ.get(remote.token_env)
        if not token:
            raise RemoteError("no_token")
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, method="GET", headers=headers)


def _read(
    remote: Remote,
    path: str,
    params: dict[str, str] | None = None,
    media_type: str | None = None,
) -> bytes:
    """The response body, at most `MAX_RESPONSE_BYTES`, measured by
    reading one byte past the cap rather than by trusting
    `Content-Length`. Raises RemoteError."""
    request = _request(remote, path, params)
    try:
        with OPENER.open(request, timeout=REMOTE_TIMEOUT_SEC) as response:
            if (
                media_type is not None
                and response.headers.get_content_type() != media_type
            ):
                raise RemoteError("bad_response")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        code = STATUS_CODES.get(exc.code, "http_error")
        exc.close()  # the error response holds a temp file; drop it now
        raise RemoteError(code) from None
    except TimeoutError:
        raise RemoteError("timeout") from None
    except URLError as exc:
        reason = exc.reason
        code = "timeout" if isinstance(reason, TimeoutError) else "unreachable"
        raise RemoteError(code) from None
    except OSError:
        raise RemoteError("unreachable") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise RemoteError("bad_response")
    return body


def _bare_md_name(name: str) -> bool:
    """A page filename and nothing else: no separator, no `..` walk, no
    control character, and a `.md` suffix."""
    return (
        name.endswith(".md")
        and "/" not in name
        and "\\" not in name
        and name == flatten(name)
    )


def _hits(body: bytes) -> tuple[RemoteHit, ...]:
    """The wire is a trust boundary: the whole response is refused
    rather than salvaged in part. Unknown fields are ignored, because a
    service may be newer than its client."""
    try:
        payload = json.loads(body)
    except ValueError:
        raise RemoteError("bad_response") from None
    if not isinstance(payload, dict) or not isinstance(
        payload.get("hits"), list
    ):
        raise RemoteError("bad_response")
    hits = []
    for rank, hit in enumerate(payload["hits"], start=1):
        if not isinstance(hit, dict):
            raise RemoteError("bad_response")
        name, title = hit.get("name"), hit.get("title")
        updated, size = hit.get("updated"), hit.get("size")
        if not all(isinstance(v, str) for v in (name, title, updated)):
            raise RemoteError("bad_response")
        if not isinstance(size, int) or isinstance(size, bool):
            raise RemoteError("bad_response")
        if not _bare_md_name(name):
            raise RemoteError("bad_response")
        hits.append(
            RemoteHit(rank, name, flatten(title), flatten(updated), size)
        )
    return tuple(hits)


def search_one(
    remote: Remote, query: str, n: int, kind: str | None
) -> Answer:
    """GET <url>/search. Catches RemoteError and returns RemoteFailure,
    so one broken remote cannot end a fan-out."""
    params = {"q": query, "n": str(n)}
    if kind is not None:
        params["kind"] = kind
    try:
        body = _read(remote, "/search", params)
        return RemoteRanking(remote.name, _hits(body))
    except RemoteError as exc:
        return RemoteFailure(remote.name, exc.code)


def fan_out(
    remotes: list[Remote], query: str, n: int, kind: str | None
) -> tuple[Answer, ...]:
    """One entry per remote, in the order given, whatever happened. One
    request per remote, in parallel, each bounded by
    REMOTE_TIMEOUT_SEC, so the wall clock is one timeout and not N."""
    if not remotes:
        return ()
    with ThreadPoolExecutor(max_workers=len(remotes)) as pool:
        answers = pool.map(lambda r: search_one(r, query, n, kind), remotes)
        return tuple(answers)


def page(remote: Remote, name: str) -> bytes:
    """GET <url>/page/<name>. Raises RemoteError. `name` is quoted whole
    so a separator in it cannot reshape the request; phase 3's route
    refuses such a name as well, and both checks stay."""
    return _read(
        remote,
        "/page/" + quote(name, safe=""),
        media_type=MARKDOWN_MEDIA_TYPE,
    )
