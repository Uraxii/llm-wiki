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

If that is not found, install it from a checkout of the `llm-wiki`
project (`github.com/Uraxii/llm-wiki`):

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

**Keeping the kb outside the repo root.** Some projects put every agent
scratch directory in one place. The upward search looks for a directory
named `.kb`, so a kb kept anywhere else needs a symlink at the repo
root:

```
mkdir -p .agent-scratch
llmwiki --kb .agent-scratch/.kb init
ln -s .agent-scratch/.kb .kb
```

Skip the symlink and every verb run from the repo root falls through to
the global store, with the stderr note above as the only warning. There
is no `init --path`; those three commands are the whole feature.

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

This is not decoration. The push warning below runs on identifiers and
only on identifiers. Story joining does too, unless you also set
`[models] dedup` (next), in which case a summary can join a story
through vector similarity even where no identifier matches. A kb that
declares no keys and leaves `dedup` unset gets one story per source
forever and never warns you about anything. Declare at least one key
that genuinely discriminates, and declare no key you cannot write a
real pattern for.

**3. Name a provider and the models it serves.**

```toml
[models]
summarize = "hosted:<chat model id>"
embed = "hosted:<embedding model id>"
# dedup is optional. See "How summaries join into stories" below.

[providers.hosted]
url = "https://api.example.com/v1"
key_env = "LLM_WIKI_API_KEY_HOSTED"
```

Every id under `[models]` is `"<provider>:<model>"`. The prefix names a
table under `[providers]` and is always required. `init` ships one
commented `[providers.hosted]` table and points every id at it, so
uncommenting that table and setting its `url` is the whole edit.
`hosted` is only the name the stub picked; rename it, or add more
tables, as your endpoints require.

A provider table takes four keys:

| Key | What it is |
|---|---|
| `url` | The endpoint base url. Required. |
| `key_env` | The environment variable holding this provider's API key. |
| `key_file_env` | An environment variable holding a path to read the key from. |
| `pdf_part` | How this endpoint takes a PDF: `file`, `image_url`, or `none`. Defaults to `file`. |

Set neither `key_env` nor `key_file_env` and no `Authorization` header is
sent, which is what a server on your own machine usually wants.

An older kb whose `config.toml` still has an `[endpoint]` table is
refused. `llmwiki lint` names the fault in one line; `llmwiki status`
prints the exact replacement to write, table by table.

**Check the config before you trust it.** `llmwiki lint` resolves every
id under `[models]` against `[providers]` and prints one line per fault,
so a kb that cannot make a single model call fails `lint` instead of
passing it:

```
/path/.kb/config.toml	config	[models].embed names provider 'hosted', but [providers.hosted] is not in config.toml
```

## The credential

Read from the environment and nowhere else. `config.toml` holds the NAME
of the variable, never a key value, and each provider carries its own:
`key_env` names a variable holding the key, `key_file_env` names one
holding a path to read the key out of. A local endpoint that needs no
key and a hosted one that does can therefore both be named from the same
`[models]` block.

**Nothing sets the variable for you.** A fresh shell has no key, and
every verb that calls a model then stops with

```
llmwiki: embed: LLM_WIKI_API_KEY_HOSTED is unset or empty
```

and exits 1. Fetch the key from wherever this machine keeps its secrets,
this user has a skill for it, and pass it inline to the one command
that needs it. Never write it to a file, never put it in
`config.toml`, never print it, never leave it exported in a shell other
agents share.

A shell may already do this for you: a wrapper that fetches the key per
command and passes it to that one process. If a plain `llmwiki search`
works without you handling a key, that is why, and you should not go
looking for one.

**A wrapper defined as a shell function may not reach you.** A function
lives in the shell that sourced it, so a non-login or snapshotted shell,
which is what most agents run in, can inherit the wrapper's name and
none of the helpers it calls. The symptom is a `command not found` for a
name you never typed. To get past it now, source the file that defines
the function, then rerun the verb.

The durable fix belongs to whoever owns the shell configuration, not to
this CLI. Move the wrapper out of the shell startup file and into an
executable on `PATH`:

```
#!/usr/bin/env bash
# ~/.local/bin/llmwiki-with-key, or any name earlier on PATH
exec env LLM_WIKI_API_KEY_HOSTED="$(your-secret-tool read llm-wiki)" \
  /path/to/real/llmwiki "$@"
```

Every shell inherits a file on `PATH`, login or not, snapshotted or not,
so the failure above cannot happen again. There is no `key_command`
setting in `config.toml`, and there will not be: it would make a config
file executable, which is a much worse trade than one script.

Which verbs need it: `ingest`, `summarize`, `embed`, `search`, and
`dedup` when `[models] dedup` is configured. `init`, `where`, `lint`
and `status` never call a model and work with no key at all. A remote
search or a remote `page` uses a different credential; see below.

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

## Asking other wikis

A kb can name other wikis to ask alongside its own, in its own
`config.toml`:

```toml
[remotes.otherwiki]
url = "https://wiki.example.internal"
token_env = "OTHERWIKI_TOKEN"
mode = "read"
```

`url` must be `https`, with no userinfo, query, or fragment.
`token_env` names an environment variable holding that remote's
credential, read fresh on every call; a remote with no `token_env`
sends no credential. That credential is separate from every provider's
`key_env`, which only ever talks to this kb's own model endpoints.
`mode` is advisory; `read` is the only value accepted today.

```
llmwiki search "query" --remote otherwiki
llmwiki search "query" --remote otherwiki --remote another
llmwiki search "query" --all              # every remote in [remotes]
```

`--remote NAME` repeats to name several remotes. `--all` adds every
remote in `[remotes]`. Either flag also asks the local kb, under the
label `local`, as long as the kb has a `wiki/` directory.

Results come back as one ranked list per wiki, under a `# <name>`
header, and are NEVER merged into one ranking. A similarity score is
comparable only inside one embedding model's distribution, so a score
from one wiki and a score from another were never on the same scale;
comparing them is a mistake this tool refuses to make for you. Under
`--remote` or `--all`, the printed line drops the score and shows a
rank inside that wiki's own list only:
`<rank>\t<name>\t<title>\t<updated>\t<size>`. Plain `search`, with no
remote flag, is unchanged and still prints
`<score>\t<name>\t<title>\t<updated>\t<size>`.

A wiki that fails, local or remote, prints `<name>\t<code>` to stderr
instead of a block, and the whole command exits 1, even though every
other wiki's ranking printed fine. Plain `search` keeps its own exit
code and is not affected by this.

`llmwiki page <name>` prints one local page's bytes. `llmwiki page
--remote NAME <page>` fetches that page from a remote instead, and on
failure prints `<name>\t<code>` to stderr and exits 1, the same shape
as a failed search participant.

There is no `schema` verb. It is not built.

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

## The service

`llm-wiki` also ships `llmwiki_service`, the HTTP service that answers
the `/search` and `/page` routes a `[remotes]` entry points at.
Setting one up is an operator task, covered in
`docs/plans/02-llmwiki-service/` and `llmwiki_service/admin.py`, not
here.

One fact worth knowing as a caller: an operator mints your remote
token with `POST /admin/tokens`, which returns the plaintext exactly
once, lists tokens with `GET /admin/tokens`, and revokes one with
`DELETE /admin/tokens/{label}`. The bootstrap admin credential that
mints the first token cannot be revoked at runtime; the operator's
path for that is mint a real token, unset the bootstrap variable, and
restart. If a remote token you were given stops working, that is the
kind of thing to ask the operator about.

The wheel only started shipping `llmwiki_service` recently. An older
install of this package has no service in it at all.
