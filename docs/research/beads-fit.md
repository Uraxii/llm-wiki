# beads fit: component in llm-wiki, and unused features here

Question: does beads (https://github.com/gastownhall/beads) belong anywhere in
agent-kb / llm-wiki? Read 2026-08-29 against `bd` 1.1.0 (`bd version`, commit
`8e4e59d39`) and upstream `main`.

## Hard-constraint check

| Constraint | Result | Evidence |
|---|---|---|
| OSI licence, verified from LICENSE file | **Pass.** MIT | https://raw.githubusercontent.com/gastownhall/beads/main/LICENSE, first lines `MIT License` / `Copyright (c) 2025 Beads Contributors` |
| Self-hostable | **Pass.** Embedded Dolt runs in-process, data in `.beads/embeddeddolt/`; server mode optional | README "Embedded (default) - `bd init`. Dolt runs in-process"; local `bd where` prints `database: .../.beads/embeddeddolt` |
| No required phone-home | **Fail as shipped, opt-out available.** Anonymous usage metrics default to ON and POST to a vendor endpoint | `bd metrics` in this repo prints `Anonymous usage metrics: ON` and `Where it goes: https://gastownhall-eventsapi.com/mp/collect`. Payload per `bd metrics --help`: command name, bd version, OS platform, "keyed by a machine-derived, HMAC-protected ID". `bd metrics off` disables it, as do the env vars `BD_DISABLE_METRICS` and `DO_NOT_TRACK` (upstream `internal/metrics/metrics.go:17,19`, endpoint constant at `:21`) |
| Free, bring your own model | **Pass.** No model required for core verbs. `bd find-duplicates` has an optional AI mode (`bd --help`, Views & Reports) |

The README makes no telemetry mention. It was only found by running the binary.
That is worth remembering about this vendor's docs.

## (a) beads or its parts inside llm-wiki: **no**

llm-wiki is three layers and nothing else: `sources/` immutable, `wiki/`
model-owned, `SCHEMA.md` (docs/design/llm-wiki.md "The shape"). Standing
decision, docs/plans/01-llm-wiki-poc/overview.md:35, "no locks, no fourth
layer, no server". Every beads part is a fourth layer.

- **Dolt store.** Direct contradiction of "No database of record. Agents grep.
  Files are the store and the query engine" and of the cut list entry killing
  the rows database (docs/design/llm-wiki.md, "The shape" and "What was cut").
  Cost is not small: 143 MB `bd` binary (`ls -la /home/nicole/.local/bin/bd`)
  and 7.4 MB of `.beads/embeddeddolt/` for ~30 issues (`du -sh`).
- **Memory.** `bd remember` stores a flat key to string map in the Dolt working
  set, namespace `kv.memory.*` (upstream `internal/storage/dolt/store.go`
  lines 2943, 2982, 3170; `cmd/bd/memory.go:33` `return store.Memories()`,
  and `:64` "write lands in the Dolt working set"). Confirmed shape locally:
  `bd memories --json` returns `{"<key>": "<string>", "schema_version": 1}`.
  That is a fourth store, opaque to grep, living in a gitignored directory
  (`.beads/.gitignore` ignores `embeddeddolt/`). llm-wiki already owns this
  job with `wiki/` pages plus `log.md` (docs/design/llm-wiki.md "log.md").
- **Sync via `refs/dolt/data`.** Solves cross-machine issue-graph merge. llm-wiki
  has no such problem: `.kb` is plain files, git or rsync already moves them,
  and vectors are a rebuildable cache (docs/design/llm-wiki.md "Vectors").
  Adopting it would put a second, non-git history object in the repo.
- **Formulas and molecules.** Work-graph scheduling. "Molecules are work graphs:
  epics whose children flow through `bd ready` as dependency-ordered steps"
  (https://github.com/gastownhall/beads/blob/main/docs/workflows/molecules.md).
  Task orchestration, not knowledge storage. No mapping onto sources or pages.
- **jsonl export and agent hooks.** Export is explicitly not authoritative:
  ".beads/issues.jsonl is an export ... not the canonical cross-machine sync
  channel" (docs/core-concepts/sync-concepts.md). Nothing to reuse; llm-wiki's
  output format is markdown with frontmatter.

**Narrow use that survives: none as a runtime component.** One idea is worth
stealing without the code: beads' rule that the export is never the source of
truth is the same rule llm-wiki already applies to `vectors/`. Design agreement,
not a dependency.

Keep beads where it is, as the tracker for building llm-wiki. That use touches
`.beads/` only and never enters a kb.

## (b) beads features this repo should be using: **yes, four**

Current state: 2 memories (`bd memories`), 0 formulas (`bd formula list`, both
search paths empty), no `.beads/issues.jsonl` (export.auto commented out in
`.beads/config.yaml`), `bd doctor` unavailable ("'bd doctor' is not yet
supported in embedded mode").

- **Formulas.** docs/plans/01-llm-wiki-poc/overview.md describes numbered phases
  with a fixed dependency order, hand-created as beads today. A formula is a
  TOML template in `.beads/formulas/`, cooked with `bd cook` and instantiated
  with `bd mol pour --var` (docs/workflows/formulas.md). One command instead of
  a hand-wired epic, and it is reusable for the next PoC plan.
- **Wisps.** This project runs arena and swarm fan-outs that create beads
  "worthless the moment they close". Wisps are ephemeral molecules, `Ephemeral
  =true`, "excluded from federation push by default", purged with
  `bd purge --force` (docs/workflows/wisps.md). Keeps the permanent graph clean.
- **Gates and merge-slot.** `bd gate` "Manage async coordination gates" and
  `bd merge-slot` "merge-slot gates for serialized conflict resolution"
  (`bd --help`, Working With Issues). This repo runs parallel agents against one
  branch; that is exactly the serialization those exist for.
- **Session-close checks.** `bd preflight` (PR readiness), `bd orphans` (open
  issues referenced in commits), `bd stale`, `bd lint` (`bd --help`). CLAUDE.md
  "Session Completion" step 2 says "run quality gates" but names none of these.

Caveat on the CLAUDE.md beads block: it tells agents `.beads/issues.jsonl` is a
passive export, but that file does not exist in this repo (`ls .beads`). What is
tracked instead is `.beads/interactions.jsonl`, the `bd audit` append-only log
(`git ls-files .beads`). The instruction points at nothing.

## What to do next

1. Run `bd metrics off` in this repo. Default-on outbound telemetry is a bad fit
   for a project whose whole design argument is "no network surface".
2. Do not put beads, Dolt, or `bd remember` inside llm-wiki. Record it as a cut
   in docs/design/llm-wiki.md so it is not re-proposed.
3. Answer the user's question about memory directly: `bd remember` writes to the
   gitignored Dolt DB, not to a file. If the memory must survive a
   `rm -rf .beads`, it belongs in a doc, not in `bd`.
4. Superseded by the dotai beads scope decision (2026-08-29, PR #2): beads is
   tickets, dependency edges, the ready frontier, and the wayfinder map only.
   Not memory, session recovery, rules injection, workflow templates, or git.
   So no formulas, wisps, or gates; and the `bd prime` SessionStart hook in
   this repo's `.claude/settings.json` is the reintroduction risk that note
   names, already present here.
5. Fix the CLAUDE.md beads block: drop the `issues.jsonl` line, add `bd
   preflight` / `bd orphans` to the session-close gates.

## Not verified

- Whether metrics are ON at first install or were enabled on this machine. The
  current ON state was observed; the shipped default was not read out of
  `internal/metrics/metrics.go`, which resolves it through a config path.
- `bd doctor --check=conventions` could not be run; embedded mode rejects it.
