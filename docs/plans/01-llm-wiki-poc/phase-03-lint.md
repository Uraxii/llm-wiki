[overview](overview.md)

# Phase 3: lint

**Goal.** The six checks from `.3` over every page under `wiki/`, callable
in-process by ingest and as a verb.

**Changes.** `llmwiki/lint.py`, `tests/test_lint.py` (replaces the old file;
fixtures under `tests/fixtures/{recipe,security}` reused). `Finding(path,
check, detail)`; checks `frontmatter`, `identifier-key`, `identifier-value`,
`cites-summary`, `dangling-source`, `story-member` as an ordered list of
`(name, fn)` pairs; `prompt_block(config)` for the summarizer; stdout
`path\tcheck\tdetail`, exit 1 on any, one log line
`## [lint] N findings over M pages`.

**Data structures.** `Finding` NamedTuple; the check table.

**Verification.** Tests: both fixtures produce their known finding sets (4 and
4, per the EXPECTED table in the old `tests/test_lint.py`), every check fires at
least once across the two, `lint <page>` filters to that page. Runtime: `lint`
on a tempdir copy of each fixture, exit code observed.
