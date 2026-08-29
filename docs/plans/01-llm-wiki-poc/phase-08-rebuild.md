[overview](overview.md)

# Phase 8: rebuild

**Goal.** `dedup --rebuild` per `.5`: the repair for lost races and
order-dependence.

**Changes.** `llmwiki/dedup.py` (one function), `tests/test_dedup.py`
(additions). Delete every `kind: story` page, drop `story:` from every summary,
replay summaries ordered by (`fetched`, hash), one log line
`## [dedup] rebuild N summaries into M stories`. Push does not run at rebuild.

**Data structures.** None new.

**Verification.** Tests: rebuild twice yields an identical `wiki/` tree;
rebuild after a manually removed membership restores it; no push lines emitted.
Runtime: `dedup --rebuild` on the tempdir copy from phase 7, `diff -r` against a
second run.
