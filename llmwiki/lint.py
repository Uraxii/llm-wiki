"""Wiki lint: six mechanical checks over every page under `wiki/`.

No warnings, no severities, no auto-fix. `lint_pages` returns every
`Finding`; the CLI verb and ingest both call it and decide what to do
with the result.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from llmwiki.core import FrontmatterValue, Kb, parse_frontmatter, slugify

CLI_KINDS = {"summary", "story"}
WIKILINK = re.compile(r"\[\[([^\]|#]+)")

ParsedPage = tuple[dict[str, FrontmatterValue], str] | None


class Finding(NamedTuple):
    path: Path
    check: str
    detail: str


class _Context(NamedTuple):
    """Cross-page facts a single-page check cannot see on its own."""

    config: dict
    kind_by_key: dict[str, str]  # page stem and title slug -> kind
    summary_hashes: set[str]  # source hashes claimed by a summary page
    source_hashes: set[str]  # hashes with a file under sources/


def _as_list(value: FrontmatterValue | None) -> list[str]:
    if isinstance(value, list):
        return value
    return [value] if value else []


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
    for ident in _as_list(fields.get("identifiers")):
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
    for ident in _as_list(fields.get("identifiers")):
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


def _check_cites_summary(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, body = parsed
    if fields.get("kind") in CLI_KINDS:
        return []
    findings = []
    for link in WIKILINK.findall(body):
        target = link.strip()
        if ctx.kind_by_key.get(slugify(target)) == "summary":
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


def _check_story_member(path: Path, parsed: ParsedPage, ctx: _Context) -> list[Finding]:
    if parsed is None:
        return []
    fields, _body = parsed
    if fields.get("kind") != "story":
        return []
    findings = []
    for member in _as_list(fields.get("members")):
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
]


def _build_context(kb: Kb, parsed_by_path: dict[Path, ParsedPage]) -> _Context:
    kind_by_key: dict[str, str] = {}
    summary_hashes: set[str] = set()
    for path, parsed in parsed_by_path.items():
        if parsed is None:
            continue
        fields, _body = parsed
        kind = str(fields.get("kind", ""))
        kind_by_key[path.stem] = kind
        title = fields.get("title")
        if title:
            kind_by_key[slugify(str(title))] = kind
        if kind == "summary" and fields.get("source"):
            summary_hashes.add(str(fields["source"]))
    source_hashes = {p.stem for p in kb.sources.glob("*") if p.is_file()}
    return _Context(kb.config, kind_by_key, summary_hashes, source_hashes)


def select_pages(root: Path, pages: list[Path] | None) -> list[Path]:
    """Every page under `wiki/`, or the subset of those matching `pages`
    (resolved so a caller can pass paths relative to its own cwd)."""
    kb = Kb(root)
    all_pages = sorted(kb.wiki.glob("*.md"))
    if pages is None:
        return all_pages
    wanted = {p.resolve() for p in pages}
    return [p for p in all_pages if p.resolve() in wanted]


def lint_pages(root: Path, pages: list[Path] | None = None) -> list[Finding]:
    """Run all six checks over `pages` (every page under `wiki/` when
    `None`). Cross-page context (identifier vocabulary, source hashes,
    summary hashes, wikilink targets) always comes from the whole wiki,
    even when linting a subset."""
    kb = Kb(root)
    targets = select_pages(root, pages)
    all_pages = select_pages(root, None)
    parsed_by_path = {p: parse_frontmatter(p.read_text(encoding="utf-8")) for p in all_pages}
    ctx = _build_context(kb, parsed_by_path)

    findings: list[Finding] = []
    for path in targets:
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
