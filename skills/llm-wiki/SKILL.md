---
name: llm-wiki
description: Use at the moments knowledge is about to be lost or re-derived: before citing a source you are not keeping, when a research or investigation finding needs to outlive the session, when starting a question you suspect you answered before, and when an answer should come from accumulated notes rather than a fresh search. Also use when maintaining that knowledgebase: adding a page, checking pages, or re-embedding after edits. Covers a project-local `.kb` or the global store driven through the `llmwiki` CLI: immutable source capture with provenance, model-written summary and story pages, agent-written pages, meaning-based search, and the mechanical lint.
---

# llm-wiki

A live model of an environment plus the accumulated reasoning over it,
stored as plain files. Three layers, no service, no daemon, no server.

| Layer | Who owns it |
|---|---|
| `sources/` | The CLI. Immutable raw bytes keyed by sha256, each with a `<digest>.toml` provenance sidecar. Written once, never edited, never deleted. |
| `wiki/` | Shared. The CLI writes pages of kind `summary` and `story`. Every other page, `index.md` included, is yours. |
| `SCHEMA.md` | You. The conventions and retrieval paths for this kb. The CLI never parses it. |

## Install

Check first, because it is usually already there:

```
llmwiki --help
```

If that is not found, install it: `references/install.md`.

## Which kb you are talking to

In order:

1. `llmwiki --kb PATH <verb>`, used exactly as given.
2. Otherwise the nearest `.kb` directory, searching the working
   directory and then each parent.
3. Otherwise the global store, `~/.local/share/llm-wiki`.

`llmwiki where` prints the one that resolved. Run it first when you are
unsure, and before any verb that writes. When resolution falls all the
way through to the global store from inside a repository, every verb
prints one stderr line naming that repository, because the alternative
is project knowledge landing in the global store with nothing said.

`llmwiki --version` prints the version and the directory the package
ran from. Use it when a change to a checkout does not show up at the
command line: an installed copy on `PATH` does not track a checkout, and
the path is the half of that line that tells them apart.

**Which one to reach for.** Knowledge about one project lives in that
project's `.kb`, at the repo root, one per project, the way `.beads/`
does. Knowledge that is not tied to a repo goes to the global store.
If you are in a repo, the knowledge belongs to it, and there is no
`.kb` yet, create it once from the repo root:

```
llmwiki init
```

`init` writes `.kb/.gitignore` containing `*`, so the whole kb stays out
of the project's history. A kb is local working knowledge that grows on
its own clock, not project source.

A fresh kb runs but does not think until `SUMMARIZE.md`, an identifier
vocabulary, and a provider are in place. Do that once, before the first
`ingest`, from `references/new-kb-setup.md`, which also covers keeping
the kb somewhere other than the repo root.

## The credential

Every verb that calls a model reads the key from the environment:
`ingest`, `summarize`, `embed`, `search`, and `dedup` when `[models]
dedup` is configured. `init`, `where`, `lint` and `status` never call a
model and work with no key at all. Set one up, or work out why a verb
says the variable is unset, from `references/credential-setup.md`. A
remote wiki takes its own token instead, covered in
`references/remote-wikis.md`.

## Keeping something

```
llmwiki ingest https://example.com/page
llmwiki ingest ./notes.md ./report.txt
llmwiki ingest -                          # bytes on stdin
llmwiki ingest --job nightly              # a job declared in config.toml
```

One `ingest` stores the raw bytes, writes provenance, summarizes each
new source into a `summary` page, places that summary into a `story`,
and embeds what it wrote. It prints one line per source: the digest,
`new` or `existing`, and the argument. A source already stored by
content hash is not fetched or summarized again.

If a page you wrote shares an identifier with the new source, `ingest`
prints a `push` line naming your page. That is the signal to go update
it.

## How summaries join into stories

Every new summary joins an existing story or starts one. `dedup
--rebuild` redoes this for every summary from scratch, useful after
you edit `config.toml`.

With `[models] dedup` unset, a summary joins the story it shares the
most identifiers with, decided without a model call. Two summaries
with no identifier in common never join, however alike their subjects.

With `[models] dedup` set, `dedup` also calls that model to judge
subject identity, at a fixed temperature so the same candidate gets
the same answer on a replay. Candidates then come from two places:
shared identifiers, and the nearest existing stories by vector
similarity, so two summaries can join with no identifier vocabulary
declared at all. Measured on the recipe fixture: joins driven by an
identifier vocabulary ran 6 of 6, joins driven by vector similarity
alone ran 4 of 4, and an unrelated subject stayed in its own story 10
of 10.

A poorly worded summary can still lose a join it should have made.
That was already true; the fixed temperature just makes it fail the
same way every time instead of only sometimes.

## Asking the wiki something

```
llmwiki search "how do we rotate the gateway credential"
llmwiki search "burnt sugar dessert" -n 5 --kind story
```

Ranked by meaning, not keywords, one line each: score, file name,
title, last modified, size. Scores are cosine similarity and are only
comparable within one kb. As a calibration from a real corpus, an
on-topic hit ran 0.83, a correct hit that shared no vocabulary with the
query ran 0.63, and a query the kb had nothing on topped out at 0.09.

`search` embeds your query, so it needs `[models] embed` and the
credential. It refuses to answer while any page lacks a current vector,
which is what `embed` is for.

`--remote NAME` and `--all` ask other wikis too. Each wiki gets its own
ranked block, and the rankings are never merged. One wiki failing exits
the command 1 with every other wiki's block printed intact.

## Asking other wikis

A kb can name other wikis to ask alongside its own. Read
`references/remote-wikis.md` before pointing a kb at another wiki, or
when a `--remote` search returns something you did not expect.

## Writing your own pages

There is no verb for this. You write the file. A page is markdown with
a `---` frontmatter block:

```markdown
---
title: Gateway credential rotation
kind: runbook
identifiers:
  - serial:AB123456
---

Body.
```

Use any `kind` except `summary` and `story`, which belong to the CLI.
Give the page real identifiers if you want `ingest` to tell you when a
new source touches it. Then:

```
llmwiki lint            # config plus seven page checks, one per line
llmwiki embed           # so search can find what you just wrote
llmwiki status          # pages without a vector, sources without a summary
```

`lint` has no severities, no warnings, and no auto-fix. A finding is a
thing to go fix. A finding whose check column reads `config` points at
`config.toml`, not at a page, and means no model-calling verb will run
at all until you fix it.

## What the CLI will never do

It will not touch `index.md`, `SCHEMA.md`, or any page you wrote. It
will not delete a source. It will not overwrite a good page with a bad
one: a generated page that fails its own lint is dropped, and the page
it would have replaced survives.

There is no `schema` verb. It is not built.
