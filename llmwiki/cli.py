"""CLI entry point for llm-wiki.

Usage: `llmwiki [--kb PATH] <verb> [args]`. `--kb`, when
given, must come before the verb and names the kb root directly (the
`.kb` directory itself, not its parent).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import NamedTuple

from llmwiki.core import Kb, append_log_entry, atomic_write_text
from llmwiki.lint import lint_pages, select_pages
from llmwiki.model import ModelError
from llmwiki.remotes import (
    LOCAL_LABEL,
    Answer,
    Remote,
    RemoteError,
    RemoteFailure,
    RemoteHit,
    RemoteRanking,
)
from llmwiki import dedup, ingest, remotes, summarize, vectors

SCHEMA_SKELETON = resources.files("llmwiki").joinpath("SCHEMA.skeleton.md")

GLOBAL_STORE = Path.home() / ".local" / "share" / "llm-wiki"

CONFIG_TOML = """\
# One model per paid pipeline step, read from this file at run time.
# Each id is "<provider>:<model>": the prefix names a table under
# [providers] below, and the rest is a model that provider serves. The
# prefix is always required. Replace these with ids your own endpoints
# serve.
[models]
summarize = "hosted:your-summarize-model"
embed = "desktop:your-embed-model"
dedup = "hosted:your-judge-model"

# Optional. A vision model for images and PDFs. Unset, visual sources
# use [models].summarize.
# summarize_image = "hosted:your-vision-model"

# One table per endpoint the ids above name. Uncomment and set your own
# urls; until then every paid step fails with "[models].summarize names
# provider "hosted", but [providers.hosted] is not in config.toml".
#
# key_env names the environment variable holding that provider's API
# key. key_file_env names one holding a path to read the key from. Set
# neither and no Authorization header is sent, which is what a server on
# your own machine usually wants. Never put a key value in this file.
#
# pdf_part is how this endpoint takes a PDF: "file", "image_url", or
# "none" to never send one. It defaults to "file".
# [providers.hosted]
# url = "https://api.example.com/v1"
# key_env = "LLM_WIKI_API_KEY_HOSTED"
# pdf_part = "file"

# [providers.desktop]
# url = "http://127.0.0.1:1234/v1"

# Identifier vocabulary. Each key can appear in a page's "identifiers"
# field as "key:value". The CLI appends this table to the summarizer
# prompt, so SUMMARIZE.md never repeats it. A key is a JOIN key: dedup
# joins two summaries that share one, so declare only keys that
# discriminate one subject from another (an isbn does; an ingredient
# name does not, it joins every page that uses salt). Edit or replace
# this example with your own.
[identifiers.isbn]
pattern = "^\\\\d{13}$"
describe = "13-digit ISBN without hyphens"

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
    print(
        f"llmwiki: init: wrote {root}; edit config.toml and set "
        "your own [providers] and [models] before running ingest"
    )
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


class SearchArgs(NamedTuple):
    query: str
    n: int
    kind: str | None
    names: tuple[str, ...]   # --remote NAME, in the order given
    every: bool              # --all


VALUE_FLAGS = ("-n", "--kind", "--remote")


def parse_search_args(args: list[str]) -> SearchArgs | None:
    """`None` on any usage error, which the caller turns into exit 2."""
    query_parts: list[str] = []
    n, kind, names, every = vectors.TOP_K, None, [], False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--all":
            every = True
            index += 1
            continue
        if arg not in VALUE_FLAGS:
            query_parts.append(arg)
            index += 1
            continue
        if index + 1 >= len(args):
            return None
        value = args[index + 1]
        if arg == "--kind":
            kind = value
        elif arg == "--remote":
            names.append(value)
        else:
            try:
                n = int(value)
            except ValueError:
                return None
            if n < 1:
                return None
        index += 2
    if not query_parts:
        return None
    return SearchArgs(" ".join(query_parts), n, kind, tuple(names), every)


def select_remotes(
    config: dict, names: tuple[str, ...], every: bool
) -> list[Remote]:
    """The remotes to ask, in the order `--remote` named them, then the
    rest of the table when `--all` is given. Raises ValueError, which
    the caller turns into exit 2, for an unknown name or a bad table."""
    table = remotes.parse_remotes(config)
    chosen = []
    for name in names:
        if name not in table:
            raise ValueError(f"unknown remote: {name}")
        if table[name] not in chosen:
            chosen.append(table[name])
    if every:
        chosen.extend(r for r in table.values() if r not in chosen)
    return chosen


def local_answer(kb: Kb, query: str, n: int, kind: str | None) -> Answer:
    """The kb on disk as one more participant, under the label `local`.
    Its failures carry the same codes the routes answer with, so one
    stderr line reads the same whichever wiki produced it."""
    try:
        ranking = vectors.rank(kb, query, n, kind)
    except vectors.StaleVectors:
        return RemoteFailure(LOCAL_LABEL, "index_stale")
    except vectors.NoEmbedModel:
        return RemoteFailure(LOCAL_LABEL, "no_embed_model")
    except ModelError:
        return RemoteFailure(LOCAL_LABEL, "upstream_model_failed")
    hits = tuple(
        RemoteHit(rank, hit.name, hit.title, hit.updated, hit.size)
        for rank, hit in enumerate(ranking.hits, start=1)
    )
    return RemoteRanking(LOCAL_LABEL, hits)


def print_answers(answers: tuple[Answer, ...]) -> int:
    """One block per wiki, in the order asked, never merged and never
    re-sorted. `rank` is a position inside one block, so no number on
    any line is comparable with a number in another block."""
    failed = False
    for answer in answers:
        if isinstance(answer, RemoteFailure):
            print(f"{answer.remote}\t{answer.code}", file=sys.stderr)
            failed = True
            continue
        print(f"# {answer.remote}")
        for hit in answer.hits:
            print(
                f"{hit.rank}\t{hit.name}\t{hit.title}\t"
                f"{hit.updated}\t{hit.size}"
            )
    return 1 if failed else 0


def cmd_search(root: Path, args: list[str]) -> int:
    parsed = parse_search_args(args)
    if parsed is None:
        print(_usage(), file=sys.stderr)
        return 2
    if not parsed.names and not parsed.every:
        return vectors.search(root, parsed.query, n=parsed.n, kind=parsed.kind)
    kb = Kb(root)
    try:
        selected = select_remotes(kb.config, parsed.names, parsed.every)
    except ValueError as exc:
        print(f"llmwiki: search: {exc}", file=sys.stderr)
        return 2
    answers: tuple[Answer, ...] = ()
    if kb.wiki.is_dir():
        answers += (local_answer(kb, parsed.query, parsed.n, parsed.kind),)
    answers += remotes.fan_out(selected, parsed.query, parsed.n, parsed.kind)
    return print_answers(answers)


def local_page(kb: Kb, name: str) -> Path | None:
    """The page's resolved path, or `None`. The resolved parent must
    equal `kb.wiki.resolve()`, equality rather than `is_relative_to` so
    a page in a subdirectory is refused too, and the suffix must be
    `.md`. Both sides resolved, because a symlinked kb root is a normal
    deployment. An embedded NUL makes `resolve` raise ValueError."""
    try:
        candidate = (kb.wiki / name).resolve()
    except ValueError:
        return None
    if candidate.parent != kb.wiki.resolve() or candidate.suffix != ".md":
        return None
    return candidate


def cmd_page(root: Path, args: list[str]) -> int:
    name, remote_name = None, None
    index = 0
    while index < len(args):
        if args[index] == "--remote":
            if index + 1 >= len(args):
                print(_usage(), file=sys.stderr)
                return 2
            remote_name, index = args[index + 1], index + 2
            continue
        if name is not None:
            print(_usage(), file=sys.stderr)
            return 2
        name, index = args[index], index + 1
    if name is None:
        print(_usage(), file=sys.stderr)
        return 2
    kb = Kb(root)
    if remote_name is None:
        return _write_local_page(kb, name)
    try:
        (remote,) = select_remotes(kb.config, (remote_name,), False)
    except ValueError as exc:
        print(f"llmwiki: page: {exc}", file=sys.stderr)
        return 2
    try:
        sys.stdout.buffer.write(remotes.page(remote, name))
    except RemoteError as exc:
        print(f"{remote.name}\t{exc.code}", file=sys.stderr)
        return 1
    return 0


def _write_local_page(kb: Kb, name: str) -> int:
    path = local_page(kb, name)
    try:
        body = path.read_bytes() if path is not None else None
    except OSError:
        body = None
    if body is None:
        print(f"{LOCAL_LABEL}\tnot_found", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(body)
    return 0


Verb = Callable[[Path, list[str]], int]


class VerbSpec(NamedTuple):
    """A verb's runner, its usage line, and every `--option` it takes.
    `options` has no default: a new verb must state its own, because
    `main` refuses any other `--` token before the verb runs."""

    run: Verb
    usage: str
    options: frozenset[str]


VERBS: dict[str, VerbSpec] = {
    "init": VerbSpec(
        cmd_init, "init            create a kb at the resolved root",
        frozenset(),
    ),
    "where": VerbSpec(
        cmd_where, "where           print the resolved kb root", frozenset(),
    ),
    "ingest": VerbSpec(
        cmd_ingest,
        "ingest <url|path>... | - | --job <name>   store sources and run the "
        "pipeline, or run one declared job",
        frozenset({"--job"}),
    ),
    "summarize": VerbSpec(
        cmd_summarize, "summarize [<hash>...]  write a summary page per source",
        frozenset(),
    ),
    "dedup": VerbSpec(
        cmd_dedup,
        "dedup [--rebuild] [<hash>...]  join or start a story per summary",
        frozenset({"--rebuild"}),
    ),
    "lint": VerbSpec(
        cmd_lint, "lint [<page>...] check wiki pages, one line per finding",
        frozenset(),
    ),
    "embed": VerbSpec(
        cmd_embed,
        "embed [<page>...]  write a vector per wiki page, skipping current ones",
        frozenset(),
    ),
    "status": VerbSpec(
        cmd_status,
        "status          pages without a vector, sources without a summary",
        frozenset(),
    ),
    "search": VerbSpec(
        cmd_search,
        "search <query> [-n N] [--kind K] [--remote NAME]... [--all]  "
        "nearest pages, one line each",
        frozenset({"--kind", "--remote", "--all"}),
    ),
    "page": VerbSpec(
        cmd_page, "page [--remote NAME] <page>  print one page's bytes",
        frozenset({"--remote"}),
    ),
}

GLOBAL_OPTIONS = frozenset({"--kb"})


def _usage() -> str:
    lines = ["usage: llmwiki [--kb PATH] <verb> [args]", ""]
    lines.extend(spec.usage for spec in VERBS.values())
    return "\n".join(lines)


def stray_option(verb: str, args: list[str]) -> str | None:
    """The first `--` token in `args` that `verb` does not declare, or
    `None`. Anything a verb does not declare would be read as a path, a
    hash, or a query, so a misplaced `--kb` would silently retarget the
    run at another kb. Single-dash arguments are untouched: `ingest -`
    reads stdin and `search -n` takes a count."""
    options = VERBS[verb].options
    return next(
        (a for a in args if a.startswith("--") and a not in options), None
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] in ("--help", "-h"):
        print(_usage())
        return 0
    kb_path: str | None = None
    if args and args[0] == "--kb":
        if len(args) < 2:
            print(_usage(), file=sys.stderr)
            return 2
        kb_path, args = args[1], args[2:]

    if not args or args[0] not in VERBS:
        print(_usage(), file=sys.stderr)
        return 2

    verb, fn = args[0], VERBS[args[0]].run
    stray = stray_option(verb, args[1:])
    if stray is not None:
        misplaced = (
            f"{stray} must come before the verb"
            if stray in GLOBAL_OPTIONS
            else f"unknown option {stray}"
        )
        print(f"llmwiki: {verb}: {misplaced}\n{_usage()}", file=sys.stderr)
        return 2
    try:
        root = resolve_root(kb_path, for_init=verb == "init")
        return fn(root, args[1:])
    except Exception as exc:
        print(f"llmwiki: {verb}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
