[overview](overview.md)

# Phase 11: fetch

**Goal.** URLs through the one door, per `.6` and `.7`.

**Changes.** `llmwiki/fetch.py`, `tests/test_fetch.py`, `ingest.py` gains
`ingest_url`. URL guard ported by hand from the old CLI (`llm_wiki.py`
L507-600: per-hop private-address check, content-type allowlist, size cap,
redirect limit, timeout). Extraction: `trafilatura` (Apache-2.0), then the
densest `<article>`/`<main>` block, `pypdf` for PDFs, else raw bytes stored as
the source. GitHub `blob` URLs rewritten to raw. Tracking params stripped. See
`docs/research/fetch-path.md`.

**Data structures.** None new; a fetched result is `(final_url, content_type,
data)`.

**Verification.** Tests against a local `http.server` thread: redirect to a
private address refused, oversize body refused, timeout is `failed`, tracking
params stripped, PDF path taken by content type. Runtime: one real
`ingest <public url>` into a tempdir kb, user-approved since it touches the
network.
