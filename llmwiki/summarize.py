"""One summary wiki page per stored source, written by the configured
chat model. A reply is self-linted before it is kept; a reply that
fails to parse or fails lint drops without touching a previous good
page.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Literal

from llmwiki.core import (
    FrontmatterValue,
    Kb,
    append_log_entry,
    as_list,
    atomic_write_bytes,
    atomic_write_text,
    kb_lock,
    parse_frontmatter,
    read_page_text,
    render_frontmatter,
    slugify,
)
from llmwiki.lint import lint_pages, prompt_block
from llmwiki.model import ModelError, ModelTarget, chat, resolve_target, step_is_configured
from llmwiki.sources import read_provenance

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

# Phase 15: an image or PDF source has no text to append after this
# note, so unlike SOURCE_DELIMITER it stands alone as the prompt's
# final sentence. The bytes ride as a separate attachment content part
# (model.chat), never inlined here.
SOURCE_ATTACHMENT_NOTE = (
    "\n\n=== SOURCE FILE ATTACHED ===\n\n"
    "The source itself is attached to this message as an image or PDF "
    "file. No source text follows; read the attachment."
)

SLUG_SUFFIX_LEN = 12  # hex chars of the digest, for a title-slug collision

FENCE = "```"
REPLY_EXCERPT_CHARS = 60  # of the reply's opening line, for a drop reason

# `ingest.ingest_path` reads a local file of any size, so without a cap
# here a 200 MB log becomes one prompt. 1 MB is roughly 250k tokens,
# which fits every context this CLI can be pointed at, and it sits above
# the largest source measured working end to end (964054 chars). A
# source refused here is one log line and the sweep carries on, where a
# source the endpoint refuses raises ModelError and ends the sweep.
MAX_SOURCE_TEXT_BYTES = 1_000_000

_PDF_TYPE = "application/pdf"

# Types sent as an attachment rather than decoded and inlined. The image
# types sources.EXTENSIONS also knows, kept here and not imported from
# sources.py, because that dict also carries text/markdown and
# .txt-fallback naming concerns this module has no business with.
_VISUAL_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", _PDF_TYPE}
)


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
    every existing summary page. A page unlinked, or made unreadable
    (permissions, a broken symlink), between the glob and this read is
    skipped, not raised on. This loop runs before any digest is
    processed, so one bad page must not take the whole sweep down."""
    index: dict[str, tuple[Path, str]] = {}
    for path in sorted(kb.wiki.glob("*.md")):
        try:
            text = read_page_text(path)
        except OSError:
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
    """The reply with a leading code fence turned back into a `---`
    frontmatter block, ready for `core.parse_frontmatter`.

    A model fences the whole reply, or fences the frontmatter alone and
    leaves the body outside it. Text after the first closing fence is
    that second shape, and it decides everything: the fenced block is
    the frontmatter, and the `---` lines go back around it unless it
    already brought its own. With nothing after that fence, the reply is
    one fenced page, and only the fence on its last line closes it, so a
    fenced block inside the body keeps its own fences.

    A reply that opens with a fence it never closes is returned
    stripped, and so is a fenced page carrying no `---` line: wrapping
    that one would read its first prose line holding a colon as a
    frontmatter field and write a page with no body at all.
    """
    stripped = reply.strip()
    if not stripped.startswith(FENCE):
        return stripped
    lines = stripped.split("\n")
    close = next(
        (i for i, line in enumerate(lines[1:], 1) if line.rstrip() == FENCE), None
    )
    if close is not None and any(line.strip() for line in lines[close + 1 :]):
        block, body = lines[1:close], lines[close + 1 :]
        if block[:1] == ["---"]:
            return "\n".join(block + body)
        return "\n".join(["---", *block, "---", *body])

    page = "\n".join(lines[1:-1])
    if lines[-1].rstrip() == FENCE and page.startswith("---\n"):
        return page
    return stripped


def _unparseable_reason(reply: str) -> str:
    """A drop reason naming what arrived in place of a frontmatter
    block. The reply is not kept anywhere, so its opening line and its
    length are all a later reader of log.md gets."""
    opening = reply.strip().split("\n", 1)[0][:REPLY_EXCERPT_CHARS]
    return (
        "unparseable reply: want a --- frontmatter block, got "
        f"{len(reply)} chars beginning {opening!r}"
    )


def _drop_reason(
    parsed: tuple[dict[str, FrontmatterValue], str], title: str
) -> str | None:
    """Why a parsed reply must be dropped before anything reaches disk,
    or `None` to keep it. An absent `identifiers` key, or one holding a
    non-empty scalar or a blank item, drops. A present but empty value
    (`identifiers:` with nothing after it, or `identifiers: []`) is an
    empty list, same as `lint` reads it through `core.as_list`."""
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


def _free_summary_path(kb: Kb, digest: str, title: str) -> Path:
    """The filename for a digest with no summary page: the plain slug
    when it is free, else the slug suffixed with this digest's own hex
    digits, which no other digest collides on. CALLER MUST HOLD
    kb_lock. `Path.exists` is only a decision under the lock; outside
    it, it is a guess."""
    candidate = kb.wiki / f"{slugify(title)}.md"
    if not candidate.exists():
        return candidate
    return kb.wiki / f"{slugify(title)}-{digest[:SLUG_SUFFIX_LEN]}.md"


def _carry_story(
    fields: dict[str, FrontmatterValue], previous: bytes | None
) -> dict[str, FrontmatterValue]:
    """`fields` with the `story:` back-reference read off `previous`
    restored. dedup owns that field and a model reply never carries it,
    so a re-summarize without this leaves a story holding a member
    whose own page denies membership. `previous` of None returns
    `fields` unchanged."""
    if previous is None:
        return fields
    parsed = parse_frontmatter(previous.decode("utf-8", errors="replace"))
    if parsed is None:
        return fields
    story = parsed[0].get("story")
    if story:
        fields = dict(fields)
        fields["story"] = story
    return fields


def _build_fields(
    reply_fields: dict,
    digest: str,
    provenance: dict,
    model_used: str,
    fingerprint: str,
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
        "model": model_used,
        "prompt_fingerprint": fingerprint,
    }
    for key, value in reply_fields.items():
        fields.setdefault(key, value)
    return fields


def _fail(kb: Kb, digest: str, reason: str) -> None:
    """Record a drop by printing the reason to stderr before writing
    log.md. A missing log.md makes append_log_entry raise, and that
    raise cannot hide the drop, because stderr already carries it. The
    raise still ends the sweep right after this drop is reported. The
    same shape lives in ingest._fail."""
    message = f"{digest}: dropped ({reason})"
    print(f"llmwiki: summarize: {message}", file=sys.stderr)
    append_log_entry(kb.log, "summarize", message)


def _commit_summary_page(
    kb: Kb, digest: str, page_path: Path, previous: bytes | None, content: str
) -> bool:
    """Write `content` to `page_path`, self-lint, and roll back on any
    finding: previous bytes restored, or the file unlinked when there
    was none. Returns True iff the write was dropped. CALLER MUST HOLD
    kb_lock: `previous` was read under it and is only current while it
    is held."""
    atomic_write_text(page_path, content)
    findings = lint_pages(kb.root, [page_path])
    if not findings:
        return False
    if previous is not None:
        atomic_write_bytes(page_path, previous)
    else:
        page_path.unlink()
    finding = findings[0]
    _fail(kb, digest, f"{finding.check}: {finding.detail}")
    return True


def _resolve_content(
    kb: Kb, digest: str, prefix: str, pdf_part: str
) -> (
    tuple[str, tuple[str, bytes] | None, str, dict]
    | tuple[Literal["inert", "actionable"], str]
):
    """The prompt text, optional attachment, model step, and provenance
    for `digest` (a 4-tuple), or a `(category, reason)` pair when
    nothing can be sent. `category` is `"inert"` when the drop derives
    only from the immutable source bytes, so no rerun under any
    configuration can change it (bytes that are not UTF-8, or more of
    them than `MAX_SOURCE_TEXT_BYTES`); `"actionable"` for every other
    reason, since a missing sidecar can be restored, a permission bit
    can be fixed, and the provider's pdf_part can be edited.

    The recorded content_type decides one thing: whether the bytes ride
    as an attachment. Everything else is text when it decodes, so a
    source type nobody thought to list still reaches the model. Reads no
    lock; source bytes and provenance are immutable once
    `sources.store` writes them, so a plain read here needs none."""
    try:
        path = _source_path(kb, digest)
        provenance = read_provenance(kb, digest)
    except FileNotFoundError as exc:
        return "actionable", f"cannot read source: {exc}"
    except (OSError, ValueError) as exc:
        return "actionable", f"cannot read provenance: {exc}"

    content_type = str(provenance.get("content_type", ""))
    normalized = content_type.split(";")[0].strip().lower()

    if normalized not in _VISUAL_TYPES:
        try:
            size = path.stat().st_size
            if size > MAX_SOURCE_TEXT_BYTES:
                return "inert", (
                    f"source too large: {size} bytes exceeds the "
                    f"{MAX_SOURCE_TEXT_BYTES} byte cap"
                )
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            return "inert", f"cannot decode source: {exc}"
        except OSError as exc:
            return "actionable", f"cannot read source: {exc}"
        return prefix + SOURCE_DELIMITER + text, None, "summarize", provenance

    if normalized == _PDF_TYPE and pdf_part == "none":
        return "actionable", "PDF attachments disabled: set pdf_part on the provider"
    try:
        attachment = (normalized, path.read_bytes())
    except OSError as exc:
        return "actionable", f"cannot read source: {exc}"
    return (
        prefix + SOURCE_ATTACHMENT_NOTE,
        attachment,
        "summarize_image",
        provenance,
    )


def _image_target(kb: Kb) -> ModelTarget:
    """`[models].summarize_image` when configured, else
    `[models].summarize`. Resolving rather than naming means the
    fallback moves the url and the credential too, not just the model
    name. A present but malformed `summarize_image` id raises out of
    `resolve_target` instead of falling back."""
    if not step_is_configured(kb.config, "summarize_image"):
        return resolve_target(kb.config, "summarize")
    return resolve_target(kb.config, "summarize_image")


def _process_digest(
    kb: Kb,
    digest: str,
    prefix: str,
    fingerprint: str,
    text_target: ModelTarget,
    visual_target: ModelTarget,
) -> Literal["inert", "actionable"] | None:
    """Summarize one source. Returns `None` when a page was kept,
    `"inert"` when the drop derives only from the immutable source
    bytes (no rerun under any configuration can change it), or
    `"actionable"` for every other
    drop, before or after the model call: a permission bit, a sidecar,
    the provider's pdf_part, a prompt, or a config pattern could each
    make a rerun succeed. `_resolve_content` is gated on
    `visual_target.pdf_part`, the limit of the endpoint that will
    actually receive the PDF. Source bytes and provenance are read, and
    the model called, before any lock is taken; the commit window opens
    only once there is content to write."""
    resolved = _resolve_content(kb, digest, prefix, visual_target.pdf_part)
    if len(resolved) == 2:  # (category, reason); a 4-tuple is success
        category, reason = resolved
        _fail(kb, digest, reason)
        return category
    prompt, attachment, step, provenance = resolved

    target = visual_target if step == "summarize_image" else text_target

    reply = chat(target, prompt, attachment=attachment)
    parsed = parse_frontmatter(_unwrap_fence(reply))
    if parsed is None:
        _fail(kb, digest, _unparseable_reason(reply))
        return "actionable"

    title = str(parsed[0].get("title", "")).strip()
    reason = _drop_reason(parsed, title)
    if reason:
        _fail(kb, digest, reason)
        return "actionable"

    reply_fields, body = parsed
    fields = _build_fields(reply_fields, digest, provenance, target.id, fingerprint)

    with kb_lock(kb.root):
        index = _summary_index(kb)
        if digest in index:
            page_path = index[digest][0]
            previous = page_path.read_bytes()
        else:
            page_path = _free_summary_path(kb, digest, title)
            previous = None
        fields = _carry_story(fields, previous)
        content = render_frontmatter(fields, body)
        dropped = _commit_summary_page(kb, digest, page_path, previous, content)

    if dropped:
        return "actionable"

    append_log_entry(kb.log, "summarize", f"{fields['title']}: {digest}")
    return None


def run(root: Path, digests: list[str] | None) -> int:
    """Write a summary page for each of `digests`, or every stored
    source when `None`. Returns 1 when any drop was actionable, since
    a rerun could change it. An inert drop, which no rerun can rescue,
    fails the run only when the caller named explicit `digests`, so it
    never fails a bare sweep forever. `_resolve_content` defines the
    two categories. Every drop reason reaches stderr as well as
    log.md."""
    kb = Kb(root)
    targets = digests if digests is not None else _all_digests(kb)
    index = _summary_index(kb)
    prefix = prompt_prefix(kb)
    fingerprint = prompt_fingerprint(prefix)

    try:
        text_target = resolve_target(kb.config, "summarize")
        visual_target = _image_target(kb)
    except ModelError as exc:
        print(f"llmwiki: summarize: {exc}", file=sys.stderr)
        return 1

    remaining = [
        d for d in targets if index.get(d, (None, None))[1] != fingerprint
    ]
    print(f"summarize: {len(remaining)} planned")

    dropped_actionable = False
    dropped_inert = False
    for digest in remaining:
        try:
            kind = _process_digest(
                kb, digest, prefix, fingerprint, text_target, visual_target
            )
        except ModelError as exc:
            print(f"llmwiki: summarize: {exc}", file=sys.stderr)
            return 1
        if kind == "actionable":
            dropped_actionable = True
        elif kind == "inert":
            dropped_inert = True

    if dropped_actionable:
        return 1
    if dropped_inert and digests is not None:
        return 1
    return 0
