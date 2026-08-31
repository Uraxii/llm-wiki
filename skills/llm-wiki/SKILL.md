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

If that is not found, install it from a checkout of the `agent-kb`
project (`github.com/Uraxii/agent-kb`):

```
uv tool install .
```

Needs Python 3.14 and pulls `sqlite-vec`, which is not optional: the
vector store is loaded at import time and there is no degraded mode.

## Which kb you are talking to

In order:

1. `llmwiki --kb PATH <verb>`, used exactly as given.
2. Otherwise the nearest `.kb` directory, searching the working
   directory and then each parent.
3. Otherwise the global store, `~/.local/share/agent-kb`.

`llmwiki where` prints the one that resolved. Run it first when you are
unsure, and before any verb that writes.

**Which one to reach for.** Knowledge about one project lives in that
project's `.kb`, at the repo root, one per project, the way `.beads/`
does. Knowledge that is not tied to a repo goes to the global store.
If you are in a repo, the knowledge belongs to it, and there is no
`.kb` yet, create it once from the repo root:

```
llmwiki init
```

`init` writes `.kb/.gitignore` excluding `vectors/`, which is the
rebuildable part, so the rest of the kb can be committed with the
project. Sources and pages are worth keeping in history; the vector
store is not.

## Starting a kb

```
llmwiki init                 # creates ./.kb
```

`init` leaves a kb that runs but does not yet think. Three things must
follow before the first `ingest`, or the pages you get back will be
poor and the lint will reject some of them outright.

**1. Write `SUMMARIZE.md`.** `init` leaves a stub of a few lines. This file
is the summarizer prompt, and it decides what a page in this kb even
is. Say what one source represents here, name every frontmatter field
you want on a summary page, and say exactly what to do about
identifiers. Terse prompts produce pages the lint drops.

**2. Declare an identifier vocabulary in `config.toml`.** Each key is a
kind of stable external handle a page can carry, written into a page's
`identifiers` field as `key:value`.

```toml
[identifiers.serial]
pattern = "^[A-Z]{2}\\d{6}$"
describe = "equipment serial, two letters then six digits"
```

This is not decoration. Two mechanisms run on identifiers and only on
identifiers: joining a new summary into an existing story, and warning
you when a new source touches a page you wrote. A kb that declares no
keys gets one story per source forever and never warns you about
anything. Declare at least one key that genuinely discriminates, and
declare no key you cannot write a real pattern for.

**3. Point at an endpoint and name the models.**

```toml
[models]
summarize = "<chat model id>"
embed = "<embedding model id>"
# Leave `dedup` unset. See Sharp edges.

[endpoint]
url = "https://api.example.com/v1"
```

## The credential

Read from the environment and nowhere else: `LLM_WIKI_API_KEY` as a
value, or `LLM_WIKI_API_KEY_FILE` as a path to one.

**Nothing sets it for you.** A fresh shell has no key, and every verb
that calls a model then stops with

```
llmwiki: search: no API key: set LLM_WIKI_API_KEY or LLM_WIKI_API_KEY_FILE
```

and exit 2. Fetch the key from wherever this machine keeps its secrets,
this user has a skill for it, and pass it inline to the one command
that needs it. Never write it to a file, never put it in
`config.toml`, never print it, never leave it exported in a shell other
agents share.

Which verbs need it: `ingest`, `summarize`, `embed`, `search`, and
`dedup` only when a judge model is configured, which you should not do.
`init`, `where`, `lint` and `status` never call a model and work with
no key at all.

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
llmwiki lint            # six mechanical checks, one line per finding
llmwiki embed           # so search can find what you just wrote
llmwiki status          # pages without a vector, sources without a summary
```

`lint` has no severities, no warnings, and no auto-fix. A finding is a
thing to go fix.

## What the CLI will never do

It will not touch `index.md`, `SCHEMA.md`, or any page you wrote. It
will not delete a source. It will not overwrite a good page with a bad
one: a generated page that fails its own lint is dropped, and the page
it would have replaced survives.

## Sharp edges

Verified, and open on the tracker at the time of writing.

- **Declare an identifier vocabulary and leave `[models] dedup` unset.**
  Those are the settings under which sources join into one story. With
  no vocabulary, nothing ever joins. With a judge configured, nothing
  ever joins either, because the judge is prompted with an
  incident-tracker definition of sameness, "one real-world occurrence",
  and two sources merely about the same subject are correctly NONE
  under it. Measured on one kb, three sources, `dish` declared: judge
  configured gave three singleton stories despite two pages carrying an
  identical `dish:creme brulee`; the same kb with the judge removed and
  `dedup --rebuild` joined those two into one story, no model call.
  `agent-kb-zn6`, `agent-kb-d1w`.
- **One unreadable source makes a bare `llmwiki summarize` exit 1 for
  good**, with the reason only in `log.md`. Read the log before you
  believe the kb is broken, and pass explicit digests to work around
  it. `agent-kb-74p`.
- **A missing `[models] embed` is reported as missing vectors** by
  `status` and `search`. Check `config.toml` before you go looking for
  a data problem. `agent-kb-5ty`.
- **`ingest` embeds each summary twice.** Wasted calls, no wrong
  output. `agent-kb-2ya`.
