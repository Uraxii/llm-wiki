# Skeptic gate: llm_wiki.py against the page-kind contract

Ticket: agent-kb-0zf.9. Target: `llm_wiki.py` at HEAD `7d812b8`, 839 lines.
Findings only, nothing fixed.

Contract read from `bd comments` on agent-kb-0zf.1, .2, .3, .4, .5, .8, .9,
`docs/design/llm-wiki.md` (working tree), `CONTEXT.md`, and `kb_lint.py` as
the reference style.

The ownership rule that decides most of this, from .2: the CLI writes exactly
`sources/`, `wiki/` pages of kind `summary` and `story`, and `log.md`. Nothing
else. Any page the CLI did not write belongs to the agent. `index.md` is the
agent's.

Assumption stated because the user rule "ask if unclear" cannot be honoured
from a leaf agent: where a decision comment and the design doc differ in
wording I took the bd comment as authoritative, since the comments are the
resolutions and the doc is their transcription.

---

## 1. Verb inventory at HEAD

Seven verbs, `build_parser` L775-829.

| Verb | Writes | Verdict | Evidence |
|---|---|---|---|
| `init` | `sources/`, `wiki/`, `SCHEMA.md`, `log.md`, **`wiki/index.md`** | DEFECT | L628 calls `write_index`; no `config.toml`, no `.gitignore` for `vectors/` anywhere in L609-629 |
| `where` | nothing | PASS | L632-637, pure read, resolution logic L323-334 is sound |
| `add` | one file in `sources/`, `log.md`, **`wiki/index.md`** | DEFECT | L667 names the file `slugify(title)`, not a content hash; L668 regenerates an index that catalogs wiki pages only |
| `page` | one file in `wiki/`, `log.md`, **`wiki/index.md`** | DEFECT | L718-726 forces `PAGE_FIELDS`, cannot set `kind`, and round-trips through the scalar-only `parse_frontmatter` at L235 |
| `index` | `wiki/index.md` | DELETE | L732-735; the whole file is the agent's per .2 |
| `log` | `log.md` | DELETE | L738-743; a one-line append to an append-only file, no caller, documented "repair-only" at L74 |
| `links` | nothing | DELETE | L746-772; 27 lines wrapping one `rg -l` that the design doc already names as the backlink query |

Three of seven survive in any form, and all three carry defects.

---

## 2. Defects, reproduced

Reproduced on a throwaway kb at
`/tmp/claude-1000/.../scratchpad/gate`, never on `tests/fixtures`.

### D1. `page` corrupts a list-valued `identifiers` block

`parse_frontmatter` (L235-269) reads scalars only; `render_frontmatter`
(L272-281) writes `key: "value"` and quotes whatever it was handed. A page
carrying the contract's `identifiers` list goes in intact and comes out as
invalid YAML.

```
$ cat .kb/wiki/rust-1-90-release.md      # before
---
kind: summary
title: "Rust 1.90 Release"
source: "a1b2c3d4e5f6"
identifiers:
  - "cve:CVE-2024-1234"
  - "crate:serde"
story: "rust-release"
---

$ llm-wiki page "Rust 1.90 Release" --touch < /dev/null
$ cat .kb/wiki/rust-1-90-release.md      # after
---
title: "Rust 1.90 Release"
summary: ""
category: ""
updated: "2026-08-29T23:35:58Z"
kind: "summary"
source: "a1b2c3d4e5f6"
identifiers: ""
- "cve: "CVE-2024-1234""
- "crate: "serde""
story: "rust-release"
---
```

Expected: the block round-trips byte for byte, which is what `--touch`
promises. Observed: `identifiers` becomes an empty string, each list item
becomes a top-level key whose name starts with `- `, and the value carries
unescaped nested quotes. The page is now unparseable by `kb_lint.parse_frontmatter`
(confirmed below).

`--touch` is the mildest possible call, the one that claims to change only
`updated`. Every other `page` write does the same thing.

### D2. The corrupted page fails lint, and so does the CLI's own index.md

```
$ python3 -c "import kb_lint, pathlib; [print(f) for f in kb_lint.lint_pages(pathlib.Path('.kb'))]"
.kb/wiki/index.md              frontmatter  frontmatter missing or unparseable
.kb/wiki/rust-1-90-release.md  frontmatter  frontmatter missing or unparseable
```

Lint scope per .3 is "every page under `wiki/`, CLI-written or not".
`generate_index` (L444-466) opens with `INDEX_BANNER`, an HTML comment, so the
file has no `---` block and check 1 fires on it forever. As long as the CLI
writes `wiki/index.md`, `llm-wiki lint` can never exit 0 on a fresh kb.
This is not a lint bug. It is the ownership violation showing up mechanically.

### D3. `init` writes no `config.toml` and does not gitignore `vectors/`

```
$ llm-wiki init ./.kb
$ find .kb -type f
.kb/log.md
.kb/SCHEMA.md
.kb/wiki/index.md
$ ls .kb/config.toml
ls: cannot access '.kb/config.toml': No such file or directory
```

Expected per .8 and .9: a `[models]` table with `summarize`, `embed`, `dedup`,
a commented `[identifiers]` table per .3, and `.kb/vectors/` gitignored per .1.
Observed: none of it. Nothing in `llm_wiki.py` reads or writes TOML; `tomllib`
is not imported. Every model-config and vocabulary decision is unimplemented at
the only point that could seed it.

### D4. Sources are named by title slug, not content hash

```
$ echo "body" | llm-wiki add "Rust 1.90 Release" --origin research
.kb/sources/rust-1-90-release.md
```

`CONTEXT.md`: "an immutable raw file under `sources/`, addressed by content
hash." `.2`: a summary's `source` is "content hash in `sources/`, the only link
between layers." `kb_lint.py:85` builds `source_hashes` from `p.stem`, so it
reads the source filename as the hash.

A summary written with `source: <sha256>` therefore never matches a slug-named
file, and lint check 5 (dangling-source) fires on every correctly-written
summary. The one link between the two layers is broken at the naming level.

### D5. `add` regenerates an index that cannot have changed

L668 calls `write_index(kb)` after writing a source. The design doc is explicit
that the index "catalogs wiki pages only, never sources". Adding a source can
never change a row. This is a wasted full rescan of `wiki/` per ingest and, per
the ownership rule, a write into a file the CLI does not own.

### D6. `page` cannot write either kind the CLI owns

`PAGE_FIELDS = ("title", "summary", "category", "updated")` (L31). There is no
`--kind` flag (L798-809), no `--identifiers`, no `--source`, no `--members`.
The verb can carry `kind` through only as an accident of the `extra_fields`
passthrough at L724, and only for a page some other tool already wrote.

Meanwhile it injects `summary: ""` and `category: ""` into every page it
touches (visible in D1 above). Neither key appears in the contract frontmatter
for `summary` or for `story`. The CLI's only wiki-write verb writes the wrong
shape and cannot be told the right one.

### D7. Stated conventions violated

- `import argparse` (L11). The standing convention for PoC modules is no
  argparse; `kb_lint.py:130-134` hand-rolls argv in five lines and is the
  reference style.
- `add --url` needs `readability-lxml` and `lxml` (L585-586). Stdlib only is
  the standing convention. The fetch guard itself is stdlib and worth keeping;
  the extraction step is what pulls the dependency.

### D8. `SCHEMA_TEMPLATE` documents a CLI that no longer exists

L47-218, 172 lines, 20% of the file, written into every new kb by `init`.
It states "Scalars only. No nested values, no lists" (L90), documents `index`,
`log` and `links` as verbs, says `index.md` is generated by the CLI (L113),
and mentions nothing about vectors, lint, config.toml, identifiers, summary,
story or dedup. A new kb is seeded with instructions that contradict five
resolved tickets.

### D9. The CLI's regression suite is invisible to the project test command

`test_llm_wiki.py` sits at repo root, 741 lines, 39 pytest-style functions.

```
$ python3 -m unittest discover tests
Ran 5 tests ... OK                      # tests/test_lint.py only
$ python3 -m unittest discover -s . -p 'test_llm_wiki.py'
Ran 0 tests ... NO TESTS RAN            # no TestCase subclasses
$ python3 -m pytest test_llm_wiki.py -q
46 passed in 11.35s
```

The suite is green but nothing in the project's stated test command runs it.
Whichever way the rewrite goes, the surviving tests need to move under `tests/`
and match the runner `tests/test_lint.py` already uses.

### D10. Fenced-output case from .4 is silently swallowed

The .4 addendum requires the CLI to either strip a code fence or reject the
page when a model wraps its output. `parse_frontmatter` L242 returns
`({}, text)` for anything not starting with `---\n`, so a fenced page degrades
to "no frontmatter" and `page` then writes a fresh scalar block above the fence.
Neither of the two allowed behaviours. Not reproducible as a crash, which is
the problem.

**Defect count: 10.**

---

## 3. Contract gaps

Capabilities the resolutions require that the file does not have at all.

| Required by | What is missing |
|---|---|
| .1 | `embed`, `status`, `search` verbs. No sqlite-vec, no `sqlite3` import, no `.kb/vectors/` |
| .3 | `lint` verb wiring `kb_lint.lint_pages`; `prompt_block(config)` appended to the summarizer prompt |
| .3, .8 | Any TOML reading. `tomllib` is not imported. `[models]` and `[identifiers]` are unreachable |
| .4 | The summarize step itself: no model call, no `SUMMARIZE.md` read, no built-in prompt skeleton |
| .5 | `dedup`, `dedup --rebuild`. No story page writer, no identifier normalisation (NFKC/casefold/collapse) |
| .2, .5 | An `ingest` path that chains summarize, embed, dedup and self-lint per source |
| .3, .5 | Self-lint before keep: a failing page is not written, source stays, log names it, ingest continues |
| .8 | Planned-count printing before any whole-wiki fan-out |

Direct contradictions rather than absences:

- **`index.md` ownership.** Four code paths write it (`init` L628, `add` L668,
  `page` L727, `index` L735). The contract says zero.
- **Scalar-only frontmatter.** L90 of the template and L235-281 of the code
  both assert it. `identifiers` and `members` are lists in every contract.
  `kb_lint.parse_frontmatter` (L27-53) already implements the correct subset
  and returns `None` on malformed input instead of degrading; there are now two
  incompatible frontmatter parsers in the repo, and the CLI has the wrong one.
- **Page identity.** `_normalize_title_for_compare` (L299-308) makes a slug
  collision a hard error. Under .5 a story colliding on its slug gets a
  `-<first 8 hex of first member hash>` suffix and proceeds. Same situation,
  opposite behaviour.
- **Frontmatter key set.** `updated` is stamped on every page (L722). Neither
  contract kind has `updated`; summaries have `fetched`, stories have
  `first_seen`/`last_seen`.

---

## 4. The three named questions

### `index`: delete the verb, do not keep it as a helper

Recommendation: **delete**, and delete `generate_index`, `write_index`,
`INDEX_BANNER` and `PAGE_FIELDS` with it.

Reasoning from the ownership rule. The CLI writes `sources/`, summary and story
pages, `log.md`, and vectors. `index.md` is on the agent's side of that line by
an explicit decision in .2, citing Karpathy on the LLM maintaining the wiki's
special files.

"Dumb helper the agent may call" fails on its own terms. The helper's output
shape is `PAGE_FIELDS`: title, summary, category, updated. No CLI-written page
carries `summary`, `category` or `updated` under the new contract. So the
helper would generate a table of four columns that are empty for every page the
CLI produces, which is what D1's output already shows happening. It is a helper
for nobody.

There is also a mechanical cost to keeping it: D2 shows the generated file is a
permanent lint finding, because lint covers every page under `wiki/` and the
banner means no frontmatter. Keeping the helper means either carving an
exception into lint for one filename, or teaching the generator to emit
frontmatter it has no fields for. Both are worse than deleting 40 lines.

The design doc's own answer for "where do I start" is now `search` (.1), which
is a CLI verb the CLI genuinely owns because it owns the vectors. That is the
replacement.

### `links`: delete the verb, make it a SCHEMA.md convention

Recommendation: **delete**. `cmd_links` writes nothing, so the ownership rule
does not place it in the CLI at all.

The design doc already answers this without the verb: "The backlink query is
`rg -l '\[\[page-name\]\]'`, which needs no stored index and is correct by
construction." `cmd_links` (L746-772) is 27 lines shelling out to exactly that
command, plus a hand-rolled Python fallback for machines without `rg`, plus an
`rg` exit-code table (L39). The verb is strictly more code than the thing it
runs, and one more surface to keep in sync with the wikilink syntax.

The convention line belongs in the `SCHEMA.md` template under a "no search
verb, `rg` is the query engine" heading, which is where the current template
already puts it (L158-164). Only the sentence pointing at `llm-wiki links`
comes out.

One note for whoever does the delete: `kb_lint.py:18` already owns wikilink
parsing (`WIKILINK = re.compile(r"\[\[([^\]|#]+)")`) for check 4. If the
pipeline ever needs a programmatic backlink walk, that regex is where it lives,
not a resurrected verb.

### `log`: delete the verb, keep `append_log_entry`

Recommendation: **delete `cmd_log`, keep `append_log_entry` (L484-495) and
`append_log_entry_best_effort` (L498-504) as functions.**

`log.md` is the one derived file the CLI does own, so the function stays. The
verb does not, for two reasons. It is documented as repair-only (L74) and has
no caller. And the pattern for using it is already established in the other
direction: `kb_lint.py:15` imports `append_log_entry` from `llm_wiki` and calls
it in-process at line 139 rather than shelling out to the verb. Every new step
(summarize, embed, dedup, ingest) will do the same. A verb whose only honest
use is `echo "## [kind] title - $(date)" >> log.md` does not earn a subparser.

`.9` already fixes the shape the lint step must produce
(`## [lint] N findings over M pages - <ts>`, via `append_log_entry`, not a
literal string), which confirms the function is the interface.

---

## 5. Fix in place, or rewrite thin

**Recommendation: rewrite thin, in the `kb_lint.py` style, reusing the named
functions from section 6 as a shared module.**

Four of seven verbs die outright. Of the three that live, all three carry
defects that reach into their core: `init` must learn TOML and stop writing an
index, `add` must be re-keyed from slug to content hash, and `page` must be
replaced by kind-specific writers because a summary and a story are written by
the pipeline, not by a human passing `--summary` on the command line. The
frontmatter layer, which every surviving verb sits on, is the wrong data model
and there is already a correct implementation of it 20 lines into `kb_lint.py`.

Fix-in-place would mean editing `parse_frontmatter`, `render_frontmatter`,
`cmd_init`, `cmd_add`, `cmd_page`, `build_parser`, `SCHEMA_TEMPLATE` and
deleting four command functions. That is every part of the file that is not on
the keep list below. Calling it a fix would just be a rewrite that inherited
`argparse` and a 172-line stale template.

Deletion list, identical either way:

| Lines | What | Why |
|---|---|---|
| L47-218 | `SCHEMA_TEMPLATE` | Documents the dead contract (D8). Rewrite from the resolutions |
| L235-281 | `parse_frontmatter`, `render_frontmatter` | Scalar-only (D1). `kb_lint.parse_frontmatter` supersedes; needs a matching renderer |
| L299-308 | `_normalize_title_for_compare` | Collision-is-an-error rule replaced by the hash-suffix rule in .5 |
| L444-481 | `generate_index`, `write_index` | CLI never writes `index.md` |
| L732-735 | `cmd_index` | Same |
| L738-743 | `cmd_log` | Verb has no caller; function survives |
| L746-772 | `cmd_links` | Wraps one `rg` call the doc already names |
| L11, L775-829 | `argparse`, `build_parser` | Convention (D7); `kb_lint.py:130-134` is the pattern |
| L31, L41-45, L39 | `PAGE_FIELDS`, `INDEX_BANNER`, `RG_OK_CODES` | Orphaned by the above |
| L673-729 | `cmd_page` | Replaced by kind-specific writers |

**Lines kept: roughly 230 to 260 of 839, about 30%.** The keep list in section 6
sums to about 250 lines including the URL-fetch block and constants. Everything
else is either deleted or rewritten against the new contract.

Sequencing note, since deletion-first is cheaper here: land the four verb
deletions and the index removal as one behaviour-narrowing commit before any new
verb is written. That commit alone takes `llm-wiki lint` from "can never exit 0"
to "can", because D2 disappears with `write_index`.

---

## 6. Worth keeping

Real work, correct as written, contract-neutral. Move these into a shared
module the new verbs and `kb_lint.py` both import.

| Function | Lines | Why it survives |
|---|---|---|
| `atomic_write_text` | L358-374 | Temp file plus `os.replace`, correct `BaseException` cleanup. Every page write in the new pipeline needs exactly this |
| `_open_temp_file`, `_default_file_mode` | L337-355 | The `mkstemp`-ignores-umask fix at L337-345 is a real, non-obvious bug fix. Do not re-derive it |
| `write_source_file` | L377-401 | Exclusive `os.link` closes the add/add TOCTOU. Keep the mechanism; change the name it links to from slug to content hash (D4) |
| `append_log_entry` | L484-495 | Already the interface `kb_lint.py:15` imports. `.9` pins the log shape to it |
| `append_log_entry_best_effort` | L498-504 | Correct call: a missing log is bookkeeping trouble, not a failed write |
| `slugify` | L284-296 | Already imported by `kb_lint.py:15`; the hash fallback for non-Latin titles is needed by story paths in .5 |
| `flatten` | L228-232 | One-line records in `log.md` and frontmatter still need it |
| `utc_timestamp` | L311-313 | `fetched`, `first_seen`, `last_seen`, log entries |
| `resolve_kb`, `global_kb_root`, `ResolvedKb` | L316-334, L221-225 | Walk-up resolution is untouched by the contract; `where` is the one PASS verb |
| `require_dir` | L603-606 | Four-line guard clause, still the right shape |
| `read_stdin_body` | L414-431 | The no-heuristic decision at L414-425 was won in an earlier gate. Preserve the reasoning even if the caller changes |
| `_reject_unsafe_url`, `_read_html`, `_NoAutoRedirect`, `fetch_url_text` | L507-600 | Per-hop SSRF guard, content-type allowlist, size cap, redirect cap. This is the most valuable block in the file and has no replacement. Split the stdlib fetch-and-guard half from the `lxml` extraction half so the dependency (D7) is isolated to one function |

`fetch_url_text` deserves the note: the guard is stdlib and correct, including
the honest comment at L516-522 admitting it does not defend against DNS
rebinding. Losing that in a rewrite would be the single worst outcome of this
gate.

---

## Verification run

```
$ python3 -m unittest discover tests
Ran 5 tests in 0.200s
OK
$ python3 -m pytest test_llm_wiki.py -q
46 passed in 11.35s
```

Each verb exercised on a scratchpad kb, never on `tests/fixtures`. `init`,
`where`, `add`, `page --touch`, `index`, `log`, `links` all ran; outputs quoted
above. `add --url` not exercised: no network, per the brief.
