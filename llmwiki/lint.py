"""Wiki lint: seven mechanical checks over every page under `wiki/`.

No warnings, no severities, no auto-fix. `lint_pages` returns every
`Finding`; the CLI verb and ingest both call it and decide what to do
with the result.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from llmwiki.core import (
    FrontmatterValue,
    Kb,
    as_list,
    parse_frontmatter,
    read_page_text,
    slugify,
)

CLI_KINDS = {"summary", "story"}
WIKILINK = re.compile(r"\[\[([^\]|#]+)")
DUPLICATE_TITLE_NAMES_SHOWN = 5  # a bigger slug group summarizes the rest as a count

ParsedPage = tuple[dict[str, FrontmatterValue], str] | None


class Finding(NamedTuple):
    path: Path
    check: str
    detail: str


class _Context(NamedTuple):
    """Cross-page facts a single-page check cannot see on its own."""

    config: dict
    stems: dict[str, str]  # page filename stem -> kind (filenames are unique, no collision)
    # title slug -> (page, kind) for every page with that title
    pages_by_title_slug: dict[str, list[tuple[Path, str]]]
    summary_hashes: set[str]  # source hashes claimed by a summary page
    source_hashes: set[str]  # hashes with a byte file under sources/


def _check_frontmatter(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is not None:
        return []
    return [Finding(path, "frontmatter", "frontmatter block missing or malformed")]


def _check_identifier_key(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, _body = parsed
    declared = ctx.config.get("identifiers", {})
    findings = []
    for ident in as_list(fields.get("identifiers")):
        key = ident.partition(":")[0]
        if key not in declared:
            findings.append(Finding(path, "identifier-key", f"{key!r} not declared"))
    return findings


def _check_identifier_value(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, _body = parsed
    declared = ctx.config.get("identifiers", {})
    findings = []
    for ident in as_list(fields.get("identifiers")):
        key, _, value = ident.partition(":")
        spec = declared.get(key)
        if spec is None:
            continue  # undeclared key is identifier-key's finding, not this one
        pattern = spec.get("pattern")
        if not value:
            findings.append(Finding(path, "identifier-value", f"{ident!r} has an empty value"))
        elif pattern and not re.fullmatch(pattern, value):
            findings.append(Finding(path, "identifier-value", f"{ident!r} fails pattern {pattern!r}"))
    return findings


def _resolves_to_summary(target: str, ctx: _Context) -> bool:
    """A wikilink target resolves against a page's exact filename stem
    first, since that is the more specific match. Only when no stem
    matches does it fall back to the title-slug group."""
    stem_kind = ctx.stems.get(target)
    if stem_kind is not None:
        return stem_kind == "summary"
    group = ctx.pages_by_title_slug.get(slugify(target), ())
    return any(kind == "summary" for _page, kind in group)


def _check_cites_summary(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, body = parsed
    if fields.get("kind") in CLI_KINDS:
        return []
    findings = []
    for link in WIKILINK.findall(body):
        target = link.strip()
        if _resolves_to_summary(target, ctx):
            findings.append(Finding(path, "cites-summary", f"[[{target}]] is a summary"))
    return findings


def _check_dangling_source(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, _body = parsed
    if fields.get("kind") != "summary":
        return []
    source = str(fields.get("source", ""))
    if source not in ctx.source_hashes:
        return [Finding(path, "dangling-source", f"source {source!r} not in sources/")]
    return []


def _duplicate_title_detail(slug: str, others: list[str]) -> str:
    shown = others[:DUPLICATE_TITLE_NAMES_SHOWN]
    detail = f"title slug {slug!r} also used by {', '.join(shown)}"
    remaining = len(others) - len(shown)
    if remaining:
        detail += f" and {remaining} more"
    return detail


def _check_duplicate_title(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    """Reports an agent page only, and only against another agent
    page. Summary and story pages may legally share a title, because
    `summarize._free_summary_path` and `dedup` disambiguate at the
    filename. Firing on a CLI page would make summarize's self-lint
    drop a good page."""
    if parsed is None:
        return []
    fields, _body = parsed
    title = fields.get("title")
    if not title:
        return []
    if str(fields.get("kind", "")) in CLI_KINDS:
        return []
    slug = slugify(str(title))
    group = ctx.pages_by_title_slug.get(slug, ())
    others = sorted(p.name for p, kind in group if p != path and kind not in CLI_KINDS)
    if not others:
        return []
    return [Finding(path, "duplicate-title", _duplicate_title_detail(slug, others))]


def _check_story_member(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, _body = parsed
    if fields.get("kind") != "story":
        return []
    findings = []
    for member in as_list(fields.get("members")):
        if member not in ctx.summary_hashes:
            findings.append(Finding(path, "story-member", f"member {member!r} is not a summary source"))
    return findings


CHECKS: list[tuple[str, Callable[[Path, ParsedPage, _Context], list[Finding]]]] = [
    ("frontmatter", _check_frontmatter),
    ("identifier-key", _check_identifier_key),
    ("identifier-value", _check_identifier_value),
    ("cites-summary", _check_cites_summary),
    ("dangling-source", _check_dangling_source),
    ("story-member", _check_story_member),
    ("duplicate-title", _check_duplicate_title),
]


def _build_context(kb: Kb, parsed_by_path: dict[Path, ParsedPage]) -> _Context:
    stems: dict[str, str] = {}
    pages_by_title_slug: dict[str, list[tuple[Path, str]]] = {}
    summary_hashes: set[str] = set()
    for path, parsed in parsed_by_path.items():
        if parsed is None:
            continue
        fields, _body = parsed
        kind = str(fields.get("kind", ""))
        stems[path.stem] = kind
        title = fields.get("title")
        if title:
            slug = slugify(str(title))
            pages_by_title_slug.setdefault(slug, []).append((path, kind))
        if kind == "summary" and fields.get("source"):
            summary_hashes.add(str(fields["source"]))
    source_hashes = {
        p.stem for p in kb.sources.glob("*") if p.is_file() and p.suffix != ".toml"
    }
    return _Context(kb.config, stems, pages_by_title_slug, summary_hashes, source_hashes)


def _filter_pages(all_pages: list[Path], pages: list[Path] | None) -> list[Path]:
    """`all_pages` itself, or the subset matching `pages` (resolved so a
    caller can pass paths relative to its own cwd)."""
    if pages is None:
        return all_pages
    wanted = {p.resolve() for p in pages}
    return [p for p in all_pages if p.resolve() in wanted]


def select_pages(root: Path, pages: list[Path] | None) -> list[Path]:
    """Every page under `wiki/`, or the subset of those matching
    `pages`. One glob of `wiki/*.md`."""
    kb = Kb(root)
    return _filter_pages(sorted(kb.wiki.glob("*.md")), pages)


def _read_pages(paths: list[Path]) -> dict[Path, ParsedPage]:
    """Parse every page in `paths` that still exists and can be read. A
    page unlinked between the caller's glob and this read, or one that
    raises on open (for example permission-denied), is dropped: absent
    from the returned dict, never reported as a finding and never
    treated as an unparseable page."""
    parsed_by_path: dict[Path, ParsedPage] = {}
    for path in paths:
        try:
            text = read_page_text(path)
        except OSError:
            continue
        parsed_by_path[path] = parse_frontmatter(text)
    return parsed_by_path


def lint_pages(root: Path, pages: list[Path] | None = None) -> list[Finding]:
    """Run all seven checks over `pages` (every page under `wiki/` when
    `None`). Cross-page context (identifier vocabulary, source hashes,
    summary hashes, wikilink targets) always comes from the whole wiki,
    even when linting a subset. One glob of `wiki/*.md`, then one read
    per page found: a page unlinked, or unreadable, between the glob
    and its read is dropped from the run, never raised on."""
    kb = Kb(root)
    all_pages = sorted(kb.wiki.glob("*.md"))
    targets = _filter_pages(all_pages, pages)
    parsed_by_path = _read_pages(all_pages)
    ctx = _build_context(kb, parsed_by_path)

    findings: list[Finding] = []
    for path in targets:
        if path not in parsed_by_path:
            continue
        parsed = parsed_by_path[path]
        for _name, check in CHECKS:
            findings.extend(check(path, parsed, ctx))
    return findings


def prompt_block(config: dict) -> str:
    """Render the declared `[identifiers]` table for the summarizer
    prompt, so SUMMARIZE.md never repeats it."""
    declared = config.get("identifiers", {})
    if not declared:
        return "No identifier keys are declared. Emit an empty identifiers list."
    lines = ["Declared identifier keys (key, pattern, description):"]
    for key, spec in declared.items():
        pattern = spec.get("pattern", "(any non-empty value)")
        describe = spec.get("describe", key)
        lines.append(f"- {key}\t{pattern}\t{describe}")
    return "\n".join(lines)
