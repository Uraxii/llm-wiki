# Context: llm-wiki substrate

Glossary for the substrate. Domain terms live in each kb's SCHEMA.md, never here.

## Terms

- **Source**: an immutable raw file under `sources/`, addressed by content hash. Never edited, never deleted.
- **Page**: a markdown file under `wiki/` with frontmatter. Identity is the frontmatter `title`.
- **CLI page**: a page of kind `summary` or `story`, the only kinds the substrate writes.
- **Agent page**: any page that is not a CLI page. The substrate reads it, never writes it; its kinds and keys are SCHEMA.md's.
- **Identifier**: one `key:value` string in a page's `identifiers` list. The key must be a declared key; the value is what the source called the thing.
- **Declared key**: an identifier key named in the kb's `[identifiers]` table, with an optional pattern the value must match and an optional description for the summarizer.
- **Vocabulary**: the full set of declared keys for one kb. The substrate has none of its own.
- **Lint**: the substrate's mechanical pass over every page: frontmatter, identifiers, citation direction, dangling hashes. Semantic checks (contradictions, staleness) are the agent's, per SCHEMA.md.
- **Finding**: one lint failure: a page, a check, a detail. There are no warnings.
