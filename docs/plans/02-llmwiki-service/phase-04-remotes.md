[overview](overview.md)

# Phase 4: `[remotes]` and the fan-out client

**Goal.** A project declares the wikis it can reach in its own `config.toml`,
and the CLI searches them and reads pages back from them, keeping each wiki's
answer separate from every other wiki's answer.

**Problem.** Phase 3 put four routes behind the door and left nobody outside
it. The routes are single-kb by design, so the fan-out has to live somewhere,
and the only place it can live is the client. That is the whole of this phase:
a table of pointers, a small HTTP client, and one rule about what the client
must never do with the numbers it gets back.

The four traps this phase is written to avoid: a client that concatenates two
wikis' rankings and sorts them, a token that reaches `config.toml` or an error
message, a fan-out that hangs on one dead host, and a response from a remote
that is trusted the way a local file is trusted.

## `[remotes]` is a table of pointers, and every key is checked

The table key is the local label for the remote. It is what the user types
after `--remote` and what the output prints as a block header.

```toml
[remotes.homelab]
url = "https://kb.example.net/kb/homelab"
token_env = "HOMELAB_KB_TOKEN"

[remotes.reports]
url = "https://kb.example.net/kb/reports"
```

| Key | Type | Required | Default | Rule |
|---|---|---|---|---|
| `url` | string | yes | none | `https` scheme, a host, no query, no fragment, no userinfo. A trailing `/` is stripped |
| `token_env` | string | no | unset | The name of an environment variable. Never the token |
| `mode` | string | no | `"read"` | `"read"` is the only accepted value |

Nothing else is accepted. **An unknown key is refused by name**, and so is a
key of the wrong type, a missing `url`, and a `[remotes]` value that is not a
table. A silently ignored `token-env` becomes a 401 the user cannot explain, so
the parse refuses it instead. The message names the remote and the key, the
verb exits 2, and no socket is opened.

Three further refusals, each closing a way a pointer file becomes a credential
file or a broken output line:

- `url` carrying userinfo, as in `https://user:secret@host/kb/x`. That is a
  credential in `config.toml`, which the [overview](overview.md) forbids.
- `mode = "write"`. Writable remotes do not exist. The value is reserved and
  refused here so a hopeful config fails at the parse rather than looking like
  it did something.
- A label that is `local`, or that holds a space, a tab, or a control
  character. `local` is reserved for the kb on disk, and `search` output is tab
  separated.

**`mode` is validated and then discarded.** It has exactly one legal value, so
a field on the parsed record would always hold `"read"` and would read as
though something consults it. Only the server decides what a caller may do,
and a client field that looks like it decides is worse than no field. Keep the
key, keep the refusal, store nothing.

**`[remotes]` is parsed when a verb needs it, not at startup.** A project whose
remotes table has a typo can still `ingest`, `lint`, and `embed`. The cost is
that the typo waits until the first `search`, and nothing else in the CLI finds
it earlier. `lint` does not grow a seventh check for this: it checks `wiki/`.

## The token comes from the environment, one variable per remote

`token_env` names the variable. The client reads it with `os.environ.get` on
each request, sends it as `Authorization: Bearer <value>`, and keeps it
nowhere else. It never reaches module state, stdout, stderr, a log line, or an
error message.

**`token_env` set and the variable absent or empty refuses that remote before
the request**, with the code `no_token`. Falling back to an unauthenticated
call turns a missing secret into a confusing 401 against a closed deployment,
and into a silent success against an open one, which is worse: the user
believes a credential was used.

**`token_env` unset sends no `Authorization` header at all.** That reaches a
deployment running `[access] read = "open"`, which phase 1 calls the expected
setting for a home or team service.

**`LLM_WIKI_API_KEY` is never a fallback.** It is the model endpoint's
credential. Sending it to a wiki host would hand the model key to a host that
has no business holding it. There is no per-remote file variant either: a token
kept in a file reaches the process through the shell that exports it.

## The client surface

Three verbs, all read-only. `remotes.py` is a new module, because `model.py`
owns one endpoint client whose entire error contract is `ModelError` and whose
credential is the model key. A second endpoint, with a different credential and
a different error set, does not belong in that file.

```python
# llmwiki/remotes.py

REMOTE_TIMEOUT_SEC = 20     # unmeasured; see the note below
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class Remote(NamedTuple):
    name: str               # the [remotes] table key
    url: str                # base url of one kb, no trailing slash
    token_env: str | None


class RemoteHit(NamedTuple):
    rank: int               # 1-based position WITHIN this remote's answer
    name: str               # page filename, e.g. "cross-site-forgery.md"
    title: str
    updated: str            # TIMESTAMP_FORMAT, as the route returned it
    size: int


class RemoteRanking(NamedTuple):
    remote: str
    hits: tuple[RemoteHit, ...]


class RemoteFailure(NamedTuple):
    remote: str
    code: str               # one of the closed set below


class RemoteError(Exception):
    """Carries `code` from the closed set. Never carries the url, the
    response body, a header, or the token."""


Answer = RemoteRanking | RemoteFailure


def parse_remotes(config: dict) -> dict[str, Remote]:
    """The `[remotes]` table, checked. Raises ValueError naming the
    remote and the offending key. Opens no socket."""


def search_one(
    remote: Remote, query: str, n: int, kind: str | None
) -> Answer:
    """GET <url>/search. Catches RemoteError and returns RemoteFailure,
    so one broken remote cannot end a fan-out."""


def fan_out(
    remotes: list[Remote], query: str, n: int, kind: str | None
) -> tuple[Answer, ...]:
    """One entry per remote, in the order given, whatever happened. One
    request per remote, in parallel, each bounded by REMOTE_TIMEOUT_SEC."""


def page(remote: Remote, name: str) -> bytes:
    """GET <url>/page/<name>. Raises RemoteError."""


def schema(remote: Remote) -> bytes:
    """GET <url>/schema. Raises RemoteError."""
```

`cli.py` gains two rows in `VERBS` and two flags on `search`:

```
search <query> [-n N] [--kind K] [--remote NAME]... [--all]
page [--remote NAME] <page>      print one page's bytes
schema [--remote NAME]           print the wiki's SCHEMA.md
```

- **No flag means no network.** `search <query>` behaves exactly as it does
  today, byte for byte, even when `[remotes]` is populated. A verb that started
  reaching hosts because a config section appeared would leak the query text to
  every declared wiki without the user asking.
- `--remote NAME` is repeatable and adds those remotes to the local search.
  `--all` adds every remote in the table. An unknown NAME exits 2.
- `-n` is per wiki. `--all -n 5` returns at most five hits from each.
- **The local kb takes part under the label `local`**, and only when
  `kb.wiki.is_dir()`. A `.kb/` holding nothing but a `config.toml` with
  `[remotes]` is a pointer, not a wiki, and the overview blesses that shape.
  Skipping it there is what makes a pointer-only project work.
- `page` and `schema` without `--remote` read the local file. They exist in
  both forms so an agent's command does not change with the wiki's location.
  Local `page` reuses phase 3's check: resolve the candidate, require the
  resolved parent to equal `kb.wiki.resolve()`, require a `.md` suffix.

**Fan-out costs the client one paid call, not one per wiki.** The local half
embeds the query once, through `vectors.rank`. Each remote embeds it on its own
side with its own model, on its own budget. A project with no `[models] embed`
and only remotes makes zero paid calls and still searches.

## The return type carries no cross-wiki sort key

Similarity scores are comparable only within one embedding model's
distribution. Phase 3 pins the reasoning and the evidence:
`NEIGHBOUR_FLOOR = 0.35` (`vectors.py:47`) is calibrated to one model over 1653
page pairs, and `vectors._embed_target` reads each kb's embed target from
that kb's own `config.toml` (`vectors.py:72-78`), so two remotes can already
be ranking with
different models. A 0.61 from one wiki and a 0.58 from another are two readings
from two instruments.

So the answers stay apart: one ranked list per wiki, labelled with the wiki it
came from, never concatenated and re-sorted.

**`RemoteHit` has no score field.** That is the structural half of the rule,
and it is why this section names a type rather than a convention. The route
returns a score, phase 3 unchanged, and the client parses it and drops it. What
reaches the caller is `rank`, a position within one wiki's own list. Sorting a
concatenation of two wikis by `rank` interleaves both wikis' first hits and is
visibly nonsense, so the merge is not discouraged, it has no key to sort on. A
caller who still wants one list has to fetch the scores itself and owns the
consequence.

Dropping the score costs nothing the client could have used. The client does
not know which embedding model produced it, so it cannot tell a strong 0.45
from a near-floor 0.45. Rank is the part that survives the crossing.

Two smaller pieces of the same rule:

- `fan_out` returns entries **in the order the remotes were named**, never in
  any order derived from the answers.
- The printer emits a `# <label>` line before each block and prints `rank`,
  not a score, in every block including `local`. No line of fan-out output
  carries a number that is comparable across two blocks.

## What a broken remote does

Every failure is one `RemoteFailure` for one remote, one line on stderr as
`<remote>\t<code>`, and no effect on any other remote. Codes are a closed set:

| Code | Cause |
|---|---|
| `no_token` | `token_env` names a variable that is absent or empty |
| `unauthorized` | 401 or 403 |
| `not_found` | 404, which is also an unknown kb and an absent `SCHEMA.md` |
| `rate_limited` | 429 from phase 3's `search` limit |
| `no_embed_model` | 501, the remote has no `[models] embed` |
| `upstream_model_failed` | 502, the remote's model endpoint failed |
| `index_stale` | 503, the remote has pages with no vector |
| `http_error` | any other status, and any 3xx |
| `timeout` | no response within `REMOTE_TIMEOUT_SEC` |
| `unreachable` | DNS, connection, or TLS failure |
| `bad_response` | the body did not parse, see below |

**One remote down.** The other blocks still print. The dead remote gets its
stderr line. The verb exits 1.

**One remote slow.** `fan_out` issues the requests in parallel through
`concurrent.futures.ThreadPoolExecutor`, so the wall clock is one timeout and
not N. The slow remote gets `timeout` and the rest print. This is not retrying,
pooling, or caching, and none of those three is added: a timed-out request is
reported and never sent again.

`REMOTE_TIMEOUT_SEC = 20` is a guess and is labelled as one in the code. It
sits below `model.REQUEST_TIMEOUT_SEC = 60`, so a remote whose own model call
is genuinely slow reads as `timeout` at the client. That is the intended trade:
a search is interactive, and a user waiting a minute for one wedged wiki is a
worse outcome than a wiki reported as slow. Measure the p99 of a real
`GET /search` against a populated kb and set it from that.

**One remote returning garbage.** The wire is a trust boundary and gets one
parse function, which rejects the whole response rather than salvaging part of
it. A response is refused with `bad_response` when any of these holds:

- the body is longer than `MAX_RESPONSE_BYTES`, measured by reading one byte
  past the cap rather than by trusting `Content-Length`. This one binds all
  three verbs, `page` and `schema` included, and an over-cap body is refused
  whole. A `page` truncated at the cap and written to `sys.stdout.buffer`
  would be a corrupted file that looks like a file
- the body is not JSON, or `hits` is not a list
- any hit is missing `name`, `title`, `updated`, or `size`, or holds one of
  the wrong type
- any hit's `name` is not a bare `.md` filename: a separator, a `..`, an
  absolute path, or an embedded NUL fails it
- `page` or `schema` answers with a media type that is not `text/markdown`.
  Compare the media type alone, lowercased, with any parameter and any
  surrounding whitespace stripped. A strict equality against the whole header
  value refuses a legitimate response the moment a service adds a parameter

Every string from the wire goes through `core.flatten` before it is printed,
so a title holding a tab cannot forge a column.

**Unknown fields on the wire are ignored.** That is the opposite of the config
rule two sections up, and the asymmetry is deliberate. A user writes
`config.toml`, so an unknown key there is a typo worth naming. A service writes
the response, and it may be newer than the client, so refusing an added field
would force every client to upgrade in lockstep with every service.

**Exit codes.** `search` exits 0 when every participant answered, 1 when any
participant failed, including a local `StaleVectors`, and 2 for a usage or
config error. `page` and `schema` exit 0, 1, or 2 the same way. A partial
answer never exits 0, because an agent reading a fan-out cannot otherwise tell
that a wiki it asked was not consulted.

## The transport is `model`'s opener

`model._OPENER` is renamed to `model.OPENER` and `remotes.py` uses it. It is
built with `ProxyHandler({})` and a redirect handler that returns `None`, and
both properties matter more here than they do for the model endpoint: a remote
token would otherwise follow a 3xx to a host `config.toml` never named, or
travel through a proxy the environment named and the config did not. One
definition for every path that carries a credential, and one place to prove
it.

`fetch.py:53` holds a second opener and keeps it. It is the same
`ProxyHandler({})` and a redirect handler that raises rather than follows, so
`fetch` can walk hops itself up to `MAX_REDIRECTS` while fetching a public
url. That path carries no credential, so the two differ for a reason and are
not merged here.

Consequences to state rather than discover:

- A remote reachable only through an HTTP proxy is not reachable. That is the
  rule working, not a bug.
- A 3xx is `http_error`. The client does not follow it and does not report
  where it pointed.
- There is no way to skip certificate verification. No `verify` key, no
  `insecure` flag, and none is added later to make a test pass.

`urllib.request.urlopen` appears nowhere, per the project rule.

The page name is quoted with `urllib.parse.quote(name, safe="")` before it
goes into the path, so a name holding a separator cannot reshape the request.
Phase 3's route refuses it as well. Both checks stay.

## What phase 4 does not do

- No writes to a remote, and no client for a write route, which does not exist.
- No merged cross-wiki ranking, under any flag, ever.
- No client for phase 3's `list` route. `search` covers discovery, and the
  route stays reachable by any HTTP client until something needs it here.
- No caching of any response, no retry, no backoff, no connection reuse.
- No credential source other than `token_env`.
- No move of `[endpoint]` out of `config.toml`. Phase 5 owns that move, and
  rung 1 already injects the url from `LLM_WIKI_ENDPOINT_URL` at run time.
- No change to any phase 3 route, request shape, or response shape.
- No change to `search`'s output when no remote flag is given.

## Changes

`llmwiki`: a new `remotes.py` holding the parse, the types, and the client;
`model._OPENER` renamed to `model.OPENER`; `cli.py` gains `page` and `schema`
in `VERBS` and `--remote` and `--all` on `search`. No existing verb changes its
behaviour or its output.

`llmwiki_service`: nothing.

`tests`: a new `tests/fake_wiki.py`, built like `tests/fake_endpoint.py`, that
serves the four phase 3 routes on a background thread bound to port 0 and
records the requests it received.

## Verification

Observed against a running fake service, never argued. Each test is seen
failing before its code exists.

**The parse, with no socket open.** Each of these names the remote and exits 2,
and `fake_wiki` records zero requests: a missing `url`, `url = "http://..."`,
a url with userinfo, a url with a query, an unknown key, `mode = "write"`,
`mode = "admin"`, a `token_env` that is not a string, a label of `local`, a
label holding a tab, and a `[remotes]` entry that is not a table.

**The token.**

- `token_env` set and the variable exported: the fake wiki sees
  `Authorization: Bearer <value>` on exactly one request.
- The token string appears in neither stdout nor stderr, on success and on
  every failure code. Search both streams for it.
- `token_env` set and the variable unset: `no_token`, and zero requests.
- `token_env` absent: the request carries no `Authorization` header.
- With `LLM_WIKI_API_KEY` and the remote's variable set to different values,
  the fake wiki never sees the model key.

**The transport.**

- A fake wiki answering 302 toward a second fake wiki yields `http_error`, and
  the second one records zero requests. This is the property the opener exists
  for and nothing else observes it.
- With `http_proxy` and `https_proxy` pointed at a dead port, the fan-out still
  reaches the fake wiki. The suite's dead-proxy run then covers this module.
- The fan-out tests build `Remote` values directly, so a plain-http fake is
  reachable while the https rule stays enforced in `parse_remotes`, which is
  the only place a user's config touches. Do not add a config key to make these
  tests pass.

**Separation, which is the phase.**

- `assert "score" not in RemoteHit._fields`. A later reader who adds the field
  back fails here first.
- Two fake wikis, one returning high scores and one low: the output holds two
  blocks, each in its own order, and no line carries a score.
- Blocks print in the order the remotes were named, not in any order derived
  from the answers. Name the low-scoring wiki first and confirm it prints
  first.
- `-n 5 --all` returns at most five hits per block, not five in total.

**Faults.**

- One url pointing at a closed port: the working block still prints, the dead
  remote is one stderr line, and the exit code is 1.
- Two remotes each blocking past the timeout: both report `timeout` and the
  wall clock is near one timeout, not two. A sequential fan-out fails this.
- 401, 404, 429, 501, 502, and 503 each map to their own code, and the 429 case
  records exactly one request. A retry shows up as two.
- Garbage, one case each, all leaving the other remote's block intact and
  writing nothing from the bad body to stdout: a body that is not JSON, `hits`
  that is not a list, a hit with no `title`, a hit named `../config.toml`, a
  hit name holding a tab, and a body over `MAX_RESPONSE_BYTES` sent with a
  `Content-Length` that understates it.

**`page` and `schema`.**

- Remote `page` bytes are identical to the file the fake wiki serves, including
  a byte that is not valid UTF-8. That is the case separating
  `sys.stdout.buffer` from `print`.
- A `page` response typed `text/html` yields `bad_response` and writes nothing.
- Local `page` refuses `../config.toml`, a name holding a separator, and a real
  page inside a subdirectory of `wiki/`, on a kb whose root is a symlink, and
  returns a legitimate page on that same kb.

**No surprises for the local path.**

- `search <query>` with a populated `[remotes]` makes zero requests to any fake
  wiki, and its stdout is byte-identical to the same command on the parent
  commit.
- A `.kb/` holding only a `config.toml` with `[remotes]`: `search --all`
  answers from the remotes, prints no `local` block, and the model endpoint
  records zero calls.
- `--all` over three remotes on a kb that does have a wiki: the model endpoint
  records exactly one call.
- `llmwiki`'s existing suite passes unmodified and stays hermetic under the
  dead-proxy run.
