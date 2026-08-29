# SCHEMA.md skeleton

The contract between the substrate (the CLI) and the user's agent. Copy this
file to `.kb/SCHEMA.md` and fill every section with domain content. The
substrate reads only the sections marked **read by the CLI**; everything else
is the agent's operating manual. Nothing here repeats what lives in
`config.toml` or `SUMMARIZE.md`.

---

## Purpose

<one paragraph: what this kb is for, who asks it questions, what a good answer
looks like>

## Page kinds

The substrate writes two kinds and never touches any other:

- `summary`: one per source, written by the summarizer. Never edit.
- `story`: one per event, written by dedup from its members. Never edit.

Agent kinds (declare each; the agent writes and maintains them):

| kind | purpose | required frontmatter | cites |
|---|---|---|---|
| `<kind>` | <what it holds> | `title`, `kind`, `last_verified`, `identifiers` (optional) | `sources/` hashes only |

Rules the lint enforces on every page, agent pages included: frontmatter
parses; every identifier key is declared in `config.toml`; values match their
pattern; no agent page wikilinks a `summary` page (cite the source hash instead).

## Vocabulary

Declared in `.kb/config.toml` under `[identifiers.<key>]`. Do not restate it
here. <one line per key on what the agent uses it for, if not obvious>

## Summarizer

Prompt lives in `.kb/SUMMARIZE.md`; the CLI appends the declared vocabulary.
<what the agent expects to find in a summary's frontmatter, and which fields it
promotes into agent pages>

## Retrieval routing  (read by the CLI: the `search` verb reads nothing here; this orders the agent's own steps)

Cheapest path first:

1. `wiki/index.md` and page titles (grep): <which questions stop here>
2. Story pages: <which questions are answered by one story>
3. `llm-wiki search "<query>"` (vectors, top-k pages): <when to use, k>
4. Summary bodies, then `sources/` raw: <when the answer needs the raw text>

## Substrate verbs the agent calls

- `llm-wiki ingest <url|path|->`: bring a source in mid-answer.
- `llm-wiki embed <page>`: after writing or editing any agent page (required; search will not see the page otherwise).
- `llm-wiki lint [<page>...]`: before finishing a maintain pass.
- `llm-wiki search "<query>"`: step 3 above.

## Workflows

### Answer
<steps: route, read, cite `sources/` hashes, write nothing unless the answer is worth a page>

### Extend
<steps: when a summary or story arrives, which agent pages gain a line; how `index.md` is updated>

### Synthesize
<steps: when several stories warrant a new agent page; required frontmatter; cite sources, never summaries; then `embed`>

### Maintain
<steps: react to `push` warnings from ingest, refresh `last_verified`, resolve contradictions, run `lint`>

## Conventions

- Citations: `[[<sha256>]]` or a `sources/` relative link, never a summary page.
- `last_verified`: <interval vs state claims and how each decays>
- `index.md`: <who updates it and when; the CLI never writes it>
