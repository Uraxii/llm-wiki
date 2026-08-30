# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Build & Test

Python 3.14, standard library only, with `sqlite-vec` the single measured
exception. See the Dependencies rule below.

**Use `.venv/bin/python`, not bare `python3`.** From phase 13 the whole CLI
requires `sqlite-vec`: `llmwiki/vectors.py` imports it at module level and
`ingest` sweeps vectors on every run, so bare `python3 -m llmwiki` raises
`ModuleNotFoundError` and so does the suite. Any `[jobs]` entry on an OS
scheduler must name the venv interpreter.

```bash
.venv/bin/python -m unittest discover tests
.venv/bin/python -m compileall -q llmwiki
```

The suite must be hermetic. Run it a second time with a dead proxy and get
the identical result; anything else means a module reached the real network:

```bash
http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
  .venv/bin/python -m unittest discover tests 2>&1 1>/dev/null | tail -4
```

Note the redirect: `unittest` reports on stderr, and `summarize` prints its
planned call count to stdout.

This repo is indexed in codebase-memory as
`var-home-nicole-Projects-agent-kb`. Query the graph to locate a symbol
before reading modules wholesale; re-index after a phase lands.

## Architecture Overview

`llmwiki` is a thin CLI over a knowledge base directory (`.kb`), built on
Karpathy's three layers and nothing else:

- `sources/` immutable raw bytes, keyed by sha256, each with a
  `<digest>.toml` provenance sidecar
- `wiki/` pages the model writes, one markdown file with a `---`
  frontmatter block each
- `SCHEMA.md` the contract the user's agent reads; the CLI never parses it

Machine settings live in `config.toml` (`[models]`, `[identifiers]`,
`[jobs]`, `[endpoint]`); the summarizer prompt lives in `SUMMARIZE.md`.

**Ownership boundary, the rule most likely to be broken.** The CLI writes
ONLY `sources/`, wiki pages of kind `summary` and `story`, `log.md` lines,
and `vectors/`. Everything else under `wiki/`, `index.md` included, belongs
to the user's agent, and the CLI must never overwrite it.

### Module map

| Module | Owns |
|---|---|
| `core.py` | `Kb` paths, `load_config`, frontmatter parse/render, `slugify`, `atomic_write_text`, `append_log_entry`. Every other module imports these and none re-implements them. |
| `cli.py` | `main(argv)`, the `VERBS` table, root resolution, `init`. One thin `cmd_*` per verb; the work lives in the module behind it. |
| `lint.py` | Six mechanical checks over `wiki/`. No warnings, no severities, no auto-fix. Also renders the identifier vocabulary into the summarizer prompt. |
| `sources.py` | Hash-keyed byte store plus provenance. The sidecar, not the byte file, is the exclusive claim that decides new from existing. |
| `model.py` | The endpoint client. `ModelError` is its entire error contract; every failure path raises it. |
| `summarize.py` | One summary page per source: build prompt, call the model, parse the reply, self-lint, keep or drop. |

Data flow: `sources.store` writes bytes and provenance, `summarize.run`
turns each digest into a wiki page, `lint_pages` gates the page before it
is kept. A page that fails its own lint is never left on disk, and a
re-run that fails never destroys the good page it was replacing.

`config.toml` is read as the plain dict `tomllib` returns and passed
around as that dict. Do not re-type it into dataclasses.

## Conventions & Patterns

**Dependencies.** The standard library, plus `sqlite-vec` and nothing else.
Convenience never justifies a package; only a property the standard library
cannot provide does, and that case is argued with a measurement, to the user,
before anything is added.

HTML extraction is a small `html.parser` densest-block reader, not
`trafilatura`, and anything it cannot read is stored as raw bytes. There is no
PDF text extraction, because the standard library has none; do not hand-roll
one. Both were dropped by user directive.

`sqlite-vec` (phase 13) is the single exception, kept for a measured reason.
A stdlib scan using `math.sumprod` over normalized `array('f')` vectors runs
about 43x slower at ten thousand pages, 911 ms against 21 ms, because sumprod
boxes every element through the iterator protocol while `sqlite-vec` runs SIMD
over the raw blob. It lives in a git-ignored `.venv` on 3.14, because PEP 668
refuses `pip install --user` on this Homebrew Python; do not reach for
`--break-system-packages`. Rebuild with `uv venv --python 3.14 .venv` and
`uv pip install --python .venv/bin/python sqlite-vec`. From phase 13 the
whole project runs under `.venv/bin/python`, not just its suite: a degrade
path was rejected because it would let `ingest` skip embedding silently and
let `search` claim the wiki has nothing on a subject it has pages for.

If any other task seems to need a package, that is a signal the task is too
ambitious for this PoC. Cut the task, do not add the package.

**Network.** Never `urllib.request.urlopen` anywhere in this project. It
reads `http_proxy` from the environment and follows redirects, and both
resend the `Authorization` header to a host `config.toml` never named. Use
`model._OPENER`. No retries, backoff, connection pooling, or response
caching.

**Credentials.** From the environment only: `LLM_WIKI_API_KEY`, else
`LLM_WIKI_API_KEY_FILE` as a path. Never in `config.toml`, never cached in
module state, never printed, never in an error message.

**Prose.** No em-dashes in text an agent writes. No vendor names and no
security-specific vocabulary in CLI modules or example configs, not even
commented out; test fixtures are exempt, and generated or fetched text is
never restyled.

**Tests.** Under `tests/` only. Copy a fixture to a tempdir; never run the
CLI against a checked-in fixture. Drive the endpoint through
`tests/fake_endpoint.py`, never the real network.

**A green suite is not evidence.** Three phases running, the delegate's
passing tests hid a real defect each time, including one that silently
destroyed a page while exiting 0. Before calling anything done, drive the
runtime path by hand with inputs the tests do not use: a missing file, a
duplicate title, a malformed reply. Prefer a real call over a fake one for
anything that talks to a model.

**Decisions.** Every non-obvious design call gets a row in
`docs/plans/01-llm-wiki-poc/decisions.tsv` (what, why, evidence, result),
and the row is corrected when later evidence contradicts it. That file is
git-ignored and stays local: keep writing rows, never commit it, never
re-add it with `git add -f`. A fixture that
disagrees with `core.parse_frontmatter` means the fixture is wrong; do not
loosen the parser to rescue it.

**Scope.** One phase per session, per `docs/plans/01-llm-wiki-poc/`. Beads
is the issue tracker only: tickets, dependency edges, the ready frontier.
Not memory, not session recovery, not rules injection.
