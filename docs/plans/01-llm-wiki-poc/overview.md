# Plan 01: llm-wiki PoC

## Context

Map `agent-kb-0zf` has every substrate decision locked except the embedding
default (`.21`, paid arena approved for the next session). The user's directive:
"get me to a poc", "stick as close to karpathy's model to keep it simple", and
every PoC written before this plan (`llm_wiki.py`, `kb_lint.py`) is ignored as a
base. This plan builds the substrate thin, from the decisions, in phases small
enough for one session each, and reaches acceptance rung 1 before any network
fetch or vector code exists, because rung 1 (`.13`) needs "no feeds, no
identifiers, no vectors".

Reviewed once (reviewer agent, 2026-08-29); all 14 edits applied.

## Scope

In: the CLI that owns `sources/`, summary and story pages, `log.md`, and the
vector cache; verbs `init`, `where`, `ingest`, `summarize`, `dedup`, `lint`,
`embed`, `status`, `search`. Acceptance rung 1 as the end-to-end proof.

Out: any agent page or `index.md` (the agent's per `.2`), the `page`, `index`,
`links`, `log` verbs of the old CLI (deleted per `.9`; the design doc section
"The page verb" is superseded by `.2`), a server or daemon, media
transcription, JS-rendered pages, the old `~/Projects/knowledgebase` code,
rungs 2 to 8.

## Constraints

- Python 3.14 stdlib plus three deps, each arriving only in its phase:
  `trafilatura` and `pypdf` (`.7`, phase 11), `sqlite-vec` (`.1`, phase 13).
- No argparse (one `--kb` first-arg convention plus positional verbs), no pyyaml,
  no numpy, no env vars for model config (`.8`); credentials ONLY from the system
  environment, `LLM_WIKI_API_KEY` or `LLM_WIKI_API_KEY_FILE` (user directive,
  design doc "Credentials"), no locks, no fourth layer, no server, no vendor
  names in modules or docs.
- Every settled decision in `docs/design/llm-wiki.md` and the ticket
  resolutions binds; a phase that needs a substrate change reopens the ticket.
- Tests under `tests/` only, `python3 -m unittest discover tests`. Fixtures are
  copied to a tempdir before any run; never run the CLI on checked-in fixtures.
  Tests needing a model point `[endpoint] url` at a local fake endpoint thread
  (phase 5); no stub flags in shipped code.
- Data shapes: `tomllib` dicts are used as returned; no re-typing of config
  sub-tables or HTTP payloads. Named types only where a tested invariant lives:
  `Page`, `Kb`, `Finding`, `Story`.
- Prose: no em-dashes. Code: comments only for a why.

## Alternatives

| Shape | Verdict |
|---|---|
| Fix `llm_wiki.py` in place | Rejected by the `.9` gate (10 defects, 3 of 7 verbs survive) and by the user's "ignore" directive. |
| One module | Lint, dedup, and vectors each carry their own tests and fixtures; every phase would touch one file. |
| Flat modules, roughly one per decision, one thin `cli.py` | **Chosen.** Each phase lands one module plus its tests. Direct imports, no package machinery beyond `__init__.py`. |

## Modules

```
llmwiki/
  core.py        Kb paths, config.toml loader, frontmatter parse+render (lists),
                 slugify, atomic write, log line
  lint.py        six checks, Finding, prompt_block
  sources.py     hash keying, bytes + provenance
  model.py       one HTTP client: chat and embeddings, credentials per design doc
  summarize.py   SUMMARIZE.md + prompt_block + source, summary page, self-lint
  dedup.py       join, candidates, judgment, story page, push warning, rebuild
  ingest.py      the serial pipeline per source, embed sweep, ingest log line
  cli.py         verb table, --kb resolution, exit codes
  fetch.py       URL guard, extraction, PDF            (phase 11)
  feeds.py       RSS, Atom, JSON Feed, jobs, modes     (phase 12)
  vectors.py     sqlite-vec file per model, embed/status/search (phase 13)
tests/           one test file per module; fixtures under tests/fixtures/
```

## Applicable skills

`ponytail:ponytail` on every phase; `principle-code-quality`;
`principle-foundational-thinking` on phases 1 and 2; `principle-prove-it-works`
(run the verb on a tempdir kb, not only the tests); `tdd` where a phase names a
cheap local target; `reviewer` agent before closing phases 5, 7, 9, 10;
`technical-writing` for `init`'s generated files.

## Phases

Critical path to rung 1:

1. [core](phase-01-core.md)
2. [init](phase-02-init.md)
3. [lint](phase-03-lint.md)
4. [sources](phase-04-sources.md)
5. [model client](phase-05-model.md)
6. [summarize](phase-06-summarize.md)
7. [dedup and push](phase-07-dedup.md)
8. [rebuild](phase-08-rebuild.md)
9. [ingest pipeline and cli](phase-09-pipeline.md)
10. [acceptance rung 1](phase-10-rung-1.md)

After rung 1:

11. [fetch](phase-11-fetch.md)
12. [feeds and jobs](phase-12-feeds.md)
13. [vectors](phase-13-vectors.md) (needs `.21` for the shipped default; builds against any model name)

Phases 3, 4, 5 are independent after 2 and may run in parallel sessions on
disjoint files. `cli.py` is provisional scaffolding in phase 2 and takes its
final verb table in phase 9.

## Verification

- Static: `python3 -m unittest discover tests`; `python3 -m compileall llmwiki`.
- Runtime: every phase ends with its verb run on a tempdir kb from the phase
  file's command, output pasted into the ticket's closing comment.
- Paid runs (phases 5, 6, 10, 13): print the planned call count first (`.8`),
  and get the user's approval per session before the first call.

## Implementation guidance

Per session: one phase, tracked as a beads issue under a new build epic (the map
is closed to execution; see the handoff). `bd update <id> --claim`, read the
phase file and the ticket resolutions it names, `how` over the modules it
imports, `ponytail` before writing, `unslop` over the diff,
`principle-prove-it-works` before closing, `show-me-your-work` TSV at
`docs/plans/01-llm-wiki-poc/decisions.tsv` for any deviation. Commit only on the
user's word (conservative profile).
