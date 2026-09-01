#!/usr/bin/env python3
"""Verify `<module>.py:<line>` and `<module>.py:<start>-<end>` citations in
docs/plans/**/*.md still point at the code the surrounding prose claims.

For each citation, the checker looks for a backticked identifier next to it
(the cited line's own text, falling back to the line above for citations
that wrap across a paragraph). When an identifier is found, its module is
parsed with `ast` and the citation is checked against that symbol's real
line span: a stale line number, a range that no longer contains the symbol,
or a symbol that no longer exists at all is reported.

A citation with no adjacent identifier gets only a bounds check (the line
falls inside the file). That is a much weaker guarantee: a citation whose
target shifted but is still inside the file passes silently. See the
module docstring in the repo's check for the exact class of rot this misses.

Does not import `llmwiki`, so it runs under any Python 3 interpreter.
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
import re

CODE_DIRS = ("llmwiki", "tests")

CITATION_RE = re.compile(
    r"([a-z_][a-z0-9_]*\.py):(\d+)(?:-(\d+))?"
)
FULL_CITATION_RE = re.compile(r"[a-z_][a-z0-9_]*\.py:\d+(?:-\d+)?")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOTTED_RE = re.compile(r"^([a-z_][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")
ASSIGN_LHS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class Citation:
    doc: Path
    doc_line: int
    text: str  # e.g. "vectors.py:67-72"
    module: str  # e.g. "vectors.py"
    start: int
    end: int


def find_citations(doc: Path) -> list[Citation]:
    out = []
    for lineno, line in enumerate(
        doc.read_text(encoding="utf-8").splitlines(), start=1
    ):
        for m in CITATION_RE.finditer(line):
            module, start_s, end_s = m.groups()
            start = int(start_s)
            end = int(end_s) if end_s else start
            out.append(
                Citation(doc, lineno, m.group(0), module, start, end)
            )
    return out


def find_module_file(module: str) -> Path | None:
    repo_root = Path(__file__).resolve().parent.parent
    for code_dir in CODE_DIRS:
        candidate = repo_root / code_dir / module
        if candidate.is_file():
            return candidate
    return None


def symbol_table(path: Path) -> dict[str, tuple[int, int]]:
    """Map every top-level or nested name (def, class, assignment) to its
    real (start_line, end_line), first definition wins."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    table: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            table.setdefault(node.name, (node.lineno, node.end_lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    table.setdefault(
                        target.id, (node.lineno, node.end_lineno)
                    )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            table.setdefault(
                node.target.id, (node.lineno, node.end_lineno)
            )
    return table


def is_strong_symbol_name(name: str) -> bool:
    """True for names this repo's convention marks as a real code symbol:
    a private helper (`_link_exclusive`) or a module constant
    (`NEIGHBOUR_FLOOR`). A plain lowercase word (`seen`, `kind`, `embed`)
    is too easily a local variable, a parameter, or a CLI verb quoted in
    prose rather than the literal symbol name, so it is left out: treating
    it as a candidate produced false "deleted symbol" reports for locals
    like `seen` and `k` that were never module-level names to begin with."""
    return name.startswith("_") or name.isupper()


def extract_candidates(text: str, module_stem: str) -> list[str]:
    """Identifiers named in backticks in `text` that plausibly refer to a
    symbol in the cited module."""
    candidates = []
    for m in BACKTICK_RE.finditer(text):
        content = m.group(1).strip()
        if FULL_CITATION_RE.fullmatch(content):
            continue  # a citation itself, not a symbol name
        dotted = DOTTED_RE.fullmatch(content)
        if dotted:
            module_ref, symbol = dotted.groups()
            if symbol == "py":
                continue  # "dedup.py" mentioned bare, not a dotted symbol
            if module_ref == module_stem:
                candidates.append(symbol)
            continue  # dotted to a different module: not ours to check
        if BARE_IDENT_RE.fullmatch(content) and is_strong_symbol_name(content):
            candidates.append(content)
            continue
        assign = ASSIGN_LHS_RE.match(content)
        if assign and is_strong_symbol_name(assign.group(1)):
            candidates.append(assign.group(1))
    return candidates


def nearby_candidates(doc_lines: list[str], doc_line: int, module_stem: str) -> list[str]:
    line = doc_lines[doc_line - 1]
    candidates = extract_candidates(line, module_stem)
    if candidates:
        return candidates
    if doc_line >= 2:
        return extract_candidates(doc_lines[doc_line - 2], module_stem)
    return []


def check(citation: Citation, doc_lines: list[str]) -> str | None:
    """Returns a reason string if stale, None if sound."""
    module_stem = citation.module[: -len(".py")]
    module_path = find_module_file(citation.module)
    if module_path is None:
        return f"module {citation.module} does not exist in {CODE_DIRS}"

    file_line_count = len(module_path.read_text(encoding="utf-8").splitlines())
    candidates = nearby_candidates(doc_lines, citation.doc_line, module_stem)

    if not candidates:
        if citation.start < 1 or citation.end > file_line_count:
            return (
                f"line {citation.start} is outside {citation.module} "
                f"({file_line_count} lines) -- no adjacent identifier to "
                f"check further"
            )
        return None  # weak: in-bounds is all we can verify

    table = symbol_table(module_path)
    unresolved = []
    wrong_span = None
    for name in candidates:
        span = table.get(name)
        if span is None:
            unresolved.append(name)
            continue
        true_start, true_end = span
        if true_start <= citation.start and citation.end <= true_end:
            return None  # verified
        wrong_span = wrong_span or (name, true_start, true_end)

    if unresolved:
        return (
            f"cites `{unresolved[0]}`, not found in {citation.module} "
            f"(deleted or renamed)"
        )
    name, true_start, true_end = wrong_span
    return (
        f"`{name}` is at {citation.module}:{true_start}-{true_end}, "
        f"citation says {citation.start}"
        + (f"-{citation.end}" if citation.end != citation.start else "")
    )


def scan(plans_root: Path) -> list[tuple[Citation, str]]:
    stale = []
    for doc in sorted(plans_root.rglob("*.md")):
        doc_lines = doc.read_text(encoding="utf-8").splitlines()
        for citation in find_citations(doc):
            reason = check(citation, doc_lines)
            if reason is not None:
                stale.append((citation, reason))
    return stale


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plans-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "plans",
        help="directory to scan for *.md files (default: docs/plans)",
    )
    args = parser.parse_args(argv)

    stale = scan(args.plans_root)
    for citation, reason in stale:
        print(f"{citation.doc}:{citation.doc_line}: {citation.text}: {reason}")

    if stale:
        print(f"{len(stale)} stale citation(s) found", file=sys.stderr)
        return 1
    print("all citations sound", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
