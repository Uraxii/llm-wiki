"""CLI entry point for llm-wiki.

Usage: `python3 -m llmwiki [--kb PATH] <verb> [args]`. `--kb`, when
given, must come before the verb and names the kb root directly (the
`.kb` directory itself, not its parent).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from llmwiki.core import Kb, append_log_entry, atomic_write_text
from llmwiki.lint import lint_pages, select_pages
from llmwiki import dedup, ingest, summarize, vectors

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SKELETON = REPO_ROOT / "docs" / "design" / "SCHEMA.skeleton.md"

GLOBAL_STORE = Path.home() / ".local" / "share" / "agent-kb"

CONFIG_TOML = """\
# One model per paid pipeline step, read from this file at run time.
[models]
summarize = "google/gemini-2.5-flash"
embed = "openai/text-embedding-3-small"

# The API endpoint that serves the models above.
# [endpoint]
# url = "https://api.example.com/v1"

# Identifier vocabulary. Each key can appear in a page's "identifiers"
# field as "key:value". The CLI appends this table to the summarizer
# prompt, so SUMMARIZE.md never repeats it. Uncomment and add your own
# keys; delete this example first.
# [identifiers.isbn]
# pattern = "^\\\\d{13}$"
# describe = "13-digit ISBN without hyphens"

# Scheduled ingest jobs. Each job fetches from a feed or a list of
# urls, in "partial" mode (skip urls already seen) or "full" mode
# (fetch everything; unchanged bytes are skipped by their hash).
# [jobs.example]
# feed = "https://example.com/feed.xml"
# mode = "partial"
"""

SUMMARIZE_STUB = """\
# Summarize

Write frontmatter with `title`, `kind: summary`, and `identifiers` (a
flat list of `key:value` strings; only use keys declared in
`config.toml`). The body is a short abstract of the source.

The CLI appends the declared identifier vocabulary from `config.toml`
below this prompt before it reaches the model.
"""


def _init_files() -> dict[str, str]:
    return {
        "config.toml": CONFIG_TOML,
        "SCHEMA.md": SCHEMA_SKELETON.read_text(encoding="utf-8"),
        "SUMMARIZE.md": SUMMARIZE_STUB,
        "log.md": "# log\n",
        ".gitignore": "vectors/\n",
    }


def resolve_root(explicit: str | None, for_init: bool) -> Path:
    """Resolve the kb root. `explicit` (from `--kb`) is used as-is when
    given. Otherwise `init` roots at `.kb` under the working directory;
    every other verb walks up from the working directory for an
    existing `.kb`, falling back to the user's global store."""
    if explicit is not None:
        return Path(explicit)
    if for_init:
        return Path.cwd() / ".kb"
    for candidate in (Path.cwd(), *Path.cwd().parents):
        found = candidate / ".kb"
        if found.is_dir():
            return found
    return GLOBAL_STORE


def cmd_init(root: Path, args: list[str]) -> int:
    if root.exists():
        print(f"llmwiki: init: {root} already exists", file=sys.stderr)
        return 1
    root.mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "wiki").mkdir()
    for name, content in _init_files().items():
        atomic_write_text(root / name, content)
    return 0


def cmd_where(root: Path, args: list[str]) -> int:
    print(root)
    return 0


def cmd_lint(root: Path, args: list[str]) -> int:
    kb = Kb(root)
    pages = [Path(a) for a in args] if args else None
    findings = lint_pages(root, pages)
    for finding in findings:
        print(f"{finding.path}\t{finding.check}\t{finding.detail}")
    page_count = len(select_pages(root, pages))
    append_log_entry(kb.log, "lint", f"{len(findings)} findings over {page_count} pages")
    return 1 if findings else 0


def cmd_summarize(root: Path, args: list[str]) -> int:
    return summarize.run(root, args or None)


def cmd_ingest(root: Path, args: list[str]) -> int:
    if "--job" in args:
        if len(args) != 2 or args[0] != "--job":
            print(_usage(), file=sys.stderr)
            return 2
        return ingest.run_job(root, args[1])
    if not args:
        print(_usage(), file=sys.stderr)
        return 2
    return ingest.run(root, args)


def cmd_dedup(root: Path, args: list[str]) -> int:
    if "--rebuild" in args:
        if args != ["--rebuild"]:
            print("llmwiki: dedup --rebuild takes no other arguments", file=sys.stderr)
            return 2
        return dedup.rebuild(root)
    return dedup.run(root, args or None)


def cmd_embed(root: Path, args: list[str]) -> int:
    paths = [Path(a) for a in args] if args else None
    return vectors.run(root, paths)


def cmd_status(root: Path, args: list[str]) -> int:
    if args:
        print(_usage(), file=sys.stderr)
        return 2
    return vectors.status(root)


def cmd_search(root: Path, args: list[str]) -> int:
    query_parts: list[str] = []
    n = vectors.TOP_K
    kind: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-n", "--kind"):
            if index + 1 >= len(args):
                print(_usage(), file=sys.stderr)
                return 2
            value = args[index + 1]
            if arg == "-n":
                try:
                    n = int(value)
                except ValueError:
                    print(_usage(), file=sys.stderr)
                    return 2
                if n < 1:
                    print(_usage(), file=sys.stderr)
                    return 2
            else:
                kind = value
            index += 2
        else:
            query_parts.append(arg)
            index += 1
    if not query_parts:
        print(_usage(), file=sys.stderr)
        return 2
    return vectors.search(root, " ".join(query_parts), n=n, kind=kind)


Verb = Callable[[Path, list[str]], int]
VERBS: dict[str, tuple[Verb, str]] = {
    "init": (cmd_init, "init            create a kb at the resolved root"),
    "where": (cmd_where, "where           print the resolved kb root"),
    "ingest": (
        cmd_ingest,
        "ingest <url|path>... | - | --job <name>   store sources and run the "
        "pipeline, or run one declared job",
    ),
    "summarize": (cmd_summarize, "summarize [<hash>...]  write a summary page per source"),
    "dedup": (
        cmd_dedup,
        "dedup [--rebuild] [<hash>...]  join or start a story per summary",
    ),
    "lint": (cmd_lint, "lint [<page>...] check wiki pages, one line per finding"),
    "embed": (
        cmd_embed,
        "embed [<page>...]  write a vector per wiki page, skipping current ones",
    ),
    "status": (cmd_status, "status          pages without a vector, sources without a summary"),
    "search": (
        cmd_search,
        "search <query> [-n N] [--kind K]  nearest pages, one line each",
    ),
}


def _usage() -> str:
    lines = ["usage: python3 -m llmwiki [--kb PATH] <verb> [args]", ""]
    lines.extend(usage for _fn, usage in VERBS.values())
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = list(argv)
    kb_path: str | None = None
    if args and args[0] == "--kb":
        if len(args) < 2:
            print(_usage(), file=sys.stderr)
            return 2
        kb_path, args = args[1], args[2:]

    if not args or args[0] not in VERBS:
        print(_usage(), file=sys.stderr)
        return 2

    verb, fn = args[0], VERBS[args[0]][0]
    try:
        root = resolve_root(kb_path, for_init=verb == "init")
        return fn(root, args[1:])
    except Exception as exc:
        print(f"llmwiki: {verb}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
