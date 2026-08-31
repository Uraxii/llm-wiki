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

Retrieval is by meaning: pages are embedded and `search` ranks them by cosine
similarity. Grep still works and is still the right tool for an exact string.
See `docs/design/llm-wiki.md` for the full design and `skills/llm-wiki/SKILL.md`
for how an agent uses it.

## CLI

Install: `uv tool install .` from the repo root. Needs Python 3.14 and
`sqlite-vec`, the project's one dependency.

`llmwiki` verbs: `init`, `where`, `ingest`, `summarize`, `dedup`, `lint`,
`embed`, `status`, `search`.

Sources, summary pages, story pages, `log.md` and the vector store are the
CLI's. Everything else under `wiki/`, `index.md` included, belongs to the
agent, and the CLI never overwrites it.

## Install as a plugin

The plugin ships one skill, `llm-wiki`. It does not ship the `llmwiki` binary,
because no plugin format for these harnesses can declare or install an external
program. Install the plugin, then install the CLI as described under CLI above.

Claude Code:

```bash
claude plugin marketplace add Uraxii/agent-kb
claude plugin install llm-wiki@agent-kb
```

Codex:

```bash
codex plugin marketplace add Uraxii/agent-kb
codex plugin add llm-wiki@agent-kb
```

Copilot CLI:

```bash
copilot plugin install Uraxii/agent-kb
```

To load the skill from a local clone without installing anything, pass the
clone to the harness for one session:

```bash
claude --plugin-dir /path/to/agent-kb
copilot --plugin-dir /path/to/agent-kb
```

`scripts/check-skill-sync.sh` fails if the manifest versions drift from
`pyproject.toml`, or if this repo's copy of the skill drifts from the copy in
a `dotai` checkout.

## Status

The pipeline runs end to end against a real endpoint: ingest, summarize,
embed, search. Joining several sources into one story needs an identifier
vocabulary declared in `config.toml`; without one, every story is a
singleton. See `docs/design/`.

## Prior art

Architecture borrows its ingest discipline from public write-ups of
enterprise knowledge base builds, and diverges from them by accreting
agent-derived knowledge rather than recomputing every answer from sources.
