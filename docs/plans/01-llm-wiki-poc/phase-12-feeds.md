[overview](overview.md)

# Phase 12: feeds and jobs

**Goal.** `ingest --job <name>` per `.6`.

**Changes.** `llmwiki/feeds.py`, `tests/test_feeds.py`, `ingest.py` gains the
job loop. RSS and Atom via `xml.etree`, JSON Feed via `json`; `[jobs.<name>]`
with `feed` or `urls` and `mode`: `partial` skips URLs that already have a
provenance file, `full` fetches all and lets hash keying skip unchanged bytes.
No scheduler: cron or a systemd timer calls the verb.

**Data structures.** None new; a feed item is `(url, title)`.

**Verification.** Tests: each feed format yields items from fixture files;
`partial` skips seen URLs; `full` refetches and reports `exists`; an unknown
job name is an error naming the config key. Runtime: one job against the local
`http.server` fixture feed.
