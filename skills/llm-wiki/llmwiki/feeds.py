"""Turn a feed document into a list of item urls: RSS 2.0, Atom, or
JSON Feed. `parse` dispatches on the document's own shape, not its
declared content type, because feed content types are unreliable in
the wild. RSS 1.0 / RDF is deliberately out of scope. Decision
agent-kb-0zf.6; article fetch is ticket .7 (fetch.py).
"""
from __future__ import annotations

import json
from typing import NamedTuple
from xml.etree import ElementTree

from llmwiki import fetch

# Feed content types a server might declare; `load` gates fetch.fetch on
# this instead of fetch.ACCEPTED_TYPES, because a feed is a list of
# sources, not a source (decision agent-kb-0zf.6, phase 12).
FEED_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/feed+json",
        "application/xml",
        "text/xml",
        "application/json",
    }
)

_DOCTYPE_SCAN_BYTES = 1024


class FeedError(Exception):
    """Raised for any reason a document did not become a list of items.
    The module's entire error contract, the same idiom as
    fetch.FetchError and model.ModelError."""


class Item(NamedTuple):
    url: str
    title: str


def load(url: str) -> list[Item]:
    _final_url, _content_type, data = fetch.fetch(url, FEED_TYPES)
    return parse(data)


def parse(data: bytes) -> list[Item]:
    """PURE: bytes in, items out. No network, no Kb, no config."""
    try:
        document = json.loads(data)
    except ValueError:
        items = _parse_xml(data)
    else:
        items = _parse_json_feed(document)
    if not items:
        raise FeedError("feed parsed but yielded zero items")
    return items


def _parse_json_feed(document: object) -> list[Item]:
    if not isinstance(document, dict):
        raise FeedError("json feed document is not an object")
    entries = document.get("items")
    if not isinstance(entries, list):
        raise FeedError("json feed document has no items array")
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue  # a missing or non-string url is skipped, not an error
        title = entry.get("title")
        items.append(Item(url, title if isinstance(title, str) else ""))
    return items


def _parse_xml(data: bytes) -> list[Item]:
    # xml.etree.ElementTree expands internal entities without a size
    # cap, so a feed carrying a DOCTYPE could otherwise make a cron job
    # allocate without bound. A DOCTYPE declaration is refused: feeds do
    # not carry one.
    if b"<!DOCTYPE" in data[:_DOCTYPE_SCAN_BYTES].upper():
        raise FeedError("a DOCTYPE declaration is refused: feeds do not carry one")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise FeedError(f"not a feed: {exc}") from exc
    tag = _local_name(root.tag)
    if tag == "rss":
        return _parse_rss(root)
    if tag == "feed":
        return _parse_atom(root)
    raise FeedError(f"unknown feed root element: {tag!r}")


def _local_name(tag: str) -> str:
    """Strip a `{namespace}` prefix, so dispatch and lookups match
    regardless of a document's namespace declarations."""
    return tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, local_name: str):
    for child in element:
        if _local_name(child.tag) == local_name:
            yield child


def _text(element: ElementTree.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _parse_rss(root: ElementTree.Element) -> list[Item]:
    items = []
    for channel in _children(root, "channel"):
        for entry in _children(channel, "item"):
            url = _text(next(_children(entry, "link"), None))
            if not url:
                continue
            items.append(Item(url, _text(next(_children(entry, "title"), None))))
    return items


def _parse_atom(root: ElementTree.Element) -> list[Item]:
    items = []
    for entry in _children(root, "entry"):
        url = _atom_link(entry)
        if not url:
            continue
        items.append(Item(url, _text(next(_children(entry, "title"), None))))
    return items


def _atom_link(entry: ElementTree.Element) -> str:
    """The `href` of the `link` whose `rel` is `alternate` or absent
    (absent means alternate per the Atom spec)."""
    for link in _children(entry, "link"):
        rel = link.get("rel")
        if rel is None or rel == "alternate":
            href = link.get("href")
            if href:
                return href
    return ""
