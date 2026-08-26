# agent-kb

An LLM wiki: a live model of an environment plus the accumulated reasoning over it.
Built to give agents cheap, easy-to-retrieve context.

## What it is

- **Sources stay live.** Systems of record remain authoritative. Inventories are not
  mirrored wholesale.
- **Connectors are plugins.** Small Python modules that run queries on a schedule and
  emit rows in a shared schema.
- **One write path.** Validation, distillation, identifier preservation, `as_of`
  stamping, link resolution and indexing all happen in exactly one place.
- **Two halves, one namespace.** A markdown vault of named entity pages, conclusions
  and sources; a rows database with full-text search over the raw.
- **Narrow tools.** `read(name)`, `search(q)`, `query(filter, agg)`, exposed over MCP
  and kept as LLM-free as possible.
- **The agent orchestrates.** Planning, fan-out and synthesis live in the agent, not
  in the service. Whatever it learns gets written back, so the derived layer accretes.

## Status

Early design. See `docs/design/`.

## Prior art

Architecture borrows its ingest discipline from
[How Cerebras Built Its Enterprise Knowledge Base](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base),
and diverges from it by accreting agent-derived knowledge rather than recomputing
every answer from sources.
