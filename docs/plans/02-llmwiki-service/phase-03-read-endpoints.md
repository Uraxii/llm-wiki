[overview](overview.md)

# Phase 3: the four read endpoints

**Goal.** An agent on another machine can search a remote wiki, read a page
back, list what is there, and learn how to interpret any of it, using the same
ranking and the same parser the local CLI uses.

**Problem.** Phases 1 and 2 built a door and nothing behind it. This phase puts
the read half of a kb behind that door, under one hard constraint that shapes
every decision below: the service imports `llmwiki` and never forks it. Two
rankers drift, and the failure mode is a remote that returns a different top
hit than the same query run locally on the same corpus.

The four traps this phase is written to avoid: a search route reimplements
ranking because the CLI's function prints instead of returning, a page route
turns a caller-supplied filename into a path outside the wiki, an open read
endpoint spends the operator's money on every request from anyone, and a
listing route walks the whole wiki on every call with no bound.

## The routes

All four hang off the `[kbs]` table in the deployment file, defined in
[phase 2](phase-02-tls.md). The table name is the name in the URL. There is no
multi-kb route: one request names one kb.

| Route | Returns |
|---|---|
| `GET /kb/<name>/search?q=&n=&kind=` | ranked hits: score, page name, title, updated, size |
| `GET /kb/<name>/page/<page>` | one page's raw bytes, frontmatter included |
| `GET /kb/<name>/list?kind=&after=&n=` | one page of a listing: name, kind, title, updated, size |
| `GET /kb/<name>/schema` | that kb's `SCHEMA.md`, raw bytes |

**Check the token before the kb name.** An implementer who looks up `[kbs]`
first hands an unauthenticated caller a kb enumerator: an unknown name answers
404 and a known one answers 401, and the difference is the list of kbs. Under
`[access] read = "token"` the token check runs first, so a caller with no
valid token gets the same refusal whatever kb name it wrote, and learns
nothing. A caller holding a valid reader token reaches every kb in `[kbs]`:
there is no per-kb grant in this design, and none is added here. A kb name
absent from `[kbs]` returns 404 with a body naming no kb.

The refusal is 401 with `{"error": "unauthorized"}`, identically for an absent
token, a malformed one, an unknown id, a wrong secret, a revoked token, and a
role below `reader`. One status and one body, so the caller cannot separate
"you are nobody" from "you are somebody with no access here".

**Encoding.** Every response body is JSON, except `page` and `schema`, which
return bytes. Errors are JSON with one shape everywhere, `{"error": "<code>"}`,
where the code is a fixed string from a closed set. Never put a path, a kb name,
an exception message, or a count of anything into an error body. The one
exception is the 503 from `search`, which carries the number of pages missing a
vector, because an operator cannot act on it otherwise.

**Content types.** `page` and `schema` return `text/markdown`, with no
`charset` parameter, and not `application/octet-stream`, which browsers
download instead of showing. The omission is load bearing. Both routes serve
the file's bytes, and `page` reads them with `Path.read_bytes` precisely so a
byte that is not valid UTF-8 survives the trip. A `charset=utf-8` parameter
would be a claim about those bytes that the route does not check and cannot
make. A client that wants text decodes and owns the failure.

**Response shapes.** Named here because two independent implementers, one
per side of the wire, cannot agree on a table of column headings.

```json
// GET /kb/<name>/search
{"hits": [
  {"score": 0.6142, "name": "cross-site-request-forgery.md",
   "title": "Cross-site request forgery", "updated": "2026-08-31T09:14:02Z",
   "size": 4821}
]}

// GET /kb/<name>/list
{"pages": [
  {"name": "index.md", "kind": "topic", "title": "Index",
   "updated": "2026-08-31T09:14:02Z", "size": 311}
], "next": "index.md"}

// the 503 from search, the one error body carrying a number
{"error": "index_stale", "missing": 37}
```

`hits` is ordered best first, and that order is part of the contract: a
client's only honest cross-wiki key is a position within one wiki's own list,
which it can only read off the order. `hits` holds at most `n` entries and may
hold fewer: `search` skips a row whose page has been unlinked since the last
embed, so a caller that asserts an exact length is wrong. `score` is the raw
float, unrounded; the CLI's 2dp formatting is a printer's business. Every
field is present on every entry, `null` only where the sections below say so.

`search` carries no `next` and no total, and that asymmetry with `list` is
deliberate. `n` goes straight into vec0's `k`, so the route already returns
the whole answer it was asked for. A second page would need a cursor over a
distance ordering that changes on every embed, and a total would be a count of
the whole corpus, which is what `list` is for.

## `schema` is what makes a remote usable at all

`SCHEMA.md` is the contract the user's agent reads, and each kb writes its own
(`cli.py:64` writes it at `init`). The CLI never parses it, so serving it is a
byte copy of `kb.root / "SCHEMA.md"`.

It looks like the smallest route and it is the one that matters most. Every
other route returns pages whose fields, kinds, and identifier vocabulary are
domain specific. Without the schema, an agent gets a page of a kind it does not
know and cannot tell an `identifiers` list from a domain field it must ignore.
A remote that serves pages and not its schema is a remote an agent can quote
and cannot use.

Serve it verbatim. Do not summarize it, do not translate it to JSON, and do not
fall back to a generic schema when the file is missing.

A kb whose `SCHEMA.md` is absent returns 404. That is the same status an
unknown kb name returns, and the collision is deliberate: distinguishing them
would restore the enumeration oracle the previous section closes. An operator
who needs the difference reads the service log, which records it. A client
cannot.

## Ranking moves into `llmwiki`, and this is the only change there

`vectors.search` (`vectors.py:350`) is a CLI verb. It prints tab separated
lines and returns an exit code: 0 when the search ran, including when it
matched nothing, 1 for stale vectors, and 2 for a model failure. A service
cannot call it, and reimplementing it inside the service breaks the constraint
this phase exists under.

Split it. Add a function to `llmwiki/vectors.py` that takes a `Kb` and returns
hits instead of printing them, then leave `search` as a thin printer over that
function. The CLI keeps its exact current stdout and stderr, byte for byte, and
the service gets the same ranking by construction rather than by review.

```python
class Hit(NamedTuple):
    score: float          # raw, not rounded; `search` formats to 2dp
    name: str             # the page filename, e.g. "cross-site-request-forgery.md"
    title: str            # already through core.flatten, as `search` prints it
    updated: str          # TIMESTAMP_FORMAT, from st_mtime, UTC
    size: int             # st_size


class Ranking(NamedTuple):
    hits: list[Hit]
    unsummarized: int     # sources with no summary page; `search` warns, the service ignores


def rank(kb: Kb, query: str, n: int = TOP_K, kind: str | None = None) -> Ranking:
    """At most `n` pages nearest `query`, best first. Fewer when a row
    survives an embed whose page has since been unlinked; those are
    skipped, exactly as `search` skips them today. Makes ONE paid
    embedding call, for the query, and only after the staleness check
    passes. Raises StaleVectors, carrying `missing`, the number of
    pages lacking a current vector, because the route's 503 body has to
    report it; NoEmbedModel when [models] embed is unset; and ModelError
    from the endpoint. ONE _plan walk and ONE connection, per _plan's
    own contract at vectors.py:180."""
```

Four rules on that split, each one a mistake a later reader would otherwise
make:

- **`rank` returns `Ranking`, not a bare list.** `search`'s "sources without a
  summary" warning needs the `seen` set `_plan` returns, bound inside
  `vectors.search` (`vectors.py:356`).
  `_plan`'s docstring pins one walk of `wiki/*.md` for the whole module and
  says so in as many words (`vectors.py:180-187`). A bare `list[Hit]` would
  force `search` to walk a second time to rebuild that count. `unsummarized`
  rides back on the return value instead.
- **`rank` raises where `search` returns an exit code.** Exit codes are a CLI
  concept. A function that returns 1 for "stale" makes every caller
  reimplement the mapping. `search` catches and prints. The service catches and
  answers with a status code.
- **`rank` checks staleness before it embeds.** `search` gets this order right
  today: the refusal at `vectors.py:363` precedes the `embed` call at
  `vectors.py:372`. Reversing it during the split costs a paid call on every
  503, and no test sees it unless one reads the endpoint's call count back.
- **`rank` does not print.** The stale warning and the missing-summary line
  stay in `search`, where a terminal is reading them.

`Hit` and `StaleVectors` are new names; neither exists in `llmwiki` today.
`vectors.neighbours` keeps its current `list[tuple[float, Path]]`
(`vectors.py:393`) and is not converted: it answers a different question,
page-to-page sameness rather than query-to-page relevance, and the two are
measured differently.

`page` and `list` need no such split, because `core.parse_frontmatter` and
`core.flatten` are already plain functions.

**`page` reads bytes, not text.** Do not use `core.read_page_text`. It decodes
with `errors="replace"` by design (`core.py:223-227`), so a page holding one
byte that is not valid UTF-8 comes back with U+FFFD where that byte was. That
is right for a lint run and wrong for a route whose contract is the file. Call
`Path.read_bytes`.

## Similarity scores are model-relative, so nothing merges them

A later phase will let one agent ask several wikis one question. The obvious
implementation sorts every hit together and returns the top ten. That
implementation is wrong, and it is wrong in the way that looks right, so pin
the rule here, where the scores are, rather than where the fan-out is.

Similarity scores are comparable only within one embedding model's
distribution. `NEIGHBOUR_FLOOR = 0.35` (`vectors.py:46`) is the standing proof.
It is calibrated to one model, measured over all 1653 page pairs of the arena
corpus, where cross-domain pairs top out at 0.3123 and within-domain pairs run
a p50 of 0.3025. Change the model and every number in that comment moves.
`vectors._model_id` reads the embedding model from each kb's own `config.toml`
(`vectors.py:67-72`), so two kbs on one service can already name different
models, and their scores already mean different things.

**The rule this pins for phase 4.** The CLI client fans out over the single-kb
routes and keeps the answers apart: one ranked list per wiki, labelled with the
wiki it came from. It must not concatenate and re-sort. A caller who wants one
list writes the merge and owns the consequence. Merged cross-wiki ranking is
out of scope in the [overview](overview.md), and this is the reason.

Phase 3 ships no federated route and no federated response shape. The four
routes above are the whole surface.

## A page name is a filename, and the caller supplies it

`GET /kb/<name>/page/<page>` takes a name from the network and turns it into a
path. Get this wrong and the route reads any file the service user can read.

Resolve the candidate path. Require that its parent equals `kb.wiki.resolve()`.
Require that its suffix is `.md`. Reject anything else with 404. Never repair a
rejected name.

Resolve both sides. `Kb.wiki` is `root / "wiki"` unresolved (`core.py:52`), and
phase 2's `[kbs.homelab] path = "/srv/kb/homelab"` makes a symlinked or
bind-mounted root the normal deployment. Comparing a resolved candidate against
an unresolved `kb.wiki` 404s every legitimate page on such a deployment.

Compare the resolved parent for equality, not containment. `is_relative_to`
passes a page in a subdirectory of `wiki/`. Wiki pages are flat by CLI
convention (`lint.py:164` globs `wiki/*.md`, not recursively), but the
ownership boundary hands `wiki/` to the user's agent, which is free to create a
subdirectory. Equality is what makes flatness a property of this route rather
than a hope about the directory.

Three inputs an implementer must handle and a naive route does not: a
percent-encoded separator, which the router may decode after the check; an
embedded NUL, on which `Path.resolve` raises `ValueError` rather than returning
a path, so an uncaught one answers 500 instead of 404; and a name that reaches
the check through a symlink inside `wiki/`, which is why the check reads the
resolved path and not the string.

The router's path parameter is `{page}`, never `{page:path}`. A path converter
accepts a separator before any code of ours sees it.

## Reads are gated by the deployment, and a search read costs money

`[access] read` decides all four routes. Under `read = "token"` each requires a
valid token of role `reader` or above. Under `read = "open"` each answers
without one. That is phase 1's policy, and this phase adds no route level
exception to it.

`search` is different from the other three in a way the policy does not
capture. `page`, `list`, and `schema` read files. `search` calls `embed` on the
caller's query text, which is a paid call to the model endpoint on every
request. Under `read = "open"`, an unauthenticated caller can spend the
operator's budget in a loop, and the service has no token to attribute the
spend to or to revoke.

Answer it with a per-caller rate limit on `search`, in the service.
`[limits] search_per_minute` is defined in [phase 2](phase-02-tls.md). It
exists because an open read route that embeds spends the operator's money on
every request from anyone.

The limit is per caller, in a sliding window, and never a single global
counter. Under `read = "token"` the caller is the token id, which is already
the unit `revoke` operates on. Under `read = "open"` the caller is the source
address. Exceeding the limit returns 429 and never a partial ranking.

This is a budget guard, not a defense. Phase 2's listing says so in the file's
own comment, so nobody later mistakes it for one.

A 429 carries `Retry-After` with the whole seconds until the caller's window
frees. The header is the only number any error response carries besides the
503's, and it is a header rather than a body field, so the closed-set error
body rule is untouched. Phase 4's client reads it and does not act on it: it
reports `rate_limited` and never retries. It is there for every other client,
which otherwise has to guess.

**Why phase 2 refuses `read = "open"` under an upstream proxy it does not
trust.** That refusal is defined in [phase 2](phase-02-tls.md), which covers
`[access] read = "open"` with `[tls] mode = "upstream"` and `trusted_proxy`
unset. Phase 2 blesses that TLS combination as the safe default, and it is,
because a forged `X-Forwarded-For` is worse than none. But it also means every
caller arrives from the proxy's address, so an address-keyed limit collapses
into one global bucket: no protection for the operator, and one busy client
locks out everyone. The two settings are individually right and jointly broken,
which is exactly the case a startup refusal exists for.

**One replica per limiter.** The limit is in process. Phase 5 must either run
one replica or move the counter out, and until it does, N replicas means N
times the budget. Recorded here so phase 5 inherits it as a known cost rather
than discovering it.

## `list` is bounded, because nothing else bounds it

`list` parses frontmatter for every page in the wiki to report `kind` and
`title`. On a large kb that is the cheapest way to load the service, and it
sits outside the `search` limit because it spends no money. Bound it instead of
limiting it.

`n` defaults to 200 and is capped at 1000. `after` takes the last name from the
previous response and the listing continues from there, in name order. A
response carries a `next` field holding the name to pass back, or null at the
end. `after` is exclusive: the listing resumes at the first name strictly
greater than it. Name order is stable, cheap, and needs no cursor state on the
server.

The guarantee that buys is exact and worth stating, because the alternative
reading is stronger than it can be. A page present for the whole of a paging
run appears exactly once. A page created or removed mid-run may be seen or
missed depending on where its name sorts against the caller's position. There
is no snapshot, and adding one would mean server-side cursor state, which is
the cost this design declines.

A page whose frontmatter does not parse has no `kind` and no `title`:
`core.parse_frontmatter` returns `None` on any malformed block
(`core.py:118-120`). List it with `kind` and `title` both null rather than
dropping it. A page the wiki holds and the listing hides is worse than a page
the listing admits it cannot read.

`search` answers differently for that same page and it is not a bug to fix
here. Its title comes from the row stored at embed time, and `_page_row` falls
back to the filename on a failed parse (`vectors.py:169-171`), so `search`
reports the filename where `list` reports null. `list` reads the file now and
can say it failed; `search` reads a row written earlier and cannot. Do not
loosen either to match: an agent that sees both learns the page is
unparseable, which is true.

`search`'s own `n` is capped the same way, at 1000. It goes straight into
vec0's `k` parameter (`vectors.py:242`), and an unvalidated one is a query the
caller sizes. Reject a non-integer, a negative, and a zero with 400.

`q` is checked the same way and before anything else. An absent `q`, an empty
one, and one that is only whitespace each return 400, and none of them reaches
`embed`. This is the same reasoning as the check-before-embed order above:
every path that spends the operator's money is refused before it spends it,
not after.

## What a stale or unconfigured index does

`rank` raises rather than returning a shorter list, and the route answers 503
with the count of pages missing a vector. Serving a ranking over a partial
index is the failure this avoids: the caller gets hits, has no way to see that
a third of the wiki was not considered, and concludes the wiki has nothing on a
subject it has pages for. That is the same reasoning that rejected a degrade
path when `sqlite-vec` is missing, recorded in the project's dependency rule.

A kb with no `[models] embed` is a different fault and gets a different answer.
`vectors._model_id` returns `None` there (`vectors.py:67-72`), and `_plan` then
treats every page as stale (`vectors.py:186`), so a naive route reports the
whole wiki as missing vectors. That names the wrong problem, and an operator
chasing it re-runs `embed` forever. `rank` raises `NoEmbedModel`, and the route
answers 501 with a distinct code. The other three routes still answer normally,
because none of them needs an embedding model.

A `ModelError` from the endpoint answers 502, not 503. The distinction is whose
fault it is: 503 means this service is not ready, and 502 means the model
endpoint it depends on failed.

Embedding on demand from a read route is out. It is a write to `vectors/` from
a read path, it needs the kb lock, and it makes one caller's `search` pay for
the whole wiki's backlog. Sweeping stays the writer's job. **Phase 5 owes that
writer.** Mode 1 has no ingesting process unless the deployment ships one, and
"container packaging" does not name it. A remote kb nobody sweeps answers 503
from `search` forever.

**Read routes take no kb lock.** Phase 14's `kb_lock` serializes writers.
Readers here are safe without it: page writes land through
`core.atomic_write_text`, so a reader sees the whole old file or the whole new
one, and the vector database runs in WAL mode (`vectors.py:78`). A read that
races a write returns content one write stale, which is not damage. State it
here so a later reader does not add a lock that would let any reader stall
every writer.

One write does survive on the read path and is not removed here: `_connect`
opens with `kb.vectors.mkdir(parents=True, exist_ok=True)` (`vectors.py:76`),
so a `search` against a kb that has never been embedded creates an empty
`vectors/`. It takes no lock, races nothing, and destroys nothing, and
`vectors/` is the CLI's own directory rather than the agent's. Named so the
next reader sees a known cost rather than a violation of the sentence above.

## Changes

`llmwiki`: `vectors.rank` extracted, `vectors.search` reduced to a printer over
it, and the `Hit`, `Ranking`, `StaleVectors`, and `NoEmbedModel` names added.
No behaviour change to any CLI verb.

`llmwiki_service`: the four routes, the token-then-name check order, the page
name check, the per-caller `search` limit, the `list` bound, and the response
shapes. The `[limits]` section and the startup refusal that guards it are
defined in [phase 2](phase-02-tls.md).

## Verification

Observed on a running service, never argued. Each test is seen failing before
its code exists.

**The anti-fork proof.**

- The same query against the same corpus returns the same ordered page names
  from `llmwiki search` and from `GET /kb/<name>/search`. Compare the name
  column.
- The service reaches ranking by importing `llmwiki.vectors.rank`. Assert it
  directly: patch `vectors.rank` to raise, and confirm the route fails. A route
  that shells out to the CLI and parses stdout passes the comparison above
  while defeating the whole phase, and only this test catches it.
- `llmwiki`'s existing suite passes unmodified, and stays hermetic under the
  dead-proxy run.
- `search`'s stdout and both stderr lines are byte-identical before and after
  the split. Capture them on the pre-split commit and diff.
- `_plan` runs exactly once per `rank` call. Count the calls.

**`page`.**

- Bytes identical to the file on disk, including the frontmatter block. Use a
  fixture page holding one byte that is not valid UTF-8, which is the only
  input that separates `read_bytes` from `read_page_text`.
- Each of these returns 404, on a deployment whose kb root is a symlink: a
  name of `../config.toml`, a name holding a separator, a percent-encoded
  separator, a name with an embedded NUL, a symlink inside `wiki/` pointing
  out, and a real page inside a subdirectory of `wiki/`. The last one fails
  against `is_relative_to` and passes only against parent equality.
- A legitimate page on that same symlinked deployment returns 200. Without this,
  a route that 404s everything passes every bullet above.

**`list`.**

- A wiki of 500 pages returns 200 entries and a `next`, and paging through with
  `after` yields all 500 exactly once, with no duplicate and no gap.
- `n=1001` is capped at 1000. `n=0`, `n=-1`, and `n=abc` each return 400.
- A page with malformed frontmatter appears in the listing with null `kind` and
  null `title`.
- `kind=story` returns only story pages, on a fixture holding at least three
  kinds.

**`schema`.**

- A kb with no `SCHEMA.md` returns 404, and the other three routes still
  answer.
- A `SCHEMA.md` that exists and cannot be read returns 500, not 404. A route
  that 404s on any `OSError` reports a permissions fault as an absent file.

**Auth and limits.**

- With `read = "token"`, each of the four routes refuses an absent token, a
  malformed token, and a revoked token, and the three refusals are
  indistinguishable to the caller.
- A token of role `reader` is accepted on all four, and the role check is
  exercised, not just token validity.
- With `read = "open"`, each of the four answers with no token.
- Under `read = "token"`, the request after `search_per_minute` from one token
  returns 429, while a second token is unaffected. A single global counter
  fails this.
- The same, keyed on address, under `read = "open"`.
- The endpoint's call count is read back after a 429 to confirm no embedding
  call was made for the rejected request.
- Sixty requests spanning a minute boundary in two seconds return 429. A fixed
  window passes only by accident.

**Faults.**

- A kb with one page missing a vector returns 503 from `search`, names the
  count, and the endpoint's call count confirms no embedding call was made.
  This is the check-before-embed order, and nothing else observes it.
- A kb with no `[models] embed` returns 501 with its own code, not a 503
  naming the whole wiki.
- A model endpoint failure returns 502.
- A kb name not in `[kbs]` returns 404 with a body naming no kb. Under
  `read = "token"`, an unknown kb and a known kb return the same status and the
  same body to a caller with no token.
- Every error body across every route above is checked against the closed code
  set. None carries a path, a kb name, or an exception message.
