# agent-kb

An LLM wiki: a live model of an environment plus the accumulated reasoning over it.
Built to give agents cheap, easy-to-retrieve context.

## What it is

Three layers, no service, no database, no network surface.

- **`sources/`.** Immutable. Articles, papers, snapshots. Written once, never
  edited, never deleted.
- **`wiki/`.** The model owns it. Pages it writes and keeps updating as new
  sources land, linked with `[[wikilink]]` syntax. `wiki/index.md` is generated.
- **`SCHEMA.md`.** The configuration that matters: conventions, workflows, and
  which retrieval path to take for which question.

No search verb. Agents grep. See `docs/design/llm-wiki.md` for the full design
and `skills/llm-wiki/SKILL.md` for how an agent uses it.

## CLI

`./llm-wiki` is a single stdlib-only Python script. Verbs: `init`, `where`,
`add`, `index`, `log`, `links`.

## Status

Early design. See `docs/design/`.

## Prior art

Architecture borrows its ingest discipline from public write-ups of
enterprise knowledge base builds, and diverges from them by accreting
agent-derived knowledge rather than recomputing every answer from sources.
