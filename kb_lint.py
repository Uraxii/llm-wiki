"""Substrate lint: mechanical checks over every page under wiki/.

Six checks, no warnings, no severities, no auto-fix. Output one line per
finding as `path<TAB>check<TAB>detail`, exit 1 if any, one log.md line.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

from llm_wiki import append_log_entry, slugify

CLI_KINDS = {"summary", "story"}
WIKILINK = re.compile(r"\[\[([^\]|#]+)")


class Finding(NamedTuple):
    path: Path
    check: str
    detail: str


def parse_frontmatter(text: str) -> dict[str, str | list[str]] | None:
    """YAML subset: `key: scalar`, `key: [a, b]`, or `key:` followed by
    `- item` lines. Returns None when the block is missing or malformed."""
    if not text.startswith("---\n"):
        return None
    lines = text.split("\n")[1:]
    if "---" not in lines:
        return None
    fields: dict[str, str | list[str]] = {}
    key = None
    for line in lines[: lines.index("---")]:
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if not isinstance(fields.get(key), list):
                return None
            fields[key].append(unquote(line.lstrip()[2:]))
            continue
        if ":" not in line or line[0].isspace():
            return None
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [unquote(v) for v in value[1:-1].split(",") if v.strip()]
        else:
            fields[key] = [] if value == "" else unquote(value)
    return fields


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_config(kb: Path) -> dict:
    path = kb / "config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def prompt_block(config: dict) -> str:
    """Declared identifier table, rendered for the summarizer prompt."""
    declared = config.get("identifiers", {})
    if not declared:
        return "No identifier keys are declared. Emit an empty identifiers list."
    lines = ["Declared identifier keys (key, pattern, description):"]
    for key, spec in declared.items():
        lines.append(f"- {key}\t{spec.get('pattern', '(any non-empty value)')}\t{spec.get('describe', key)}")
    return "\n".join(lines)


def as_list(value: str | list[str] | None) -> list[str]:
    return value if isinstance(value, list) else [value] if value else []


def lint_pages(kb: Path, pages: list[Path] | None = None) -> list[Finding]:
    declared = load_config(kb).get("identifiers", {})
    source_hashes = {p.stem for p in (kb / "sources").glob("*") if p.is_file()}
    # Context comes from the whole wiki even when linting a subset: wikilink
    # targets and story members may live on pages outside `pages`.
    all_pages = sorted((kb / "wiki").glob("*.md"))
    parsed = {p: parse_frontmatter(p.read_text(encoding="utf-8")) for p in all_pages}
    kind_by_slug: dict[str, str] = {}
    summary_hashes: set[str] = set()
    for p, fm in parsed.items():
        if fm is None:
            continue
        kind = str(fm.get("kind", ""))
        kind_by_slug[p.stem] = kind
        if fm.get("title"):
            kind_by_slug[slugify(str(fm["title"]))] = kind
        if kind == "summary" and fm.get("source"):
            summary_hashes.add(str(fm["source"]))

    findings: list[Finding] = []
    for p in pages or all_pages:
        fm = parsed.get(p)
        if fm is None:
            fm = parse_frontmatter(p.read_text(encoding="utf-8"))
        if fm is None:
            findings.append(Finding(p, "frontmatter", "frontmatter missing or unparseable"))
            continue
        kind = str(fm.get("kind", ""))
        for ident in as_list(fm.get("identifiers")):
            key, _, value = ident.partition(":")
            if key not in declared:
                findings.append(Finding(p, "identifier-key", f"{key!r} not declared"))
            elif not value or ("pattern" in declared[key] and not re.search(declared[key]["pattern"], value)):
                findings.append(Finding(p, "identifier-value", f"{ident!r} fails pattern"))
        if kind not in CLI_KINDS:
            for link in WIKILINK.findall(p.read_text(encoding="utf-8")):
                if kind_by_slug.get(slugify(link.strip())) == "summary":
                    findings.append(Finding(p, "cites-summary", f"[[{link.strip()}]] is a summary"))
        if kind == "summary" and str(fm.get("source", "")) not in source_hashes:
            findings.append(Finding(p, "dangling-source", f"source {fm.get('source')!r} not in sources/"))
        if kind == "story":
            for member in as_list(fm.get("members")):
                if member not in summary_hashes:
                    findings.append(Finding(p, "story-member", f"member {member!r} is not a summary hash"))
    return findings


def main(argv: list[str]) -> int:
    kb = Path(".kb")
    if argv[:1] == ["--kb"]:
        kb, argv = Path(argv[1]), argv[2:]
    pages = [Path(a) for a in argv] or None
    findings = lint_pages(kb, pages)
    for f in findings:
        print(f"{f.path}\t{f.check}\t{f.detail}")
    (kb / "log.md").touch()
    append_log_entry(kb, "lint", f"{len(findings)} findings over {len(pages or list((kb / 'wiki').glob('*.md')))} pages")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
