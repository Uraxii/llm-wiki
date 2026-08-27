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

## Verbs

```
llm-wiki init [PATH]        create the kb tree (default ./.kb)
llm-wiki where               print which kb resolves, and how
llm-wiki add TITLE           write a source, body from stdin
llm-wiki index                regenerate wiki/index.md
llm-wiki log KIND TITLE      append one entry to log.md
llm-wiki links PAGE          print pages that link to PAGE
```

Every verb accepts `--kb PATH` to override resolution. Without it: walk up
from cwd for a `.kb` directory, else fall back to the global store
`~/.local/share/agent-kb` (honouring `XDG_DATA_HOME`).

Capture a source:

```
llm-wiki add "Widget Catalog" --origin research \
    --source "https://example.com/widgets" <<'EOF'
body text goes here
EOF
```

`--origin` is `curated` (default), `research`, or `collector`.

Write or extend a page by hand (pages are updated, not regenerated), then:

```
llm-wiki index
```

Check what links to a page:

```
llm-wiki links widget-supplier
```

`links` scans `wiki/` only; a `[[wikilink]]` written in a `sources/` file is
not reported, since backlinks are a wiki-layer concept.

Record what happened:

```
llm-wiki log ingest "captured widget catalog source"
grep "^## \[" .kb/log.md | tail -5
```

## Frontmatter contracts

Scalars only, no nested values, no lists.

Source frontmatter: `origin`, `source`, `collected_at`, and for a collector
snapshot additionally `job`, `mode` (`full` or `partial`).

Page frontmatter: `title`, `summary`, `category`, `updated`. `index.md` is
built from exactly those four fields and nothing else.

## Retrieval, ranked by token cost

1. A previously answered question is a page, not a search. One synthesized
   page costs 500 to 1,500 tokens. Re-deriving the same answer from three
   sources costs 20,000 or more.
2. Never return bodies by default. Path, title, snippet, score, then fetch
   only what got picked.
3. Chunk, so a hit is a section, not a whole document.
4. Field projection on structured data. Grepping a raw snapshot for one
   answer can drag 1.6 MB into context, roughly 400,000 tokens; a projected
   query over the same data is a few hundred tokens.
5. Read `wiki/index.md` first, so the first real query is targeted instead
   of a blind guess.
6. `SCHEMA.md` is what makes the cheap path get taken. Read it before
   deciding what to do next.

## No search verb

There is no search command. `rg` (or `grep`) is the query engine, and
backlinks come from the same tool, not a stored index:

```
rg -l '\[\[page-name\]\]' .kb/wiki/
```

## Install

Symlink this directory into `~/.claude/skills`.
