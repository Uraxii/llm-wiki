[overview](overview.md)

# Phase 1: core

**Goal.** The shared primitives every later module imports, so no module writes
its own parser, path logic, or config loader.

**Changes.** `llmwiki/__init__.py`, `llmwiki/core.py`, `tests/test_core.py`.
Ported by hand, not imported, from the old CLI (reference only): `slugify`
(`llm_wiki.py` L284), `atomic_write_text` (L358), `append_log_entry` (L484).
New: one frontmatter parser and renderer handling scalar strings and flat
string lists (the `identifiers` and `members` shape), `None` on malformed
input, exact round-trip. `load_config(kb)` is `tomllib.load` returning the dict
as is, `{}` when the file is absent, an error naming the file when malformed.

**Data structures.** `Page(path, fields: dict[str, str | list[str]], body)`.
`Kb(root)` with `wiki`, `sources`, `vectors`, `log`, `config` (the loaded dict).

**Verification.** Tests: round-trip of `tests/fixtures/recipe/.kb/wiki/omelette-story.md`
and `tests/fixtures/security/.kb/wiki/incident-story.md`; malformed block
returns `None`; NFC and NFD spellings of one title fold to one slug; log line
`## [kind] title - <utc ts>`; config on missing and malformed files. Runtime:
none (library phase), flagged.
