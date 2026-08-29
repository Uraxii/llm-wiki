[overview](overview.md)

# Phase 6: summarize

**Goal.** One summary page per source, per `.4` and `.3`.

**Changes.** `llmwiki/summarize.py`, `tests/test_summarize.py`. Prompt = whole
`SUMMARIZE.md` + `lint.prompt_block(config)` + source text. Frontmatter: `kind:
summary`, `title`, `source`, `source_url` and `fetched` (from provenance),
`model`, `prompt_fingerprint` (sha256 of the prompt minus the source),
`identifiers`. Body = abstract only (`.4` supersedes `.2`'s "picking-surface
fields in the body"). A reply wrapped in one outer code fence is unwrapped
before parsing (the `.4` addendum, decided here: strip one fence, reject
anything else via the `frontmatter` finding). Self-lint before keep; a failing
page is not written and the log names it. `summarize [<hash>...]` re-runs on
missing or fingerprint-changed pages.

**Data structures.** The reply parsed with `core`'s frontmatter parser.

**Verification.** Tests with the fake endpoint: page written, fingerprint
stable across runs, fenced reply unwrapped, undeclared identifier drops the page
and leaves the source, re-run skips up-to-date pages. Runtime: one real
summarize on a fixture source with approval.
