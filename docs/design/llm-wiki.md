# llm-wiki

Design record. 2026-08-26.

Supersedes `docs/design/llm-wiki-collector.md` and the architecture note of the
same date. Replaces the design in agent-kb PR #1, which is closed.

A live model of an environment plus the accumulated reasoning over it, stored as
plain files an agent can read without asking permission. Three layers, no
service, no database of record, no network surface.

---

## The shape

Three layers, after Karpathy's llm-wiki. The first is immutable and the model
only reads it. The second the model owns outright. The third tells it how to
behave in the first two.

| Layer | Rule |
|---|---|
| `sources/` | **Immutable.** Articles, papers, snapshots, images. Written once, never edited, never deleted. The source of truth. |
| `wiki/` | **The model owns it.** Pages it writes and keeps updating as new sources land. You read this layer; it writes it. |
| `SCHEMA.md` | **The configuration that matters.** Conventions, workflows, and which retrieval path to take for which question. Co-evolved by hand. |

A kb is a directory, discovered the way a harness finds its own config: walk up
from the working directory looking for `.kb`, fall back to the user's global
store. No daemon, no port, no container, and therefore no authentication problem
to solve.

```
<project>/.kb/          and  ~/.local/share/agent-kb/
├── SCHEMA.md            how an agent works this kb
├── log.md               append-only, greppable prefix
├── sources/             immutable, flat
├── vectors/             GENERATED cache, one sqlite file per embedding model
└── wiki/
    ├── index.md         GENERATED, never hand-edited
    └── <page>.md
```

**No service.** Processing that needs a model, embedding, atomizing, clipping,
extraction, runs as a CLI the agent shells out to. A command that can fail is
smaller than a daemon that can be down, and it removes the network surface
entirely.

**No database of record.** Agents grep. Files are the store and the query engine.
A rows index was solving a scale problem that does not exist yet, and ripgrep
over a few hundred megabytes is a second or two. The sqlite files under
`vectors/` are a rebuildable cache derived from `wiki/`, never a store of
record (see Vectors).

**The whole kb is gitignored** (operator directive, 2026-09-04). `init` writes
`.kb/.gitignore` containing `*`. An earlier version excluded `vectors/` alone,
on the reasoning that sources and pages are worth keeping in history; observed
practice went the other way in three repositories out of four, and a kb is
local working knowledge that grows on its own clock rather than project source.

**`.kb` is the store**, not a pointer to one. A pointer file stays available
later if a project needs to share a store; the indirection buys nothing until
two projects want the same kb.

---

## Sources

Flat, immutable, append-only. Three origins land in the same directory and are
distinguished by frontmatter, not by path: curated by a human, discovered by an
agent mid-research, or fetched by a collector.

Collector output is a timestamped snapshot, `<job>__<ISO8601>.json`, never
overwritten. A re-fetch is a new file. That gives history for free: *when did
this value change* becomes answerable, where an upsert would have destroyed the
answer.

### The one central rule

Every snapshot declares `mode: full` or `mode: partial`.

- **full** means this is everything as of that moment, so anything absent is
  gone upstream.
- **partial** means here is what changed and nothing is claimed about the rest.

Without that flag, a record missing from today's file is ambiguous between
deleted and simply not mentioned, and nothing downstream can tell which. It is
one frontmatter field, not a diff engine.

Everything else about collection strategy belongs to the collector, because only
it knows whether its source can answer *what changed since T* or only hand over
the whole document. A full pull every day and an incremental pull every day are
both correct, for different sources.

A job that only ever pulls partials can never detect removals, so a withdrawn
record lingers forever. If that matters for a given source, that collector
schedules an occasional full pull. Also its choice.

Retention in v1: none. Snapshots are kept forever.

---

## Wiki

Pages are updated, not regenerated. That single choice decides several others:
pages accumulate reasoning that exists nowhere else, so they are truth alongside
sources rather than a disposable rendering, and an agent writing a conclusion
onto a page will still find it there next week.

### Backlinks

Pages connect with `[[wikilink]]` syntax. The backlink query is
`rg -l '\[\[page-name\]\]'`, which needs no stored index and is correct by
construction. No edge table, no traversal primitive.

Backlinks answer *given this page, what else touches it*. They cannot answer
*where do I start*. The index file covers that for a cheap first read, and the
`search` verb covers it when the index is not enough (see Vectors).

### index.md is generated

Built from page frontmatter, `title`, `summary`, `category`, `updated`, and
nothing else. Regenerating rather than editing in place means two agents racing
produce the same file and the loser loses nothing, so no lock is needed.

It catalogs **wiki pages only, never sources**. At a few hundred pages the whole
index is roughly five thousand tokens, which is cheaper than three wrong reads.
At the eight thousand notes already sitting in the existing vault it would be a
hundred and thirty thousand, which is most of a context window spent before
answering anything. Sources stay reachable by search, where a snippet costs a
few hundred tokens instead of a catalog line each.

### The `page` verb

`llm-wiki page TITLE` is the only way to write into `wiki/`. Page identity
is the frontmatter `title`, not the filename: two titles that fold to the
same slug (case, whitespace, NFC/NFD) update the same page, and a page
with no stored title yet just adopts whatever title comes next. Two
titles that only look alike within `slugify`'s length cap are still
compared in full and refused as a collision rather than silently merged.

A body is required on stdin for every call except `--touch`, which
re-stamps `updated` and leaves the body untouched. `--touch` still reads
whatever stdin holds and refuses a non-empty one instead of guessing
which the caller meant; it never inspects what kind of file descriptor
stdin is, so a tty, a pipe, and a redirected file all behave the same.

`index.md` can lag a page that just landed under concurrent writers (see
above); `llm-wiki index` always repairs it from whatever is actually on
disk.

### log.md

Append-only, one line per ingest, query or lint pass, each entry opening with a
fixed prefix so `grep "^## \[" log.md | tail -5` is the whole reader. Appends
are atomic at this size, so concurrent writers are safe. It also gives collector
run bookkeeping a home that a human can read, which is why there is no separate
state file.

---

## Retrieval, ranked by token cost

The optimization target is how few tokens an agent spends to answer a question.
Ranked by actual impact, largest first.

1. **A previously answered question should be a page, not a search.** Reading one
   synthesized page is 500 to 1,500 tokens. Searching and re-deriving the same
   answer from three sources is 20,000 or more. Filing answers back is the cache,
   and it is the only item here that gets cheaper the more the kb is used.
   Everything below is a constant factor.
2. **Never return bodies by default.** Path, title, snippet, score. The agent
   picks, then fetches. The failure mode to avoid is any tool that helpfully
   returns full content.
3. **Chunk, so a hit is a section.** A 10,000 word document is one hit that means
   somewhere in here. This is what atomization already buys.
4. **Field projection on structured data.** Grepping a raw snapshot to answer one
   question can drag 1.6 MB toward the context, roughly 400,000 tokens. A
   projected query over the same data is a few hundred. Four orders of magnitude,
   and the worst single thing an agent can do here.
5. **An overview page, so the first query is targeted.** Blind exploration costs a
   round trip per guess, and every tool result stays in context for the rest of
   the session.
6. **SCHEMA.md is what makes the agent take the cheap path.** All of the above is
   available and none of it is chosen unless the routing is written down.

Metadata should answer freshness without a read. If an agent has to open a page
to learn it is stale, full price was paid to discard it.

### Vectors are on wiki pages only

Never on raw sources, never on chunks, and never on collector rows: identifier
lookups want exact match, and near-duplicate records collapse into one region of
vector space until top-k stops discriminating. The store and the verbs are in
the next section.

---

## Vectors

One vector per `summary` page and one per synthesis page, never per raw source
and never per chunk. Two consumers: dedup inside serial ingest, which takes the
nearest pages as its candidate set, and the agent's `search` verb. Deferring
vectors was rejected because dedup consumes them at ingest, so they exist from
day one.

**Store.** A sqlite-vec `vec0` table, cosine metric, one file per embedding
model at `.kb/vectors/<model-slug>.sqlite`. The slug is the model name with `/`
and `:` replaced by `--`. The configured embed model selects the file; a missing
file is created empty on first use. Each file's `vec0` has fixed dimensions for
its model, and vectors from different models are never compared. Switching back
to a previous model is free, and two files side by side is what the embedding
arena needs to compare models.

**Row.** `page_path` as primary key, `file_hash`, `kind` as a filterable
metadata column, and the vector. `file_hash` is the sha256 of the row's own
semantic content: kind, title, and the embedded text. A row is stale when that
hash differs; mtime is never used. Frontmatter (identifiers, domain fields)
still shapes the vector, because it sits inside that embedded text, but a
frontmatter field the embedding never reads, dedup's `story:` back-reference
say, no longer costs a paid re-embed. The column name is historical: it once
held a whole-file hash, and it is kept so an existing database still reads.
Content hash is what local-first tools
converge on. The model name is not stored per row; one model per file makes it
redundant.

**What is embedded.** Not the whole file. The text fed to the model is `title`,
`identifiers`, and the `summary` frontmatter field when present, else the first
N characters of the body (N set per model, about 2k). For CLI summary pages the
body is already the abstract, so this is the whole page. For agent pages it is
the lever SCHEMA.md gets: write a `summary` field and that is what search sees.
Long text embedded whole gives a diffuse vector that loses to a short stub on
the same topic, which is the length bias the embedding arena must include in
its fixture. The hash stays whole-file.

**A cache, never a store of record.** Gitignored. Delete it on any trouble;
`embed` rebuilds it from `wiki/`. Page row and vector are written in one sqlite
transaction, so there are no orphan vectors and no dual-write ordering problem.
sqlite-vec is pre-1.0 with breaking changes expected, so the pin is
`sqlite-vec>=0.1.9,<0.2`. Licence MIT/Apache dual. KNN is brute-force, no ANN,
which is fine at wiki scale; the upgrade trigger stays observable (query
latency), not architectural. No numpy: vectors are serialized as float32 through
sqlite-vec's helpers.

**Verbs.**

- `llm-wiki embed [<page>...]`. With pages, embeds those. Without, sweeps
  `wiki/`, compares hashes, embeds only missing or stale rows, and deletes rows
  for pages that no longer exist. Ingest runs the sweep-less path for the page
  it wrote. SCHEMA.md tells the agent to call `embed <page>` after writing a
  page; the sweep at the start of every ingest is the safety net. A sweep after
  a model change is the one paid fan-out over the whole wiki. There is no spend
  ceiling by decision; the sweep prints its planned page count before the first
  call, so an oversized sweep is visible in the log. Revisit only if a runaway
  run recurs.
- `llm-wiki status`. Read-only, no model calls. Reports wiki pages with no
  current vector in the configured model's file, and sources under `sources/`
  with no summary page, which is an interrupted ingest. Lists the offenders and
  the fix command.
- `llm-wiki search "<text>" [-n N] [--kind K]`. Embeds the query with the
  configured model and prints `score<TAB>path<TAB>title<TAB>updated<TAB>bytes`
  lines (`last_seen` stands in for `updated` on story pages). The two extra
  columns are projection, not ranking: freshness and body length are visible
  without a page read, so the agent can discount stale pages and stubs. `kind` is the
  only filter; identifier joins and hybrid ranking are out of scope. Starting
  defaults are top-k 10 and a cosine score cutoff of 0.5, both tunable and to be
  set by the embedding arena. Before searching it runs the `status` check. Any
  page without a current vector is a refusal: nonzero exit, and the message
  names the count and `llm-wiki embed`, because an empty result would read as
  "the wiki has nothing on X". Unsummarized sources only warn on stderr, since
  search is still correct about every page it can see. Exact identifier lookup
  stays `rg`.

**Rejected.** Per-page sidecar vector files (thousands of small reads per
query). Vectors in frontmatter (bloats every page the agent reads). A whole
search-layer tool that bundles its own embedding model (conflicts with per-step
model configuration and does not do storage). Float blobs plus numpy (an extra
dependency and cosine by hand). usearch, faiss, or lancedb (ANN not needed yet,
and heavy). Silently searching only the rows that match the current model on a
mismatch (an undetectable lie).

---

## Vocabulary and lint

The substrate has no identifier vocabulary of its own. A kb declares its keys in
`.kb/config.toml`, one sub-table per key beside the `[models]` table:

    [identifiers.cve]
    pattern = "^CVE-\\d{4}-\\d{4,}$"
    describe = "CVE id as printed in the advisory"

    [identifiers.ingredient]

Both sub-keys are optional. No `pattern` means any non-empty value; no
`describe` means the key name alone is shown to the model. The CLI appends the
declared table to the summarizer prompt, so SUMMARIZE.md never repeats it and
the two cannot drift. No `[identifiers]` table at all means no keys exist and
any identifier on any page is an error; there is no lenient mode.

On a page, `identifiers` is a flat list of `key:value` strings
(`cve:CVE-2024-1234`, `ingredient:egg`). The prefix up to the first colon is
the key and must be declared; the rest is the value and must match the pattern
if one is set. Matching is exact. Case is the regex author's choice;
normalising values for a join belongs to dedup, not lint.

`llm-wiki lint [<page>...]` reads every page under `wiki/`, CLI-written or not,
and runs seven checks, each of which would hold for a recipe box as well as a
security wiki:

1. frontmatter does not parse
2. identifier key not declared
3. identifier value fails its pattern
4. a page the CLI did not write wikilinks a `kind: summary` page (synthesis
   cites sources, never summaries)
5. a summary whose `source` hash has no file in `sources/`
6. a story whose `members` are not summary hashes

There are no warnings and no auto-fix. Findings go to stdout, one per line as
`path`, check, detail, tab-separated; the exit code is 1 if there are any;
log.md gets one line with the counts. Semantic lint, contradictions, staleness,
missing freshness fields, is the agent's job per SCHEMA.md, as in Karpathy's
original.

Ingest runs the same checks on each summary or story it has just written. A
page that fails is not kept: the source stays in `sources/`, log.md names the
source and the check, and ingest moves to the next source. The next run retries
it because the summary is missing.

---

## Dedup and stories

Dedup runs once per source inside serial ingest, after summarize and embed. Its
output is story membership: every summary belongs to exactly one story page, and
a story with one member is a singleton. Singletons cost one small page and buy a
uniform rule for search folding and rebuild.

**Join.** Lint compares identifiers exactly; dedup normalises for the join. The
key is untouched. The value is NFKC-normalised, casefolded, trimmed, and internal
whitespace collapsed to one space. One rule for every key, whether or not it has
a pattern.

**Candidates.** Candidates are story pages, never summaries. Two sources, unioned:
every story whose normalised identifier set intersects the new summary's, and,
when a vector file exists for the configured embed model, the stories of the five
nearest summaries. No vector file means the second source is empty and dedup
still runs. Order: most shared identifiers first, then earliest `first_seen`.

**Judgment.** Zero candidates: a new story, no model call. Otherwise the dedup
model from `[models]` receives the new summary and each candidate story, each as
frontmatter plus body, and answers on its first line with exactly one candidate
slug or `NONE`. Anything else is read as `NONE` and logged. With no dedup model
configured the fallback is deterministic: the first candidate. A summary with no
identifiers has no join candidates; vector candidates apply when they exist;
otherwise it becomes a singleton.

**Story page.** Frontmatter: `kind: story`, `title` (first member's title),
`members` (summary hashes, arrival order), `identifiers` (union, first spelling
kept per normalised form), `first_seen` and `last_seen` (min and max of the
members' `fetched`), `model`. The body is regenerated on every write: one
`## <member title>` section per member holding its abstract, in member order.
Path `wiki/<slugify(title)>.md`; if that path holds a different page the slug
gets `-<first 8 hex of the first member hash>`. The summary gets `story: <slug>`
written back. Both pages pass lint before they are kept; a failing story is not
written, the summary stays story-less, the log names it, ingest continues.

**Rebuild.** `dedup --rebuild` deletes every story page, drops `story:` from
every summary, and replays summaries ordered by (`fetched`, hash). Manual only,
no cadence; run it after changing the dedup model or after a logged drop. The
deterministic order is what repairs order-dependence and a lost race.

**Log.** One line per summary, `## [dedup] <hash> -> <slug> (new|joined) - <ts>`,
and one `## [dedup] rebuild N summaries into M stories - <ts>` per rebuild.

**Push.** After dedup places a summary, the same join runs against every agent
page's `identifiers`. One stdout line per match, `push\t<page>\t<shared>`, and
one log line `## [push] <hash> touches <page> (<n> shared) - <ts>`. The CLI
never edits the agent page; reacting is the agent's maintain workflow. Not run
at rebuild. No opt-out: a page with no identifiers never matches.

## Models and configuration

Every paid pipeline step (summarize, summarize_image, embed, dedup) has its own
model, set in `.kb/config.toml` beside `SCHEMA.md`. Each id is
`"<provider>:<model>"`: the prefix names a table under `[providers]`, and the
rest is a model that provider serves. The prefix is always required, so an id
says which endpoint answers it without the reader counting how many providers
are declared.

```toml
[models]
summarize = "hosted:your-summarize-model"
embed = "desktop:your-embed-model"
dedup = "hosted:your-judge-model"

[providers.hosted]
url = "https://api.example.com/v1"
key_env = "LLM_WIKI_API_KEY_HOSTED"
pdf_part = "file"

[providers.desktop]
url = "http://127.0.0.1:1234/v1"
```

A provider table takes four keys and rejects any other: `url`, `key_env`,
`key_file_env`, and `pdf_part`. `key_env` and `key_file_env` name environment
variables, never hold a value, and are both optional: a provider naming neither
gets no `Authorization` header, which is what a server on the operator's own
machine wants. `pdf_part` says how that one endpoint takes a PDF, so two
endpoints with different limits no longer share one global answer. This
replaces the single `[endpoint]` table, and a config still holding `[endpoint]`
is refused with a message naming what to write instead.

Model ids are whatever the named provider accepts. There is no per-run model
override flag. The file is read with the standard library `tomllib`. The embed
target selects the vector file, keyed on the full `provider:model` string so
two providers serving the same model name get separate databases (see Vectors).
No spend ceiling exists: paid runs print their planned counts and the
operations journal records what ran.

Asking whether a step is turned on is a separate question from asking whether
its id is valid. A missing `[models]` key means the step is off and its caller
takes a documented fallback; a key that is present but malformed raises, so a
typo can never read as "not configured" and quietly do less work.

## Media

One rule for everything that is not already text: derive text once at ingest,
store it beside the untouched original, index the text, and cite back to the
original with an anchor. Search, embeddings and agent reading are all text, so
anything else is invisible until it produces a durable text artifact.

### Images

Bytes are captured into a per-project `assets/`, named by content hash, which
gives dedupe when the same figure appears twice and makes re-ingesting a source
idempotent. The note references the image in place with ordinary markdown, so it
renders in Obsidian with no new format.

The generated description lives in the alt text, with a caption line beneath for
anything longer. That puts it in the note body, so keyword search covers it with
no schema change, and an agent knows what a figure shows without ever loading
pixels. For charts, diagrams and screenshots, OCR of the labels and values
matters more than any description of the composition.

### Only the images that carry meaning

Three signals, all free, before any model call:

- **Dimensions.** Under roughly 200px on a side is an icon, avatar, spacer or
  tracking pixel. Extreme aspect ratios are banners. SVG is almost always an icon.
- **Markup context.** Inside a `<figure>`, or carrying a caption or real alt text,
  is a figure the author meant. In the header, nav or footer, or with `logo`,
  `icon`, `avatar` or `ad` in the class or filename, is not.
- **Hash frequency.** An image whose hash already appears across many notes is
  site furniture. A logo repeats on every clip from a domain; a figure does not.
  Self-tuning, and more accurate as the vault grows.

For PDFs the heuristics differ: figures are large, embedded, and usually have
`Figure N` or `Table N` in nearby text. Because the bytes are kept, skipping a
description is reversible at any time, which is what makes a crude rule safe to
start with.

### Long-form

A book is roughly a million tokens and is read once, expensively, into pages.
Every later question is answered from those pages. Chunking has to be
size-bounded with overlap rather than heading-based, since extracted PDF text
rarely has reliable headings, and each chunk needs a positional anchor or
citations degrade to *somewhere in this book*.

PDF and EPUB extraction does not exist yet. Neither does transcription. Both are
ingest-time CLI work when they arrive.

---

## Collector

Completely separate from the kb. Job definitions, credentials, a cadence, and a
directory of timestamped snapshots it has fetched. Agents point at it, filter
what it holds, and push what they want into the kb.

It is not part of the wiki: no pages, no schema, no embedding, no atomizing. All
of that already exists on the kb side.

**Ingest is agent-driven.** The collector never pushes on its own; an agent
decides what crosses into the kb. Automatic ingest fills the kb with raw volume,
which is dumping rather than distilling. Agent-driven keeps the collector a
staging area and the kb curated, and means the collector needs no knowledge of
kb internals beyond one call.

**Store, do not proxy.** Snapshots land on disk rather than every question
becoming a live API call. History, repeat queries without re-hitting a rate
limit, and the ability to answer what changed later.

**One plugin per call**, not one per vendor. A shared thin client per vendor
holds auth and paging. Per-endpoint cadence, independent failure, and a plugin
small enough to review on one screen. Paging style differs per endpoint, not per
vendor.

---


**Decided interface (ticket .6).** `ingest <url|path|->...` is the one door for
feeds, snapshots, and an agent mid-answer. Bytes are keyed by sha256 as
`sources/<hash>.<ext>`; a provenance file `sources/<hash>.toml` (url, fetched,
content_type, job) sits beside them as the raw file's label. Jobs live in
`config.toml` as `[jobs.<name>]` with `feed` or `urls` and `mode` `partial`
(skip URLs already seen) or `full` (fetch all, hash keying skips unchanged
bytes). Cadence is the OS scheduler calling `ingest --job <name>`; the CLI is a
one-shot process. Output one line per source `hash\tnew|exists|failed\turl`;
failures never stop a job.

## Credentials

The one part of the closed PR that survives every storage argument since, because
it never depended on any of them. Secrets management belongs to whoever deploys
this. The code ships no secret store and names no vendor.

**Two forms.** `<VAR>` as a direct value, or `<VAR>_FILE` as a path whose
contents are read with exactly one trailing newline stripped. First hit wins.
Neither present means the plugin is skipped loudly and the run continues. The
`_FILE` convention is what Prometheus, Grafana and the official Postgres and
MySQL images already use.

**No exec resolver.** There is no `_CMD` form and no shelling out to fetch a
secret. Every platform already lands secrets as an env var or a file, so it buys
nothing and adds an arbitrary code execution surface. A deployer needing a vault
runs something that writes a file.

**Isolation.** One subprocess per plugin and connection, with a hand-built
environment dict. The parent alone resolves secrets and reads files; the child
never touches any store and never inherits the parent environment. Inheriting the
whole environment hands every plugin every other plugin's credentials.

**Masked handle.** A secret is a late-resolving handle whose text and repr forms
are masked by construction, never a string. Masking at the type level is the only
version that survives an f-string, a list repr, and a traceback written by code
that had never heard of secrets.

A secret never appears in argv, in stored content, in a log line, in an exception
message, or in a file written to a temp directory. Strip the authorization header
before re-raising, and never echo a URL carrying a token in its query string.

---

## What was cut, and why

Each of these was designed, and in two cases built, before being removed.
Recorded so they are not re-proposed.

| Cut | Why |
|---|---|
| The container topology: an api container, a collector container and a shared volume | Append-only snapshots mean no two writes touch the same file, which killed the single-writer requirement that was the only reason the api had to own the volume |
| A database container | SQLite is an in-process library, not a server. A separate container either wraps it in a server that has to be invented, or shares one file between two writers |
| The rows database and identifier join table | Solving a scale problem that has not arrived. Grep answers the same questions at the sizes in play |
| Deferring vectors past v1 | Dedup at ingest consumes vectors, so they have to exist on day one. Mixed models producing incomparable vectors is solved by one sqlite-vec file per model, never compared across files |
| A central diff engine | Only the collector knows whether its source supports incremental fetch. The framework needs one field, not a strategy |
| An edge table and a path traversal primitive | The agent is the traversal engine and joins with successive cheap queries. Persisting edges only pays when a hop is a live API call |
| RBAC | Never built. Permissions are inherited from whatever holds the bytes |

---

## Still open

- **Scope union.** A project kb and the global kb are both in scope. Whether an
  agent searches both by default, and in what order, is undecided. It needs to be
  explicit or answers will silently draw on the wrong store.
- **Identifier vocabulary.** Resolved, see Vocabulary and lint: declared per kb in
  config.toml, none in the substrate.
- **Extraction and transcription.** No PDF path, no EPUB path, no ASR. All
  ingest-time CLI work, none of it written.
- **Structured query over collector data.** Deferred along with the service. Item
  four of the retrieval ranking is the argument for bringing it back, and the
  trigger is an agent grepping a large snapshot.

Out of scope by decision: whether `.kb` is committed to a project repository is
the team's call.

---

## Repo state at time of writing

- **agent-kb.** `main` clean at `3a93330`. PR #1 closed. Branch `scaffolding`
  retained for the credential structures only; its storage model is superseded by
  this document.
- **agent-workbench.** Branch `perf/kb-incremental-index` holds uncommitted work
  on the whole-vault reindex, stopped mid-review. `main` untouched.
- **existing kb.** Unchanged and still the daily driver. 13 projects, 8,805
  notes. It rebuilds its whole keyword index on every write, which is the bug that
  branch was addressing.
