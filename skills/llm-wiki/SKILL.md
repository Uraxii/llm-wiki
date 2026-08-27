---
name: llm-wiki
description: Read and write an llm-wiki knowledgebase (a project-local `.kb` or the global `~/.local/share/agent-kb` store) through the `llm-wiki` CLI. Use whenever you would otherwise cite a raw source without keeping it, need to record a finding so it survives past this session, or are about to re-derive an answer you likely already wrote down before. Covers capturing a source, writing or extending a wiki page, regenerating the index, checking backlinks, and logging what happened. There is no search verb; agents grep.
---

# llm-wiki

A live model of an environment plus the accumulated reasoning over it,
stored as plain files. Three layers, no service, no database.

| Layer | Rule |
|---|---|
| `sources/` | Immutable. Written once, never edited, never deleted. |
| `wiki/` | The model owns it. Pages are written and kept updated as new sources land. |
| `SCHEMA.md` | The configuration that matters: conventions and which retrieval path to take. |

Install: `uv tool install .` (or `pipx install .`) from the repo root.

## Verbs

```
llm-wiki init [PATH]     create the kb tree, default ./.kb
llm-wiki where           print which kb resolves, and how
llm-wiki add TITLE       write a source, body from stdin, or --url to fetch one
llm-wiki page TITLE      write or update a wiki page, body from stdin, or --touch to re-stamp only
llm-wiki index           repair-only: regenerate wiki/index.md (add/page do this already)
llm-wiki log KIND TITLE  repair-only: append one entry to log.md (add/page do this already)
llm-wiki links PAGE      print pages that link to PAGE
```

`add` and `page` are the only write paths into the kb. Both log the write
and regenerate `wiki/index.md` themselves right after, so `log.md` and
`index.md` never drift from what is on disk. Reach for `index`/`log`
directly only to repair them by hand.

Every verb accepts `--kb PATH` to override resolution. Without it: walk up
from cwd for a `.kb` directory, else fall back to the global store
`~/.local/share/agent-kb` (honouring `XDG_DATA_HOME`).

Capture a source, body from stdin:

```
llm-wiki add "Widget Catalog" --origin research \
    --source "https://example.com/widgets" <<'EOF'
body text goes here
EOF
```

`--origin` is `curated` (default), `research`, or `collector`. `add` never
writes `job` or `mode`; those are written by the collector itself, which
does not exist yet.

Capture a source by fetching a URL instead (mutually exclusive with
stdin; `source` frontmatter is set to the URL automatically):

```
llm-wiki add --url "https://example.com/widgets"
```

This path needs `readability-lxml` and `lxml` installed; every other verb
runs on the standard library alone. A missing dependency or a network
failure exits loudly before anything is written.

Write or update a wiki page, body from stdin:

```
llm-wiki page "Widget Catalog" --summary "one line" --category ref <<'EOF'
page body goes here
EOF
```

`page` always reads a real body from stdin to EOF, no timeout: empty or
whitespace-only stdin is a loud error, never a silent no-op. Updating a
page replaces the body with stdin and keeps any frontmatter field not
passed as a flag; `updated` is always re-stamped.

Re-stamp `updated` without touching the body, since a page holds
reasoning that exists nowhere else:

```
llm-wiki page "Widget Catalog" --touch
```

`--touch` never reads stdin; piping anything alongside it is refused as
ambiguous, and it requires the page to already exist.

Check what links to a page:

```
llm-wiki links widget-supplier
```

`links` scans `wiki/` only; a `[[wikilink]]` written in a `sources/` file is
not reported, since backlinks are a wiki-layer concept.

Read the log if `add`/`page` ever need auditing:

```
grep "^## \[" .kb/log.md | tail -5
```

## Frontmatter contracts

Scalars only, no nested values, no lists.

Source frontmatter: `origin`, `source`, `collected_at`, and for a collector
snapshot additionally `job`, `mode` (`full` or `partial`).

Page frontmatter: `title`, `summary`, `category`, `updated`. `index.md` is
built from exactly those four fields and nothing else.

## Retrieval, largest saving first

1. A previously answered question is a page, not a search. One synthesized
   page costs 500 to 1,500 tokens. Re-deriving the same answer from three
   sources costs 20,000 or more.
2. Read `wiki/index.md` before searching anything. At a few hundred pages
   the whole index costs roughly 5,000 tokens, cheaper than one wrong
   guess at what to search for.
3. Freshness comes from the index's `updated` column, not from opening the
   page.
4. `SCHEMA.md` is what makes the cheap path get taken. Read it before
   deciding what to do next.

Route by question shape: check the index, read the matching page, check
backlinks, only then search `sources/`, then file the answer back as a
page (`page` regenerates the index itself).

## No search verb

There is no search command. `rg` (or `grep`) is the query engine, and
backlinks come from the same tool, not a stored index:

```
rg -l '\[\[page-name\]\]' .kb/wiki/
```

## Install

Symlink this directory into `~/.claude/skills`.
