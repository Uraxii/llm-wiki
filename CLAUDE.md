# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Build & Test

Python 3.14, stdlib only until a phase adds its dependency (see `docs/plans/01-llm-wiki-poc/overview.md`).

```bash
python3 -m unittest discover tests
python3 -m compileall -q llmwiki
```

**Interpreter.** Phases 1 to 10 run on bare `python3` (3.14.7). From phase 11
the suite needs `trafilatura`, `pypdf` and `sqlite-vec`, which live in `.venv`,
so run it as `.venv/bin/python -m unittest discover tests` instead. That venv
is Python 3.14.7 and git-ignored. It exists because PEP 668 refuses
`pip install --user` on this Homebrew Python; do not reach for
`--break-system-packages`, which risks the Homebrew install. Rebuild it with:

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python trafilatura pypdf sqlite-vec
```

`sqlite-vec` needs a SQLite extension load. This build allows it, verified: a
`vec0` virtual table creates and a KNN query returns. A Python built without
`enable_load_extension` would break the phase 13 store outright.

The suite must be hermetic. Run it a second time with a dead proxy and get
the identical result; anything else means a module reached the real network:

```bash
http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 \
  python3 -m unittest discover tests 2>&1 1>/dev/null | tail -4
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

**Dependencies.** Standard library first, always. A dependency arrives only
in the phase that needs it (`trafilatura` and `pypdf` in phase 11,
`sqlite-vec` in 13). Never argparse, pyyaml, numpy, or requests.

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
