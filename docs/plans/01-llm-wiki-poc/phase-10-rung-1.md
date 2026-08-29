[overview](overview.md)

# Phase 10: acceptance rung 1, the recipe box

**Goal.** Ticket `.13`: a dozen hand-added recipes, no feeds, no identifiers,
no vectors, expressible in SCHEMA.md with zero substrate changes.

**Changes.** No module changes expected. `tests/fixtures/rung1/` with twelve
recipe sources, a filled `SCHEMA.md` and `SUMMARIZE.md`, and an empty
`[identifiers]` table. Edge cases kept from `.13`: two titles folding to one
slug (NFC vs NFD), re-adding the same file is `exists`, a source with nothing
extractable (a `failed` line, source kept). Dropped from `.13`: "generated
index" (the agent's per `.2`) and `page --touch` (verb deleted per `.9`).

**Verification.** Runtime is the phase: `init`, `ingest` twelve files,
`summarize` with the real model (approved count printed), `dedup`, `lint`
clean, then grep retrieval answers these three questions from the populated
`wiki/`: "which recipes use eggs", "which recipe has the shortest cook time",
"which two sources describe the same dish" (the story page). Any substrate
change reopens the ticket it touches instead of landing here.
