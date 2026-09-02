[overview](overview.md)

# Phase 14: concurrent writers

**Goal.** N separate `llmwiki` processes may run against one `.kb` without
losing or corrupting pages. Driven by a real need: several agents ingesting
into one shared kb at the same time.

**Problem.** There is no locking anywhere in the package. An audit found
eleven snapshot-then-write races. The five worst live in `dedup.py`, which
snapshots the whole wiki at `dedup.py:305`, then writes from that snapshot at
`dedup.py:242`. Two processes joining one story lose a member and leave a
dangling `story:` back-reference no lint check covers. Two processes finding
no candidate both create a story for the same occurrence. A self-lint
rollback at `dedup.py:271` can unlink a story another process just wrote.
Outside dedup: `summarize.py:169` checks a slug free and `summarize.py:221`
writes it, so two sources with the same model-chosen title destroy one page,
which then reads as `no summary` in `status`. The audit called that permanent
because the source sidecar already claims "exists". Traced and overstated: a
bare `llmwiki summarize` full sweep does recover it, since `_summary_index`
no longer finds the digest and the `exists()` check is live. The destroyed
page is real; "forever" is not, unless no full sweep ever runs again.
`vectors.py` opens sqlite with no WAL and catches bare
`sqlite3.OperationalError` at `vectors.py:115` and `:227` as "table not
created yet", so `database is locked` is swallowed, the planner re-embeds the
whole wiki as paid work, and `search` prints nothing and exits 0. That last
one is a defect today, single process, independent of concurrency.
`lint.py` globs `wiki/*.md` and then reads each page, and raises if a page
vanishes in between. An earlier change removed a second glob and was recorded
as closing this. It did not: `core.read_page_text` calls `Path.read_bytes`, so
the read is what raises, and five other readers share the hazard.

**Approach.** Layered, not one global mutex. A whole-run mutex closes every
hazard but serializes the paid model calls, which destroys the concurrency
the phase exists to buy. Per `principle-code-quality`, eliminate sharing
first and serialize only the true invariant.

- Slug claims become atomic. Reuse `sources._link_exclusive` at
  `sources.py:27-35`, already an `os.link` plus `FileExistsError` exclusive
  create, rather than hand-rolling a second exclusivity primitive.
- `vectors.py` enables WAL and narrows the bare `OperationalError` catches
  so a locked database is never read as an absent table. It also sets
  `busy_timeout` explicitly, which is documentation and not a behaviour
  change: `BUSY_TIMEOUT_MS` is 5000 and `sqlite3.connect`'s implicit default
  is also 5000 ms. An earlier note in this phase recorded that line as a fix.
  It is not one.
- Story placement takes an exclusive lock spanning snapshot to write, not
  just the write. Narrower than the snapshot reinstates the duplicate-story
  races verbatim. With `[models] dedup` unset, the recommended setting, that
  span makes no model call, so it serializes milliseconds of disk work while
  `summarize` and `embed` stay concurrent.
- `dedup --rebuild` takes the exclusive lock for the whole run. It unlinks
  every story page at `dedup.py:467`, and decision row 27 already accepts a
  window where summaries carry no story. That window is safe alone and unsafe
  beside any other writer.
- The lock is `fcntl.flock`, standard library, released by the kernel when
  the holder dies, so a crashed agent cannot wedge the kb and no stale-lock
  reaping is needed. Acquisition is bounded, not infinite: a blocked call
  that outlives an agent's tool timeout gets retried by that agent and piles
  on, so the wait ends in a clear busy exit instead.

**Changes.** `llmwiki/core.py` gains the lock helper. `dedup.py`,
`summarize.py`, `vectors.py`, `lint.py`. Tests under `tests/`.

**Data structures.** One lock file in the kb root. It is a new CLI write
target, so the ownership boundary in CLAUDE.md gains it explicitly.

**Verification.** The gate is a reproducer built and proven RED against
unmodified `llmwiki` BEFORE any fix lands, per `principle-prove-it-works`: a
guard never seen failing proves nothing. Each detected race asserts on
observable damage on disk, never on timing. The same harness must go green
after, with the hit rate reported over at least twenty consecutive runs. The
existing suite passes unchanged and stays hermetic under the dead-proxy run.
