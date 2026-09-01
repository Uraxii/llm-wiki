"""One summary wiki page per stored source, written by the configured
chat model. A reply is self-linted before it is kept; a reply that
fails to parse or fails lint drops without touching a previous good
page.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

from llmwiki.core import (
    FrontmatterValue,
    Kb,
    append_log_entry,
    as_list,
    atomic_write_bytes,
    atomic_write_text,
    parse_frontmatter,
    read_page_text,
    render_frontmatter,
    slugify,
)
from llmwiki.lint import lint_pages, prompt_block
from llmwiki.model import ModelError, chat, model_name
from llmwiki.sources import _link_exclusive, read_provenance

# The CLI's own summarizing rules (decision agent-kb-0zf.4): a
# SUMMARIZE.md never has to restate these to get a workable reply.
BUILT_IN_PROMPT = """\
Reply with a single markdown page: a `---` frontmatter block, then a \
blank line, then the body. Do not wrap the reply in a code fence.

The frontmatter must include:
- `kind: summary`
- `title`: a short title for the source
- `identifiers`: a flat list of `key:value` strings, using only the \
keys declared below. Copy each value exactly as it appears in the \
source. Do not put a URL in a value unless the key asks for one. \
Never emit a blank item, and omit a key entirely when the source has \
no value for it.

Put every other fact about the source in the frontmatter, never the \
body. The body is the abstract alone: a few plain sentences, nothing \
else.

Write any rule or expression as plain text, not as a JSON string with \
escaping backslashes.

This is not YAML: match the shape of the example below exactly. The \
closing `---` line is required. Each identifiers item is indented \
with exactly two spaces before the hyphen-space marker, and there is \
no space after the colon in a `key:value` item.

---
kind: summary
title: Example Title
identifiers:
  - ingredient:example-value
  - isbn:9780123456789
---

A few plain sentences of abstract text go here.

When no identifiers apply, write the key with an empty list, never a \
bare key:

---
kind: summary
title: Example Title
identifiers: []
---

A few plain sentences of abstract text go here.\
"""

SOURCE_DELIMITER = "\n\n=== SOURCE TEXT FOLLOWS ===\n\n"
SLUG_SUFFIX_LEN = 12  # hex chars of the digest, for a title-slug collision


def prompt_prefix(kb: Kb) -> str:
    """Built-in rules, then an optional `SUMMARIZE.md`, then the
    declared identifier vocabulary, joined by blank lines."""
    parts = [BUILT_IN_PROMPT]
    summarize_md = kb.root / "SUMMARIZE.md"
    if summarize_md.is_file():
        parts.append(summarize_md.read_text(encoding="utf-8"))
    parts.append(prompt_block(kb.config))
    return "\n\n".join(parts)


def prompt_fingerprint(prefix: str) -> str:
    """sha256 hexdigest of the prompt prefix alone. Excludes the source
    text and the model name: swapping models is not a prompt change."""
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def _all_digests(kb: Kb) -> list[str]:
    return sorted(
        p.stem for p in kb.sources.glob("*") if p.is_file() and p.suffix != ".toml"
    )


def _summary_index(kb: Kb) -> dict[str, tuple[Path, str]]:
    """digest -> (page path, its recorded prompt_fingerprint), for
    every existing summary page. A page unlinked between the glob and
    this read is skipped, not raised on."""
    index: dict[str, tuple[Path, str]] = {}
    for path in sorted(kb.wiki.glob("*.md")):
        try:
            text = read_page_text(path)
        except FileNotFoundError:
            continue
        parsed = parse_frontmatter(text)
        if parsed is None:
            continue
        fields, _body = parsed
        source = fields.get("source")
        if fields.get("kind") == "summary" and source:
            index[str(source)] = (path, str(fields.get("prompt_fingerprint", "")))
    return index


def _unwrap_fence(reply: str) -> str:
    """Drop exactly one outer ``` fence when the stripped reply both
    opens and closes with one. Anything else is returned stripped."""
    stripped = reply.strip()
    lines = stripped.split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].rstrip() == "```":
        return "\n".join(lines[1:-1])
    return stripped


def _drop_reason(
    parsed: tuple[dict[str, FrontmatterValue], str] | None, title: str
) -> str | None:
    """Why a parsed reply must be dropped before anything reaches disk,
    or `None` to keep it. An absent `identifiers` key, or one holding a
    non-empty scalar or a blank item, drops. A present but empty value
    (`identifiers:` with nothing after it, or `identifiers: []`) is an
    empty list, same as `lint` reads it through `core.as_list`."""
    if parsed is None:
        return "unparseable reply"
    if not title:
        return "blank title"
    fields = parsed[0]
    if "identifiers" not in fields:
        return "identifiers missing or not a list"
    identifiers = fields["identifiers"]
    if isinstance(identifiers, str) and identifiers != "":
        return "identifiers missing or not a list"
    if any(not item.strip() for item in as_list(identifiers)):
        return "blank identifier"
    return None


def _source_path(kb: Kb, digest: str) -> Path:
    for path in kb.sources.glob(f"{digest}.*"):
        if path.is_file() and path.suffix != ".toml":
            return path
    raise FileNotFoundError(f"no source bytes for {digest}")


def _claim_new_summary_path(kb: Kb, digest: str, title: str) -> Path:
    """Atomically claim a filename for a digest with no existing
    summary page: the plain slug if free, else the slug suffixed with
    this digest's own hex digits, which no other digest can collide
    on. Reuses `sources._link_exclusive`, the package's one atomic
    exclusive-create primitive, so two processes whose model-chosen
    titles slugify identically can never both believe they claimed the
    plain slug and let one destroy the other's page."""
    candidate = kb.wiki / f"{slugify(title)}.md"
    fd, tmp_name = tempfile.mkstemp(dir=kb.wiki, prefix=f".{candidate.name}.")
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        if _link_exclusive(tmp_path, candidate):
            return candidate
    finally:
        tmp_path.unlink(missing_ok=True)
    return kb.wiki / f"{slugify(title)}-{digest[:SLUG_SUFFIX_LEN]}.md"


def _build_fields(
    reply_fields: dict, digest: str, provenance: dict, kb: Kb, fingerprint: str
) -> dict:
    """CLI-owned keys first, in the pinned order, then every remaining
    reply field carried over opaquely (`identifiers`, domain fields).
    `setdefault` means a CLI key always wins."""
    fields = {
        "kind": "summary",
        "title": reply_fields["title"],
        "source": digest,
        "source_url": provenance.get("url", ""),
        "fetched": provenance.get("fetched", ""),
        "model": model_name(kb.config, "summarize", None),
        "prompt_fingerprint": fingerprint,
    }
    for key, value in reply_fields.items():
        fields.setdefault(key, value)
    return fields


def _commit_summary_page(
    kb: Kb, digest: str, page_path: Path, previous: bytes | None, content: str
) -> bool:
    """Write `content` to `page_path`, self-lint, and roll back on any
    finding: previous bytes restored, or the file unlinked when there
    was none (including a just-claimed path holding only the empty
    placeholder `_claim_new_summary_path` left behind). Returns True
    iff the write was dropped."""
    atomic_write_text(page_path, content)
    findings = lint_pages(kb.root, [page_path])
    if not findings:
        return False
    if previous is not None:
        atomic_write_bytes(page_path, previous)
    else:
        page_path.unlink()
    finding = findings[0]
    append_log_entry(
        kb.log, "summarize", f"{digest}: dropped ({finding.check}: {finding.detail})"
    )
    return True


def _process_digest(
    kb: Kb, digest: str, prefix: str, fingerprint: str, index: dict
) -> bool:
    """Summarize one source. Returns True if it was dropped. Source
    bytes and provenance are read before the paid model call so a
    doomed source never costs one."""
    try:
        text = _source_path(kb, digest).read_bytes().decode("utf-8")
        provenance = read_provenance(kb, digest)
    except (FileNotFoundError, UnicodeDecodeError) as exc:
        append_log_entry(
            kb.log, "summarize", f"{digest}: dropped (cannot read source: {exc})"
        )
        return True

    reply = chat(kb.config, "summarize", prefix + SOURCE_DELIMITER + text)
    parsed = parse_frontmatter(_unwrap_fence(reply))
    title = str(parsed[0].get("title", "")).strip() if parsed else ""
    reason = _drop_reason(parsed, title)
    if reason:
        append_log_entry(kb.log, "summarize", f"{digest}: dropped ({reason})")
        return True

    reply_fields, body = parsed
    fields = _build_fields(reply_fields, digest, provenance, kb, fingerprint)
    content = render_frontmatter(fields, body)

    if digest in index:
        page_path = index[digest][0]
        previous = page_path.read_bytes()
    else:
        page_path = _claim_new_summary_path(kb, digest, title)
        previous = None

    if _commit_summary_page(kb, digest, page_path, previous, content):
        return True

    append_log_entry(kb.log, "summarize", f"{fields['title']}: {digest}")
    return False


def run(root: Path, digests: list[str] | None) -> int:
    """Write a summary page for each of `digests`, or every stored
    source when `None`. Returns 0 iff nothing was dropped or skipped
    for a decode failure."""
    kb = Kb(root)
    targets = digests if digests is not None else _all_digests(kb)
    index = _summary_index(kb)
    prefix = prompt_prefix(kb)
    fingerprint = prompt_fingerprint(prefix)

    remaining = [d for d in targets if index.get(d, (None, None))[1] != fingerprint]
    print(f"summarize: {len(remaining)} planned")

    dropped = False
    for digest in remaining:
        try:
            dropped = _process_digest(kb, digest, prefix, fingerprint, index) or dropped
        except ModelError as exc:
            print(f"llmwiki: summarize: {exc}", file=sys.stderr)
            return 1

    return 1 if dropped else 0
