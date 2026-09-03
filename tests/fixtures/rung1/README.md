# Rung 1: the recipe box

Acceptance rung 1 (`agent-kb-0zf.13`), built in phase 10
(`agent-kb-3uz.10`). Twelve hand-added recipes, no feeds, no vectors, no
identifier vocabulary, through the real CLI end to end.

This is not a unit-test fixture. Nothing under `tests/` imports it and
`unittest discover` does not collect `run_rung1.py`. It is the PoC bar,
kept re-runnable so a later chain can check the substrate still clears it.

## Layout

| path | what it is |
|---|---|
| `inbox/` | the thirteen input files a person would hand the CLI: twelve recipes as `.md` and `.txt`, plus one photo of a recipe card with no extractable text |
| `kb/config.toml` | the machine settings, with an empty `[identifiers]` table. No provider host and no credential: the harness appends `[providers.hosted]` (its `url` from the environment, its `key_env` naming `LLM_WIKI_API_KEY_HOSTED`) at run time |
| `kb/SCHEMA.md` | the contract the user's agent reads. The CLI never parses it |
| `kb/SUMMARIZE.md` | the per-kb summarizer prompt, appended to the CLI's built-in skeleton |
| `kb/config.vocab-appendix.toml`, `kb/SUMMARIZE.vocab-appendix.md` | appended to the two files above only in the `--vocab` pass |
| `run_rung1.py` | the harness |

## Running it

```
export PROTON_PASS_SESSION_DIR="/tmp/pass-agent-$USER"
LLM_WIKI_API_KEY_HOSTED="$(...)" \
LLM_WIKI_ENDPOINT_URL=https://openrouter.ai/api/v1 \
  python3 tests/fixtures/rung1/run_rung1.py [--vocab]
```

It refuses to start without both variables, makes a fresh temp kb it never
deletes, prints raw stdout, stderr and exit code for every CLI call, and
exits 0 only when every assertion passed. **Read the raw output, not the
PASS column.**

Twelve paid model calls for the base pass, twenty four with `--vocab`. The
photo prints a planned line but fails on the UTF-8 decode before any call.

## The two passes, and why there are two

The rung as written asks for two things that cannot both hold. It says no
identifier vocabulary, and it asks which two sources describe the same
dish, answered by a story page. `dedup.candidates` joins on shared
identifiers and on nothing else, so with no key declared every summary
gets an empty candidate list and its own singleton story.

- **Base pass** is the rung exactly as specified. Twelve singleton
  stories, and question three has no story page to answer it.
- **`--vocab` pass** appends one discriminating key, `dish`, resummarizes
  and rebuilds. One story with two members (the two Crème Brûlée
  sources), every other story a singleton, lint clean.

Filed as `agent-kb-zn6` against the dedup design. Do not "improve" the
base fixture by adding an ingredient vocabulary: `judge` with no
`[models] dedup` configured takes the first candidate unconditionally, so
every recipe sharing salt would land in one story.

## Edge cases the fixture carries on purpose

- `03-creme-brulee-nfc.md` and `04-creme-brulee-nfd.txt` are two different
  recipes whose titles are the same string in NFC and NFD form. Both
  slugify to `cre-me-bru-le-e`, so the second page takes the twelve-hex
  collision suffix. They are also the same dish, which is what the
  `--vocab` pass files into one story.
- Re-ingesting a stored file reports `exists` and runs no pipeline, so no
  paid call.
- `13-recipe-card-scan.png` fails, and its bytes and provenance sidecar
  are kept. A later bare `summarize` retries it and exits 1 every time
  (`agent-kb-74p`).

## The model matters more than expected

`config.toml` pins `google/gemini-2.5-flash`, not the project default.
The default corrupted `cook_time_minutes` on ten of twelve pages while
every other field on the same reply was clean, and `lint` called the kb
clean anyway. See `agent-kb-g4x` and `agent-kb-tgd`.
