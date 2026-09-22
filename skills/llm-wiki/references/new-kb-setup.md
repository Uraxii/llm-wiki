# Setting up a new kb

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

This is not decoration. The push warning `ingest` prints runs on
identifiers and only on identifiers. Story joining does too, unless you
also set `[models] dedup` (next), in which case a summary can join a
story through vector similarity even where no identifier matches. A kb that
declares no keys and leaves `dedup` unset gets one story per source
forever and never warns you about anything. Declare at least one key
that genuinely discriminates, and declare no key you cannot write a
real pattern for.

**3. Name a provider and the models it serves.**

```toml
[models]
summarize = "hosted:<chat model id>"
embed = "hosted:<embedding model id>"
# dedup is optional. See "How summaries join into stories" in SKILL.md.

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

## Keeping the kb outside the repo root

Some projects put every agent scratch directory in one place. The
upward search looks for a directory named `.kb`, so a kb kept anywhere
else needs a symlink at the repo root:

```
mkdir -p .agent-scratch
llmwiki --kb .agent-scratch/.kb init
ln -s .agent-scratch/.kb .kb
```

Skip the symlink and every verb run from the repo root falls through to
the global store. The only warning is the one line on stderr that each
verb prints when it resolves past a repository this way. There is no
`init --path`; those three commands are the whole feature.
