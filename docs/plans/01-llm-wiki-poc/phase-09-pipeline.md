[overview](overview.md)

# Phase 9: ingest pipeline and cli

**Goal.** Serial per source: store, summarize, embed (when a vector module
exists; a no-op until phase 13), dedup, push; every verb wired in one table.

**Changes.** `llmwiki/ingest.py` (owns the pipeline: `ingest_path`,
`ingest_stdin`, the per-source loop, the embed sweep hook at start, the
`## [ingest] <job> N new, M exists, K failed` log line; URLs and jobs arrive in
phases 11 and 12), `llmwiki/cli.py` (final verb table: `init`, `where`,
`ingest`, `summarize`, `dedup`, `lint`; `--kb` first arg else walk up for
`.kb`; each verb returns an exit code), `tests/test_cli.py`. Ingest continues
past any per-source failure, the log names it, the next run retries (a missing
summary or a story-less summary is detectable from the tree).

**Data structures.** The verb table.

**Verification.** Tests: subprocess smoke of every verb on a tempdir kb whose
`[endpoint] url` is the fake endpoint; a failing summarize leaves the source
and the loop continues; stdout line shape `hash\tnew|exists|failed\turl`.
Runtime: full `ingest` of two fixture sources, then `lint` clean. Reviewer gate
before close (ownership rule: the CLI wrote only what `.2` allows).
