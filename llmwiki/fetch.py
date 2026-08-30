"""Turn a url into stored bytes: fetch it under a guard that refuses a
private or local address, then reduce html to text. Decision
agent-kb-0zf.7.
"""
from __future__ import annotations

import email.message
import ipaddress
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.error import HTTPError

from llmwiki.core import flatten

TIMEOUT_SEC = 20
MAX_BYTES = 5_000_000
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = ("http", "https")
# application/pdf is deliberately absent: there is no pdf text reader in
# the standard library, so a pdf url fails here with an honest reason
# instead of failing three steps later.
ACCEPTED_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}
)
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "llm-wiki/1.0"
TRACKING_PARAMS = frozenset({"fbclid", "gclid", "ref"})
IGNORED_TAGS = frozenset({"script", "style", "nav", "header", "footer", "aside"})
BLOCK_TAGS = frozenset({"article", "main"})


class FetchError(Exception):
    """Raised for any reason a url did not become bytes. The module's
    entire error contract, mirroring model.ModelError."""


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a redirect into an HTTPError instead of following it, so
    every hop is checked against the same policy as the first."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Built once at import time. ProxyHandler({}) disables the environment
# proxy lookup; the redirect handler above hands each hop back to the
# loop below instead of following it silently.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoAutoRedirect()
)


def is_url(arg: str) -> bool:
    return urllib.parse.urlsplit(arg).scheme in ALLOWED_SCHEMES


def clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), "")
    )


def fetch(url: str) -> tuple[str, str, bytes]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _require_public(current)
        request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT})
        try:
            with _OPENER.open(request, timeout=TIMEOUT_SEC) as response:
                kind = response.headers.get_content_type()
                if kind not in ACCEPTED_TYPES:
                    raise FetchError(f"content type {kind!r} is not accepted")
                # Built from the parsed pieces rather than the raw header,
                # and flattened: this value is written into the provenance
                # sidecar, and a control character in it makes that file
                # permanently unparseable.
                charset = response.headers.get_content_charset()
                content_type = flatten(
                    f"{kind}; charset={charset}" if charset else kind
                )
                data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise FetchError(f"response is larger than {MAX_BYTES} bytes")
            if data.startswith(b"%PDF-"):
                raise FetchError("content is a pdf, which is not supported")
            return current, content_type, data
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code not in REDIRECT_CODES or not location:
                raise FetchError(str(exc)) from None
            current = urllib.parse.urljoin(current, location)
        except (OSError, ValueError, HTTPException) as exc:
            raise FetchError(str(exc)) from None
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")


def _resolved_addresses(host: str) -> list[str]:
    """Thin wrapper around socket.getaddrinfo so a test can point a
    localhost url at a fake public address and exercise the real guard
    in `_require_public` without a real DNS dependency."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _require_public(url: str) -> None:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise FetchError(f"malformed url: {exc}") from None
    if parts.scheme not in ALLOWED_SCHEMES:
        raise FetchError("only http and https urls are fetched")
    if not parts.hostname:
        raise FetchError("no host in the url")
    try:
        addresses = _resolved_addresses(parts.hostname)
    except OSError as exc:
        raise FetchError(f"cannot resolve host: {exc}") from None
    for address in addresses:
        # partition drops an IPv6 scope id, which ip_address would
        # otherwise reject.
        addr = ipaddress.ip_address(address.partition("%")[0])
        if not addr.is_global:
            raise FetchError("refuses a private or local address")


def extract(content_type: str, data: bytes) -> tuple[str, bytes]:
    if content_type.split(";")[0].strip().lower() not in HTML_TYPES:
        return content_type, data
    try:
        text = data.decode(_charset(content_type), errors="replace")
    except LookupError:
        # A page can declare a charset Python has no codec for. Falling
        # back keeps the last-resort promise: extraction failing must
        # never lose the source.
        text = data.decode("utf-8", errors="replace")
    text = densest_text(text)
    if not text.strip():
        # The raw bytes are the last resort: a source is never lost
        # because extraction found nothing.
        return content_type, data
    # text/markdown per decision agent-kb-0zf.6; the body is plain text
    # rather than real markdown now that the html-to-markdown converter
    # is gone.
    return "text/markdown", text.encode("utf-8")


def _charset(content_type: str) -> str:
    header = email.message.Message()
    header["Content-Type"] = content_type or "text/html"
    return header.get_content_charset("utf-8")


def densest_text(html: str) -> str:
    parser = _Densest()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):
        return ""
    blocks = [b for b in parser.blocks if b.strip()]
    candidates = blocks if blocks else [parser.whole]
    return max(candidates, key=len)


class _Densest(HTMLParser):
    """Collect the text of each <article> and <main> region, and of the
    document as a whole, ignoring the tags that carry page furniture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored = 0  # depth inside IGNORED_TAGS
        self._open: list[list[str]] = []  # one buffer per open block tag
        self.blocks: list[str] = []
        self._whole: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in IGNORED_TAGS:
            self._ignored += 1
        elif tag in BLOCK_TAGS:
            # An unclosed furniture tag would otherwise suppress every
            # text node after it, article included, and the scrap that
            # survived would be stored as the whole page.
            self._ignored = 0
            self._open.append([])

    def handle_endtag(self, tag):
        if tag in IGNORED_TAGS:
            self._ignored = max(self._ignored - 1, 0)
        elif tag in BLOCK_TAGS and self._open:
            # An unclosed block tag leaves its buffer here forever; its
            # text still reaches `whole`, so nothing is lost.
            self.blocks.append(_join(self._open.pop()))

    def handle_data(self, data):
        if self._ignored:
            return
        self._whole.append(data)
        for buffer in self._open:
            buffer.append(data)

    @property
    def whole(self) -> str:
        return _join(self._whole)


def _join(chunks: list[str]) -> str:
    # One text node per line: inline markup splits a sentence over
    # lines, accepted for a PoC extractor.
    return "\n".join(c.strip() for c in chunks if c.strip())
