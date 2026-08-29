[overview](overview.md)

# Phase 4: sources

**Goal.** Bytes in, hash out, provenance beside: the storage half of `.6`.

**Changes.** `llmwiki/sources.py`, `tests/test_sources.py`. `store(kb, data,
url, content_type, job) -> (hash, "new" | "exists")` writes
`sources/<sha256>.<ext>` with exclusive create (`O_EXCL`; the old
`write_source_file` pattern, ported) and `sources/<hash>.toml` with `url`,
`fetched`, `content_type`, `job` (four `key = "value"` lines; no writer dep).
`read_provenance(kb, hash) -> dict` via `tomllib`. The `ingest` verb is wired
in phase 9; this phase is library only.

**Data structures.** None new; provenance is the dict `tomllib` returns.

**Verification.** Tests: same bytes twice is `exists` and writes nothing; two
processes racing the same hash both succeed with one file; extension by content
type (`text/markdown` to `.md`, `application/pdf` to `.pdf`, else `.txt`);
provenance round-trips. Runtime: in-process `store` of a fixture file into a
tempdir kb, `ls sources/` shows the pair.
