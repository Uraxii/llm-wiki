[overview](overview.md)

# Phase 7: dedup and push

**Goal.** Story membership per `.5` and the push warning per `.22`, without
rebuild (phase 8) and without vector candidates (the seam is a list argument;
phase 13 fills it).

**Changes.** `llmwiki/dedup.py`, `tests/test_dedup.py`. `normalise`,
`candidates(kb, page, extra=())` (identifier join, sorted by shared count desc
then `first_seen` asc), `judge` via `model.chat` with the first-line contract
(candidate slug or `NONE`, garbage reads as `NONE` and is logged) and the
deterministic first-candidate fallback when no dedup model is configured; story
write with the regenerated body, `story:` written back, self-lint before keep;
`push(kb, page)` joining against every non-CLI page, stdout
`push\t<page>\t<shared>` and one log line.

**Data structures.** `Story(path, fields, members: list[str])`.

**Verification.** Tests (fake endpoint for the judge): join across casing and
whitespace joins; no shared identifier gives a new story; no identifiers gives a
singleton; `NONE` and garbage give a new story; slug collision suffix;
lint-failing story not kept and the summary unchanged; push warns on an agent
page and never on a story page. Runtime: `dedup` on a tempdir copy of the
security fixture. Reviewer gate before close (invariant: every summary in
exactly one story).
