[overview](overview.md)

# Phase 13: vectors

**Goal.** The `.1` store: one sqlite-vec file per embedding model, verbs
`embed`, `status`, `search`; vector candidates for dedup.

**Changes.** `llmwiki/vectors.py`, `tests/test_vectors.py`, `cli.py` gains the
three verbs, `ingest.py`'s embed sweep becomes real, `dedup.candidates` gains
the top-5 nearest summaries' stories. File `.kb/vectors/<model-slug>.sqlite`
with a pages table (`path` PK, `file_hash`, `kind`, `title`) and a `vec0`
table; staleness by a sha256 over kind, title and the embedded text; rows deleted for pages that no longer
exist; embedded text = title + identifiers + summary or body head; dimension
stored on first embed, mismatch refuses. `status` prints pages without a
current vector and sources without a summary. `search "<q>" [-n N] [--kind K]`
refuses when any page lacks a current vector, warns on unsummarized sources,
prints `score\tpath\ttitle\tupdated\tbytes`. Default model name blank until
`.21` closes; `embed` refuses naming the key.

**Data structures.** None new beyond the two tables.

**Verification.** Tests with the fake endpoint returning fixed vectors: stale
detection, deletion of vanished pages, both refusals, `status` output, top-k
order, `--kind` filter, dedup picks a vector candidate with no shared
identifier. Runtime: `embed`, `status`, `search` on a tempdir kb against the
fake endpoint, then one real run with approval.
