# Concurrent readers and writers on one kb

Question, verbatim: "how are people fixing multiple readers/writers for
llm-wikis? Sub-agents are the default setup now. There must be a solution
already."

Read 2026-09-03 against HEAD `ba3ae6e`. Every external claim carries the url of
the primary source that owns it. Every internal claim carries a repo path and
line number.

## Short answer

There is no packaged solution for "several agents share one markdown wiki",
because nobody ships that as a product. What exists is four mechanisms that
shipping systems reuse, and llm-wiki already runs two of them. Content-addressed
naming with an exclusive `link()` claim, which is what `sources/` does
(`llmwiki/sources.py:27`) and what maildir does so that "an MUA can read and
delete messages while new mail is being delivered"
(https://cr.yp.to/proto/maildir.html). A single-writer lock, which is what
`kb_lock` does (`llmwiki/core.py:66`), what git's lockfile does with
`O_CREAT|O_EXCL` on `<filename>.lock`
(https://raw.githubusercontent.com/git/git/master/lockfile.h), and what SQLite
WAL enforces, "there can only be one writer at a time"
(https://www.sqlite.org/wal.html). Optimistic concurrency with a version check
and a retry, which is HTTP `If-Match` plus `412`
(https://www.rfc-editor.org/rfc/rfc9110.txt section 13.1.1), Delta Lake's
commit protocol
(https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md), and
SQLite's `BEGIN CONCURRENT`
(https://sqlite.org/src/doc/begin-concurrent/doc/begin_concurrent.md). And
CRDTs, which merge without any coordination at the cost of a binary format
(https://raw.githubusercontent.com/automerge/automerge/main/README.md). For
llm-wiki the gap is not the mechanism. It is that `kb_lock` is advisory
(https://man7.org/linux/man-pages/man2/flock.2.html) and the pages a subagent
writes by hand never call it, so the lock binds the CLI to itself and nothing
else.

## What llm-wiki already handles

| Concern | Mechanism | Where |
|---|---|---|
| Two writers mutating `wiki/` at once | Exclusive `fcntl.flock(LOCK_EX\|LOCK_NB)` on `<kb>/.lock`, polled at 50 ms, `KbBusy` after 30 s | `llmwiki/core.py:66` to `llmwiki/core.py:95`, constants at `llmwiki/core.py:33` to `llmwiki/core.py:35` |
| Stale lock after a crash | None needed, the kernel drops the lock when the fd closes | `llmwiki/core.py:68` docstring; man page confirms locks release "when all such file descriptors have been closed" (https://man7.org/linux/man-pages/man2/flock.2.html) |
| Two writers storing the same bytes | Content-addressed path plus `os.link` that returns False on `FileExistsError`, so the sidecar is the exclusive claim | `llmwiki/sources.py:27`, `llmwiki/sources.py:38` |
| A half-written page being read | `atomic_write_text` writes a temp file and renames | `llmwiki/core.py:249` |
| Summary page commit racing another writer | The read of `previous`, the path choice, and the commit are all inside one `kb_lock` span | `llmwiki/summarize.py:401` to `llmwiki/summarize.py:411` |
| Story placement racing another writer | `dedup.place` and `dedup.rebuild` require the lock for their whole span, documented as a hard precondition | `llmwiki/dedup.py:554`, `llmwiki/dedup.py:515`, `llmwiki/dedup.py:578` |
| Two processes writing the vector db | Deliberately not under `kb_lock`. SQLite `journal_mode=WAL` plus `busy_timeout=5000` | `llmwiki/ingest.py:91` comment "NO LOCK: vectors converge on their own", `llmwiki/vectors.py:79`, `llmwiki/vectors.py:80`, `llmwiki/vectors.py:40` |

That is a real design, not an accident, and most of it holds up against what
the shipping systems below do.

## Where it is actually exposed

**1. The lock is advisory, and the user's agent is not a caller.** flock
"places advisory locks only; given suitable permissions on a file, a process is
free to ignore the use of flock() and perform I/O on the file"
(https://man7.org/linux/man-pages/man2/flock.2.html). The project's own
ownership rule says everything under `wiki/` other than `summary` and `story`
pages belongs to the user's agent (`CLAUDE.md`, "Ownership boundary"). Those
writes go through the agent's editor tool, not through `llmwiki`, so they never
touch `<kb>/.lock`. Subagent A hand-editing `wiki/index.md` while subagent B
runs `llmwiki dedup` is completely unserialized today. This is the largest gap
and it is the one the user's premise creates.

**2. `dedup` holds the whole-kb lock across a model call.** The code says so
itself, and prints a warning: "this run holds the kb lock across one model call
per target; a second writer gets KbBusy after 30s"
(`llmwiki/dedup.py:563` to `llmwiki/dedup.py:565`). With N subagents ingesting
in parallel, one slow endpoint turns into `KbBusy` for everyone else. The lock
is also whole-kb, so two subagents ingesting two unrelated sources serialize
even though they touch disjoint pages.

**3. Readers take no lock at all.** `cmd_search`, `cmd_page`, `cmd_status` and
`cmd_lint` never call `kb_lock` (`llmwiki/cli.py:125`, `:166`, `:275`, `:310`;
`kb_lock` appears only in `ingest.py`, `summarize.py`, `dedup.py`). Per-file
atomicity from `atomic_write_text` means a reader never sees half a page, but
it says nothing across files. A reader can observe a summary page that already
carries `story:` before the story page exists, because `dedup` writes both
inside one lock span the reader is not participating in. SQLite WAL is the
contrast: it gives readers a consistent snapshot, "readers do not block writers
and a writer does not block readers" (https://www.sqlite.org/wal.html).

**4. `log.md` appends are outside the lock.** `append_log_entry` opens the file
in `"a"` mode and writes one line (`llmwiki/core.py:261` to
`llmwiki/core.py:269`), and `summarize` calls it after its `kb_lock` block
closes (`llmwiki/summarize.py:416`). Unsourced: I could not settle from a
primary source whether CPython's buffered text write of a single short line
always reaches the kernel as one `write(2)`, which is what would make the
append atomic. Treat interleaved log lines as possible, not proven.

**5. WAL rules out a network filesystem.** "All processes using a database must
be on the same host computer; WAL does not work over a network filesystem"
(https://www.sqlite.org/wal.html). `vectors/` is therefore host-local by
construction. flock is less strict, since "Since Linux 2.6.12, NFS clients
support flock() locks by emulating them as fcntl(2) byte-range locks on the
entire file" (https://man7.org/linux/man-pages/man2/flock.2.html), so the two
layers disagree about whether a shared kb over NFS is legal. Nothing in the
repo states which one wins.

**6. The service is single-replica by design.** `_build_auth_state` builds the
token store and both limiters once per process and warns that a second instance
is "exactly the split-brain a single deployment file exists to prevent"
(`llmwiki_service/__main__.py:110` to `llmwiki_service/__main__.py:127`). Remote
concurrency is bounded by that, not by `kb_lock`.

## What shipping systems actually do

Each row names the mechanism and quotes the primary source that owns the claim.

**Advisory whole-resource lock.** Linux `flock(2)`. Advisory only, tied to the
open file description so `fork` and `dup` share the lock, released when every
descriptor closes. https://man7.org/linux/man-pages/man2/flock.2.html

**Lock file created with `O_CREAT|O_EXCL`, then rename.** git. "When we want to
change a file, we create a lockfile `<filename>.lock`, write the new file
contents into it, and then rename the lockfile to its final destination
`<filename>`", and "We create the `<filename>.lock` file with `O_CREAT|O_EXCL`
so that we can notice and fail if somebody else has already locked the file".
https://raw.githubusercontent.com/git/git/master/lockfile.h
This is a lock and an atomic publish in one step, and unlike flock it needs no
kernel lock manager.

**Single writer plus non-blocking readers (WAL).** SQLite. "since there is only
one WAL file, there can only be one writer at a time" and "writers do nothing
that would interfere with the actions of readers, writers and readers can run
at the same time". https://www.sqlite.org/wal.html

**Optimistic concurrency with a conflict abort.** SQLite `BEGIN CONCURRENT`.
"The system uses optimistic page-level-locking to prevent conflicting
concurrent transactions from being committed"; on commit it checks whether any
page the transaction read has been modified, and if so returns
`SQLITE_BUSY_SNAPSHOT` and "all the client can do is ROLLBACK the transaction".
The same doc notes commit itself is still serialized: "At most one writer may
hold this lock at any one time".
https://sqlite.org/src/doc/begin-concurrent/doc/begin_concurrent.md
Note this is a branch document, not mainline SQLite behaviour.

**Optimistic concurrency over HTTP.** RFC 9110 section 13.1.1. `If-Match` "is
most often used with state-changing methods (e.g., POST, PUT, DELETE) to
prevent accidental overwrites when multiple user agents might be acting in
parallel on the same resource (i.e., to prevent the 'lost update' problem)",
and a server "MAY indicate that the conditional request failed by responding
with a 412 (Precondition Failed) status code".
https://www.rfc-editor.org/rfc/rfc9110.txt

**MVCC over an append-only commit log in a plain directory.** Delta Lake.
"Delta's transactions are implemented using multi-version concurrency control
(MVCC)"; writers "optimistically write out new data files", then "commit,
creating the latest atomic version of the table by adding a new entry to the
log". Winning the commit can be done by "atomically publish[ing] it to the
filesystem directly, relying on PUT-if-absent primitives". Old versions are
reclaimed by a separate `vacuum` step, which is the compaction half of the
pattern. https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md

**Unique filename per writer, so writers never collide.** maildir. "Two words:
no locks. An MUA can read and delete messages while new mail is being
delivered: each message is stored in a separate file with a unique name, so it
isn't affected by operations on other messages." Delivery writes into `tmp`
first and then moves into `new`, and "The maildir format is reliable even over
NFS". The unique name is built from time, hostname, and a per-delivery
identifier, so two hosts sharing the directory still cannot collide.
https://cr.yp.to/proto/maildir.html

**CRDT merge with no coordination.** Automerge. A library of "fast
implementations of several different CRDTs, a compact compression format for
these CRDTs, and a sync protocol", aimed at letting application developers
"avoid thinking about hard distributed computing problems".
https://raw.githubusercontent.com/automerge/automerge/main/README.md
The compact binary format is the cost, and it is stated in the same paragraph.

**Agent memory stores: no mechanism at all.** Anthropic's memory tool is the
closest first-party comparable, and it is worth naming what the document does
not say. "The memory tool operates client-side: Claude requests file
operations, and your application executes them. You control where and how the
data is stored through your own infrastructure." The reference handler is a
plain directory of files, and the SDKs ship `BetaLocalFilesystemMemoryTool`
against a local path. Concurrency, locking, multiple simultaneous agents, and
subagents are never mentioned anywhere in that document, including its Security
considerations section, which covers sensitive data, file size, expiration, and
path traversal, and stops there.
https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
This is the direct answer to "there must be a solution already". At the
first-party layer there is not one. The store is handed to you as a directory
and the concurrency question is handed to you with it.

## Poor fits, and why

**CRDTs (Automerge, Yjs).** They buy conflict-free merge of concurrent edits to
the same document. llm-wiki's contested writes are not concurrent edits to one
paragraph, they are two agents deciding independently that a source deserves a
page, which is a naming and identity conflict a text CRDT would happily merge
into a mess. The format cost is disqualifying on its own: `wiki/` is markdown
that the user's agent greps and edits by hand, and Automerge's value proposition
is a compressed binary format
(https://raw.githubusercontent.com/automerge/automerge/main/README.md).

**Delta-style transaction log with a catalog.** It solves multi-writer commits
over object stores with no rename primitive
(https://raw.githubusercontent.com/delta-io/delta/master/PROTOCOL.md). llm-wiki
is on a local POSIX filesystem where `link` and `rename` are already atomic and
already used (`llmwiki/sources.py:27`, `llmwiki/core.py:249`). Adding a version
log plus checkpointing plus vacuum buys nothing here and adds two failure modes.

**Promoting SQLite to the store of record.** WAL would give real snapshot reads
(https://www.sqlite.org/wal.html), but it means the wiki stops being files an
agent can grep, which is the design premise. It also inherits "WAL does not work
over a network filesystem" for the whole kb rather than just `vectors/`. A prior
research note in this directory already rejected a database of record on the
same grounds (`docs/research/beads-fit.md`).

**`BEGIN CONCURRENT`.** Not mainline SQLite, and the contended resource in this
project is markdown pages, not vector rows. Even inside its own doc, commits
still serialize
(https://sqlite.org/src/doc/begin-concurrent/doc/begin_concurrent.md).

**A long-lived single-writer daemon.** It is the cleanest textbook answer, and
it contradicts the local mode: an agent shells `llmwiki` and gets files on disk.
A daemon adds start, stop, health, and a socket, and the remote service already
exists for anyone who wants a process in the middle
(`llmwiki_service/__main__.py`). Inference, not sourced.

**Multi-replica service.** Blocked until `FailureLimiter` and `SearchLimiter`
move out of process memory (`llmwiki_service/__main__.py:110` to `:127`). The
split-brain warning in that docstring is the whole argument.

## Shortlist for llm-wiki

Ranked by value over cost. Each item says whether it rests on evidence I found
or on my own reasoning.

**1. Stop holding `kb_lock` across a model call in `dedup`.** Call the model
outside the lock, then re-read and re-validate inside it, dropping the result if
the world moved. Cost: `dedup.place` currently loads the whole wiki and writes
from that load (`llmwiki/dedup.py:554`), so this is a restructure, not a
one-liner, and it needs a real subprocess test since in-process reproducers do
not work against a process-level flock. **Evidence**: the code prints its own
warning about this (`llmwiki/dedup.py:563`), and SQLite's own guidance is that
serializing the commit is fine while serializing the work is not
(https://sqlite.org/src/doc/begin-concurrent/doc/begin_concurrent.md). The
specific fix shape is my inference.

**2. Give the user's agent a locked way to write.** A `llmwiki write <page>`
verb, or a `llmwiki claim` that holds the lock while the agent edits, so
hand-written pages stop bypassing the lock entirely. Cost: one new verb, one new
line in `SCHEMA.md`, and it only works if the agent is told to use it, since
flock cannot force anything. **Evidence**: flock is advisory and a process "is
free to ignore" it (https://man7.org/linux/man-pages/man2/flock.2.html), and the
ownership boundary in `CLAUDE.md` puts most `wiki/` writes outside the CLI. The
verb shape is my inference.

**3. Make story-page naming collision-proof rather than lock-proof.** Derive the
story page's filename from something a second writer would derive identically,
or from something guaranteed unique per writer, so two agents racing produce
either the same path (one `link` wins, as `sources.py:27` already does) or two
distinct paths that a later pass joins. Cost: changes page naming, and
`dedup.rebuild` already exists as the repair path for exactly this
(`llmwiki/dedup.py:508`). **Evidence**: maildir builds its whole no-lock
guarantee on unique names plus tmp-then-move
(https://cr.yp.to/proto/maildir.html), and llm-wiki already runs this pattern
for `sources/`. Applying it to story pages is my inference.

**4. Narrow the lock from whole-kb to per-page.** git's `<filename>.lock` with
`O_CREAT|O_EXCL` is the exact precedent, and it needs no kernel lock manager
(https://raw.githubusercontent.com/git/git/master/lockfile.h). Cost: real. A
per-page lock does not cover `dedup`, which reads the whole wiki and needs a
global view, so you end up with two lock scopes and a rule about which one
applies where. I would not do this before item 1, because item 1 removes most of
the contention that makes the whole-kb scope hurt. **Inference.**

**5. `If-Match` on the remote service's write endpoints.** If the service ever
grows a write path, put an ETag on the page and require `If-Match`, answering
`412` on a mismatch. Cost: low, and it is the standard answer.
**Evidence**: RFC 9110 section 13.1.1 defines it precisely for the lost-update
problem (https://www.rfc-editor.org/rfc/rfc9110.txt). Whether the service needs
a write path at all is a separate question.

**6. Leave `log.md` alone.** The exposure is interleaved lines in an append-only
log nothing parses. Fixing it means either taking the lock (which widens every
lock span) or writing one file per entry, maildir-style. Neither is worth it
until someone shows a corrupted log. **Inference.**

Not on the list: any new dependency. Nothing above needs one. `sqlite-vec` is
already the only declared dependency and it is not implicated in any of these
gaps.

## Open questions

- Whether CPython's buffered text write in `append_log_entry`
  (`llmwiki/core.py:268`) reaches the kernel as a single `write(2)` under
  `O_APPEND`. I could not settle this from a primary source in the timebox, so
  point 4 above is labelled unsourced.
- Whether Claude Code subagents actually issue tool calls concurrently against
  one working directory. The subagents documentation page at
  `https://platform.claude.com/docs/en/claude-code/sub-agents` is a JavaScript
  application and returned no extractable prose to `curl`. The premise that they
  do is taken from the user, not sourced here.
- What Letta, mem0, and similar agent memory frameworks do about concurrent
  writers. Not checked. Their source is public and this is the obvious next
  thing to read, since they are the closest functional comparables to llm-wiki.
- Whether flock behaves correctly on this machine's actual filesystem. The repo
  root is reached through a symlink (`/home/nicole/Projects/agent-kb` to
  `/var/home/nicole/Projects/llm-wiki`) and the host is running an overlay
  layout. Not tested, and testing it needs real subprocesses.
- Whether a shared kb over a network filesystem is supposed to be legal. flock
  is emulated over NFS
  (https://man7.org/linux/man-pages/man2/flock.2.html) but SQLite WAL refuses
  network filesystems outright (https://www.sqlite.org/wal.html). Nothing in the
  repo picks a side.

---

# Second pass: Karpathy, agent frameworks, and the subagent premise

Appended 2026-09-03, same HEAD `ba3ae6e`. Prompted by the pushback "I refuse to
believe that someone like karpathy doesn't have a solution for sub-agents."
The first pass never hunted the named upstream artifact, and three of its own
open questions were in scope this time.

## The verdict did not move, and one premise flipped

The pushback does not survive contact with the artifacts. Karpathy has no
published solution for concurrent or multi-agent writes, and I could not locate
a published `llm-wiki` artifact by him at all. The rest of the field is worse
than llm-wiki, not better: the two agent memory frameworks whose source I read
protect their store with an in-process `threading.Lock`, which does nothing
across processes, where `kb_lock` uses a kernel lock that works across
processes (`llmwiki/core.py:66`). On this specific axis llm-wiki is ahead of
mem0 and CrewAI, not behind them.

What did get settled in the user's favour is the other premise. "Sub-agents are
the default setup now" was unsourced in the first pass and is now sourced:
subagents do run simultaneously and the harness caps them at 20. So the
exposure is real even though the solution is not out there.

## Target 1: the Karpathy artifact

**The citation in this repo carries no url.** `docs/design/llm-wiki.md:16` says
"Three layers, after Karpathy's llm-wiki" and `CLAUDE.md:43` says "Karpathy's
three layers and nothing else". Neither names a url, a repo, or a post.
`docs/plans/01-llm-wiki-poc/overview.md:7` records the instruction as "stick as
close to karpathy's model to keep it simple". I could not find the artifact
those lines point at. This is a real gap in the repo's own provenance, not just
in my search.

**No such repo exists under his account.** I listed all repositories at
https://api.github.com/users/karpathy/repos (100 per page, sorted by updated).
There is no `llm-wiki`, and nothing memory-shaped or wiki-shaped. The list is
training code, teaching material, and small tools: `nanochat`, `nanoGPT`,
`llm.c`, `micrograd`, `minGPT`, `minbpe`, `makemore`, `llama2.c`, `rustbpe`,
`build-nanogpt`, `nn-zero-to-hero`, `LLM101n`, `ng-video-lecture`,
`autoresearch`, `llm-council`, `reader3`, `rendergit`, `arxiv-sanity-lite`,
`arxiv-sanity-preserver`, `researchlei`, `researchpooler`, `scholaroctopus`,
`hn-time-capsule`, `ulogme`, `calorie`, `jobs`, `cryptos`, `sqlitedict`,
`convnetjs`, `reinforcejs`, `recurrentjs`, `neuraltalk`, `neuraltalk2`,
`char-rnn`, `scriptsbots`, `lecun1989-repro`, `deep-vector-quantization`,
`pytorch-normalizing-flows`, `randomfun`, `karpathy.github.io`.

**The repos say nothing about concurrency.** I read the READMEs at
https://raw.githubusercontent.com/karpathy/nanochat/master/README.md,
https://raw.githubusercontent.com/karpathy/llm.c/master/README.md,
https://raw.githubusercontent.com/karpathy/nanoGPT/master/README.md, and
https://raw.githubusercontent.com/karpathy/micrograd/master/README.md and
grepped each for concurrency, locking, parallel writes, multi-agent, and
subagent. Every hit was a false positive: `uv.lock` in a directory tree listing,
"earlocks" in a Shakespeare sample, and hyperparameter prose. Zero substantive
matches. These are single-operator training tools with no shared mutable store,
so there is nothing there to find.

**The blog says nothing either.** https://karpathy.bearblog.dev/blog/ lists 13
posts: "Sequoia Ascent 2026 summary", "2025 LLM Year in Review", "Chemical
hygiene", "Auto-grading decade-old Hacker News discussions with hindsight",
"The space of minds", "Verifiability", "Animals vs Ghosts", "Vibe coding
MenuGen", "Power to the people: How LLMs flip the script on technology
diffusion", "Finding the Best Sleep Tracker", "The append-and-review note",
"Digital hygiene", "I love calculator". None is about agent memory, a wiki, or
subagents.

**The closest artifact is a human note-taking habit, and it is single-writer.**
"The append-and-review note" (https://karpathy.bearblog.dev/the-append-and-review-note/)
describes one text file in Apple Notes: "Any time any idea or any todo or
anything else comes to mind, I append it to the note on top, simply as text",
then "As things get added to the top, everything else starts to sink towards
the bottom, almost as if under gravity", and on review "If I find anything that
deserves to not leave my attention, I rescue it towards the top by simply copy
pasting". It is not even strictly append-only: "Sometimes I merge, process,
group or modify notes when they seem related." Concurrency, multiple writers,
and agents are never mentioned. It is a personal system, and the "merge" is one
person consolidating their own notes.

So: nothing. These are single-operator tools. The three-layer shape llm-wiki
borrowed was never load-bearing for concurrency, because in its original setting
there was exactly one writer.

## Target 2: the gaps I admitted

**Claude Code subagents: premise sourced, concurrency unaddressed.**
https://code.claude.com/docs/en/sub-agents states "For independent
investigations, spawn multiple subagents to work simultaneously" and "Each
subagent runs in its own context window with a custom system prompt, specific
tool access, and independent permissions." The only concurrency constraint on
the page is a spawn cap: "By default, when 20 subagents are running in a
session, spawning another with the Agent tool fails with `Concurrent subagent
limit reached`." Multiple subagents writing the same files, a shared working
directory, locking, and race conditions are never mentioned. The premise is
confirmed; the coordination story is absent, exactly as in the memory tool doc.

**mem0: an in-process `threading.Lock` around one shared SQLite connection.**
https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/memory/storage.py opens
`sqlite3.connect(self.db_path, check_same_thread=False)` at line 14, creates
`self._lock = threading.Lock()` at line 15, and wraps every mutating method in
`with self._lock:` around an explicit `BEGIN` / `COMMIT` / `ROLLBACK` (lines 26,
103, 129, 163 and the commits and rollbacks that follow each). That is thread
safety inside one Python process and nothing more. Two mem0 processes on one
database file have no mutual exclusion beyond whatever SQLite's own locking
gives them, and the file does not set WAL or a busy timeout. llm-wiki's
`vectors.py` does set both (`llmwiki/vectors.py:79`, `llmwiki/vectors.py:80`).

**CrewAI: a single-writer thread, also in-process.**
https://raw.githubusercontent.com/crewAIInc/crewAI/main/lib/crewai/src/crewai/memory/unified_memory.py
declares `_save_pool` as a `ThreadPoolExecutor(max_workers=1,
thread_name_prefix="memory-save")` at lines 165 to 167, plus
`_pending_lock: threading.Lock` at line 171 and `_reset_lock: threading.RLock`
at line 172. Saves are funneled through that one worker: "Submit a save
operation to the background thread pool" (line 298). This is the single-writer
pattern applied correctly and scoped to one process. Nothing coordinates two
CrewAI processes.

**Letta: unsettled.** I could not read the source in the timebox.
`https://raw.githubusercontent.com/letta-ai/letta/main/letta/orm/sqlalchemy_base.py`,
`.../letta/server/db.py`, and `.../letta/orm/base.py` all returned a 14-byte
"404: Not Found", the GitHub contents API for `letta/orm` returned nothing, and
authenticated code search for `lock` in that repo returned only `AI_POLICY.md`,
`PRIVACY.md`, and a workflow file. Either the paths moved or the search index
did not cover it. Marked as still open; do not read anything into the absence.

## Target 3: the broader category

**MCP has no resource mutation primitive at all, so it has no concurrency
story.** The resources section of the specification
(https://modelcontextprotocol.io/specification/2025-06-18/server/resources)
defines `resources/list`, `resources/read`, `resources/templates/list`,
`resources/subscribe`, and the `notifications/resources/updated` and
`notifications/resources/list_changed` notifications. There is no write, no
put, no patch, and no delete. Resource metadata carries `uri`, `name`, `title`,
`description`, `mimeType`, and `size`, plus optional annotations `audience`,
`priority`, and `lastModified`. There is no ETag, no version, and no
precondition. Its Security Considerations section lists four items, all about
URI validation and access control, and concurrency is not among them. So MCP
cannot be the place a shared-write protocol comes from: the read side is
specified and the write side does not exist. That weakens shortlist item 5 as a
standards-alignment argument, though `If-Match` on the project's own service
remains valid on its own terms (https://www.rfc-editor.org/rfc/rfc9110.txt).

**LangGraph refuses concurrent writes rather than merging them, unless you
declare a reducer.** `LastValue` is documented as "Stores the last value
received, can receive at most one value per step", and its `update` raises when
handed more than one value
(https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/langgraph/channels/last_value.py,
lines 21 and 55 onward). The exception is `InvalidUpdateError`, whose docstring
points at the troubleshooting guide `INVALID_CONCURRENT_GRAPH_UPDATE`
(https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/langgraph/errors.py,
lines 90 to 97). This is the most interesting design in the whole second pass.
Two parallel branches writing the same key is treated as a programming error
that must surface, not as a merge to be resolved silently. Aggregation is
opt-in per key through a channel that declares how to combine (the `binop`
channel), so the schema, not the runtime, decides which keys tolerate
concurrent writes.

**AutoGen: not settled.** Code search surfaced only docs and an assistant-agent
module, nothing that reads as a memory-store concurrency mechanism, and I did
not read the source. Marked open rather than answered.

## What this changes in the shortlist

The ranking above stands. Two amendments.

**New candidate, sitting between items 2 and 3: fail loudly on a detected
race.** When two writers would place a story for the same subject, refuse and
say so, instead of letting one silently win. LangGraph treats exactly this as an
error class with a named troubleshooting code
(https://raw.githubusercontent.com/langchain-ai/langgraph/main/libs/langgraph/langgraph/errors.py:90).
llm-wiki already has the two halves this needs: `KbBusy` as the refuse-and-exit
convention (`llmwiki/core.py:58`) and `dedup.rebuild` as the repair path
(`llmwiki/dedup.py:508`). The design is my inference; the precedent for
preferring a loud refusal over a silent merge is sourced.

**Item 5 is weaker than I implied.** MCP defines no resource write, so
`If-Match` on the service is not aligning with an emerging agent standard, it
is just correct HTTP. Keep it, drop the standards-alignment framing.

Nothing found in this pass argues for adopting a dependency, and nothing argues
for a rewrite. The honest summary is that llm-wiki's process-level `flock` is
already stronger than what the shipping agent memory frameworks use, and the
remaining exposure is the one named in the first pass: the lock is advisory and
the user's own agent does not call it.

## Still unsourced after this pass

- The `llm-wiki` artifact by Karpathy that `docs/design/llm-wiki.md:16` and
  `CLAUDE.md:43` both cite. Not found in his GitHub repositories or his blog. If
  it exists it is somewhere I did not look, most likely a post on a platform
  that resists fetching. The repo should record the url when someone has it.
- Letta's concurrency mechanism. Three raw paths 404, contents API empty,
  code search unhelpful. Open.
- AutoGen memory backends. Not read.
- Everything left open at the end of the first pass except the subagent premise,
  which is now sourced, stays open: the `O_APPEND` write atomicity question,
  flock behaviour on this machine's filesystem, and whether a kb over a network
  filesystem is meant to be legal.
