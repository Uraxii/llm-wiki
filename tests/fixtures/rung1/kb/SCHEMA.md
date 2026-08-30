# SCHEMA.md: the recipe box

The contract between the substrate (the `llmwiki` CLI) and the agent that
answers questions from this kb. The CLI parses nothing in this file. Machine
settings live in `config.toml`; the summarizer's instructions live in
`SUMMARIZE.md`. Everything below is the agent's operating manual.

---

## Purpose

This kb is one household's recipe box. Sources are recipes as they arrived:
cookbook pages, blog pastes, index cards, a friend's text message. The person
asking questions is standing in their own kitchen with an hour and whatever is
in the fridge, so a good answer names specific recipes, cites the source the
claim came from, and is short. "Six of your recipes use eggs, and the fastest
is the French omelette at three minutes" is a good answer. A wall of prose
about egg cookery is not.

The kb is small and grep is cheap. Prefer reading two pages over summarizing
twelve.

## Page kinds

The substrate writes two kinds and never touches any other:

- `summary`: one per source, written by the summarizer. One recipe, one page.
  Never edit one. A correction goes in an agent page that cites the source.
- `story`: one per dish, written by `dedup` from its member summaries. A story
  with two or more members is the kb telling you it holds two versions of the
  same dish. Never edit one; `dedup --rebuild` regenerates them all.

Agent kinds, which the agent writes and maintains:

| kind | purpose | required frontmatter | cites |
|---|---|---|---|
| `dish` | one dish across every version the box holds, with what differs between them and which to cook when | `title`, `kind`, `last_verified` | `sources/` hashes only |
| `technique` | a method used across dishes (folding an omelette, tempering custard, proving dough) with the recipes that lean on it | `title`, `kind`, `last_verified` | `sources/` hashes only |
| `menu` | a meal or a week that worked, with what was cooked and what to change | `title`, `kind`, `last_verified`, `cooked_on` | `sources/` hashes only |

`index.md` is an agent page too, with no `kind`. It is the agent's own map of
the box: dish pages, technique pages, and anything the agent wants to find
without a search. **The CLI never writes or reads `index.md`.** If it ever
appears without the agent having written it, that is a substrate bug worth
reporting.

Rules the lint enforces on every page, agent pages included: the frontmatter
parses; every identifier key is declared in `config.toml`; every identifier
value matches its declared pattern; no page outside the two CLI kinds wikilinks
a `summary` page; every `source` hash on a summary exists in `sources/`; every
story member is a real summary.

## Vocabulary

Declared in `config.toml` under `[identifiers.<key>]` and not restated here.

**This kb declares none, on purpose.** A recipe has no catalogue number and no
external key worth joining on, and joining on ingredients is worse than
nothing: every recipe shares salt. The consequence the agent must know is that
`dedup` has nothing to join on, so **every story page holds exactly one
member** and story pages carry no information a summary does not. Skip them in
routing until the day a vocabulary is declared.

## Summarizer

The prompt lives in `SUMMARIZE.md`; the CLI appends the declared vocabulary to
it. Every `summary` page carries, besides `kind`, `title`, `identifiers`,
`source` and `fetched`:

| field | shape | what the agent does with it |
|---|---|---|
| `dish` | lowercase plain name | groups versions of one dish; the key a `dish` page is built around |
| `course` | one of breakfast, lunch, dinner, dessert, side, snack | filters "what's for dinner" |
| `cook_time_minutes` | bare integer, active cooking only | sorts numerically; this is the field that answers "what is quick" |
| `serves` | bare integer | scales a menu |
| `main_ingredients` | one lowercase comma separated line | the ingredient grep target; the whole reason it is one line |
| `notes` | one line, or `none` | resting and chilling time, substitutions, warnings |

The body of a summary is an abstract of two or three sentences. It is not the
method. **A method question always ends in the source, never in a summary.**

Promote `dish`, `cook_time_minutes` and `notes` into a `dish` page when one is
written. Never promote a claim without the source hash it came from.

## Retrieval routing

Cheapest path first. Stop at the first step that answers the question.

1. **Frontmatter grep over `wiki/*.md`.** Every named field above is one line
   of plain text, so one `grep` answers most questions outright. Ingredient
   questions: `grep -ilE '^main_ingredients:.*\beggs?\b' wiki/*.md`. **Use the
   word boundary.** A bare `.*egg` also matches eggplant, and a box this size
   has one. Speed questions:
   `grep -H '^cook_time_minutes:' wiki/*.md | sort -t: -k3 -n`. Course, dish
   and serving-count questions the same way. **Most questions stop here.**
2. **`index.md` and page titles.** "Do we have anything for X" and "what did we
   cook in March" are index questions, not grep questions.
3. **Story pages.** One story is every version of one dish. Use it for "which
   of these do we have twice" and "what is the difference between our two". In
   this kb, with no vocabulary declared, stories are singletons and this step
   is dead; the equivalent is `grep '^dish:' wiki/*.md | sort` on the `dish`
   field, which finds the duplicates just as well.
4. **Summary bodies, then `sources/` raw.** A summary body says what a dish is.
   The source says how to cook it. Any question about quantities, steps,
   temperatures or timings within the method goes straight to
   `sources/<hash>.<ext>`; do not answer one from a summary.

A vector step will sit between 3 and 4 when the substrate grows one. It does
not exist yet, and neither does a `search` verb; do not call one.

## Substrate verbs the agent calls

The CLI has six verbs today: `init`, `where`, `ingest`, `summarize`, `dedup`,
`lint`. The agent uses three of them.

- `llmwiki ingest <path|->`: bring a recipe in mid answer. It stores the bytes,
  writes the summary page and files it into a story, in one call. A file
  already in the box comes back `exists` and costs nothing.
- `llmwiki lint [<page>...]`: run it before finishing any pass that wrote a
  page. A finding is an error, never a warning, and the agent fixes it.
- `llmwiki summarize`: the retry path. It re-runs every source whose page is
  missing or was written under an older prompt. Run it after editing
  `SUMMARIZE.md`, and after a source has failed.

**One kept source in this box has no readable text** (a photo of a recipe
card). It is stored, it has no summary page, and a bare `summarize` will keep
retrying it and keep exiting 1. That exit code is not a signal that anything
broke; check whether the failure names that source before acting on it.

## Workflows

### Answer

1. Route by the four steps above. Do not read a page a grep already ruled out.
2. Read the smallest set of pages that settles it. Two is usually enough.
3. Cite every factual claim with the source hash it came from, as `[[<hash>]]`.
4. Write nothing. An answer becomes a page only when it will be asked again.

### Extend

1. A new `summary` appeared. Read it and its `dish` field.
2. If a `dish` page for that dish exists, add one line naming what this version
   does differently and cite the new source hash. If two or more summaries now
   share a `dish` value and no page covers them, write the `dish` page.
3. If the recipe leans on a technique that already has a page, add it to that
   page's recipe list.
4. Add a line to `index.md` for any page created. Refresh `last_verified` on
   every page touched.
5. `llmwiki lint`.

### Synthesize

1. Trigger: three or more summaries share a dish, a course, or a technique, and
   answering about them keeps requiring all three to be read.
2. Write one agent page of the right kind with `title`, `kind`,
   `last_verified`, and `cooked_on` for a menu.
3. Cite `sources/` hashes only. Never wikilink a summary page; the lint rejects
   it, and a summary can be rewritten out from under the link.
4. Link the new page from `index.md`.
5. `llmwiki lint`.

### Maintain

1. `ingest` and `dedup` print a `push` line when a new recipe shares an
   identifier with an agent page. With no vocabulary declared this never fires
   in this kb; when it does, the named page is stale and is the one to revisit.
2. Refresh `last_verified` on any page re-read and found still true.
3. Resolve contradictions between two versions of a dish by naming both on the
   `dish` page with their sources. Do not pick a winner silently.
4. Delete an agent page whose dish left the box. Never delete a summary, a
   story, or anything under `sources/`.
5. `llmwiki lint` and fix every finding before finishing.

## Conventions

- **Citations** are `[[<sha256>]]` or a relative link into `sources/`. Never a
  summary page, never a story page, never a bare recipe name.
- **`last_verified`** is `YYYY-MM-DD`, the day the agent last read the page's
  sources and found it still true. A `technique` page decays slowly and is
  worth re-reading once a year. A `dish` page decays whenever a new version of
  that dish arrives, whatever the date says. A `menu` page never decays; it is
  a record of one evening and `cooked_on` is the date that matters.
- **`index.md`** is written and maintained by the agent alone, on every Extend
  and Synthesize pass. The CLI never touches it.
- **Titles** on summary pages come from the source and may collide. Two
  recipes titled the same land on two files, the second suffixed with the first
  twelve characters of its source hash. Both are real pages; read both.
