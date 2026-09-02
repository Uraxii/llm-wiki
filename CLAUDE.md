# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

## Build & Test

Python 3.14. Dependencies are allowed; see the Dependencies rule below for the
bar they clear. In use today: `sqlite-vec` for the vector store, `starlette`
and `uvicorn` for the service, and `mutmut` for the mutation gate. Only
`sqlite-vec` is declared in `pyproject.toml`; declaring the rest belongs to
plan 02 phase 5, which owns packaging, so a fresh checkout needs
`uv pip install --python .venv/bin/python starlette uvicorn mutmut` until then.

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
`var-home-nicole-Projects-llm-wiki`. Query the graph to locate a symbol
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
`vectors/`, and the `.lock` file at the kb root. Everything else under
`wiki/`, `index.md` included, belongs to the user's agent, and the CLI must
never overwrite it.

### Module map

| Module | Owns |
|---|---|
| `core.py` | `Kb` paths, `load_config`, frontmatter parse/render, `slugify`, `atomic_write_text`, `append_log_entry`. Every other module imports these and none re-implements them. |
| `cli.py` | `main(argv)`, the `VERBS` table, root resolution, `init`. One thin `cmd_*` per verb; the work lives in the module behind it. |
| `lint.py` | Seven mechanical checks over `wiki/`. No warnings, no severities, no auto-fix. Also renders the identifier vocabulary into the summarizer prompt. |
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

**Dependencies. The stdlib-only mandate is dropped, by user directive.** It is
superseded twice over and neither earlier version binds: not "stdlib plus
`sqlite-vec` and nothing else", and not the pure-Python-only relaxation that
briefly replaced it. The CLI and the service may both take any dependency.

The rationale that held the old rule up was that the CLI is a skill tool that
installs anywhere. That stopped being true at phase 13, where `vectors.py`
began importing `sqlite-vec` at module level. The CLI already requires
`.venv/bin/python` and already ships a venv, so the marginal cost of the next
wheel is close to zero.

What survives is judgment, not a ban. A package earns its place by doing
something worth more than the reading, the pinning, and the failure modes it
adds. Prefer the standard library when it is genuinely adequate, because fewer
moving parts is still a real property. Reach for a package when it is not, and
say in one line what it bought.

**A dropped constraint is not a mandate to rewrite.** The measured constants,
the concurrency work in phase 14, and the seven lint checks are the value in this
repo, and none of them gets better by being rebuilt on a library. Swap a piece
out when the library is better at that piece, one piece at a time, each with
its own tests staying green.

HTML extraction is currently a small `html.parser` densest-block reader, and
anything it cannot read is stored as raw bytes. `trafilatura` was dropped by
user directive under the old rule; that rule is gone, so this is now the
strongest candidate in the repo for a library swap, not a settled decision.

**PDFs and images do not get parsed at all.** Phase 15 hands the raw bytes to a
vision model, which reads scans, charts, and diagrams that no text extractor
sees. Never hand-roll a PDF parser. `pypdf` is the recorded fallback if, and
only if, the endpoint probe in `docs/plans/01-llm-wiki-poc/phase-15-visual-sources.md`
shows the endpoint refuses PDF parts; reach for it then, not before.

`sqlite-vec` (phase 13) was the first dependency, kept for a measured reason.
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

`sqlite-vec` is no longer an exception to anything; it is simply the first
dependency, and its 43x measurement above is the shape of argument a new one
should come with.

**Network.** Never `urllib.request.urlopen` anywhere in this project. It
reads `http_proxy` from the environment and follows redirects, and both
resend the `Authorization` header to a host `config.toml` never named. Use
`model.OPENER`. No retries, backoff, connection pooling, or response
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

**A green suite is not evidence.** Five consecutive phases running, the
delegate's passing tests hid a real defect each time, including one that
silently destroyed a page while exiting 0. A green suite proves the tests
that exist pass, not that they would catch a real bug. Before calling
anything done, drive the runtime path by hand with inputs the tests do not
use: a missing file, a duplicate title, a malformed reply. Prefer a real
call over a fake one for anything that talks to a model. The gate below
covers whether the tests are strong enough to catch a mutated line; it does
not cover anything the tests never call, so hand-driving still applies.

**The mutation testing gate measures test strength, not just test count.**
`scripts/check-mutation-gate.py` mutates `llmwiki_service/auth.py`,
`llmwiki_service/tokens.py`, `llmwiki_service/deployment.py`,
`llmwiki_service/app.py`, `llmwiki_service/tls.py`, and
`llmwiki_service/routes.py` with `mutmut`, reruns the matching
`tests/test_service_*.py` files against each mutant, and fails when a
mutant survives that `scripts/mutation-survivor-baseline.txt` does not
already name. Run it with:

```bash
.venv/bin/python scripts/check-mutation-gate.py
```

It ratchets rather than demands zero survivors: every mutant currently on
the baseline is an equivalent mutant with a recorded reason, not a test gap,
so killing more of them means proving a new one is also equivalent, not
chasing the count down. The gate exists to stop the count growing, so a new
surviving mutant, meaning a test got weaker or a line lost its coverage,
fails the run and names the mutant.

The gate also fails when a mutant lands in mutmut's `no tests` state:
no test in `pytest_add_cli_args_test_selection` ran against it at all.
This is worse than a survivor, a survivor was tested and beat the tests,
a `no tests` mutant means the line has zero mutation coverage while the
gate stays green. This is not theoretical: `SearchLimiter` lived in
`llmwiki_service/auth.py`, which was already mutated, but its tests lived
in `tests/test_service_routes.py`, which was not in the selection, so all
42 of its mutants read `no tests` and the gate reported green while
checking nothing on that class. There is no baseline for `no tests`; the
count must always be zero. Every module added to `only_mutate` needs its
covering test file added to `pytest_add_cli_args_test_selection` in the
same change, or its mutants fall into this hole.

Regenerate the baseline only with `--update-baseline`, never by piping
`mutmut results` over the file: that flag rewrites the survivor list while
keeping the header and every entry's `# reason` comment, where the old
piped command destroyed the header on every regeneration.

```bash
.venv/bin/python scripts/check-mutation-gate.py --update-baseline
```

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
