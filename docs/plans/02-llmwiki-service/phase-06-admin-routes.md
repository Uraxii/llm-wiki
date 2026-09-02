[overview](overview.md)

# Phase 6: the three admin routes

**Goal.** An operator with the bootstrap credential can mint a token, see what
tokens exist, and revoke one, over the same listener that serves reads, without
the plaintext of any token ever reaching a log line or a second response.

**Problem.** Phase 1 built `mint`, `list_tokens`, and `revoke` and gave them no
caller. All three sit in `llmwiki_service/tokens.py` (`mint` at
`llmwiki_service/tokens.py:197`, `list_tokens` at
`llmwiki_service/tokens.py:232`, `revoke` at
`llmwiki_service/tokens.py:241`) and every call to any of them today comes
from a test. `ROLE_FOR_OPERATION` in `llmwiki_service/auth.py:25` already maps
an `admin` operation to the `admin` role, and no route passes that operation:
`llmwiki_service/routes.py:140` passes `"read"` and nothing else does. So the
service can recognise an admin and has nothing for one to do.

Underneath that sits a harder problem the earlier phases left open. The
environment variable LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN is read at exactly one
place, `llmwiki_service/deployment.py:187`, and only for presence. Its value
never reaches `TokenStore`, and `authorize` verifies against the database alone
(`llmwiki_service/auth.py:259`). Today the variable gates startup and
authenticates nobody. A first admin route with no way to authenticate the
first admin is a route nobody can call.

**A note on citation form.** Citations into `llmwiki` and `tests` use the
checked `module.py:line` form. A citation into `llmwiki_service` uses that
same form with its one directory prefix, `llmwiki_service/module.py:line`,
never the bare form: two modules of the same name in different packages would
make the bare form ambiguous, so `check-plan-citations` only resolves the
prefixed one for this package.

## The routes

Three, on one path, service wide.

| Route | Does |
|---|---|
| `POST /admin/tokens` | mint one token and return its plaintext once |
| `GET /admin/tokens` | every row the store may disclose, never a hash |
| `DELETE /admin/tokens/{label}` | revoke by the friendly name |

They do not hang off `/kb/<name>/`. A token is service wide: phase 3 settled
that a valid reader token reaches every kb in `[kbs]` and that no per-kb grant
exists in this design ([phase 3](phase-03-read-endpoints.md), lines 39 to 41).
Minting a token under one kb's path would read as a grant scoped to that kb,
and it would not be one.

The label is in the URL on `DELETE` and nowhere else, and no route ever puts a
credential in a path or a query string. Uvicorn's access log records the
request line, so a token in a URL is a token in a log file on every deployment
that turns access logging on. That is why `mint` is a `POST` carrying a JSON
body rather than the `GET` that would otherwise be shorter to type.

## The bootstrap credential gets its own path, and `authorize` is not touched

This is the decision the phase turns on. Three ways were open.

**Seed the token database at boot with a row holding the bootstrap secret.**
Refused outright by [phase 5](phase-05-packaging.md) line 162: no entry point
that migrates, seeds, or mints anything at boot. The rule is right. A boot that
writes a row makes the database's contents a function of the environment at
last restart, and an operator who changes the variable then cannot say which
rows the old value left behind.

**Special-case the environment variable inside `authorize`.** `authorize` is
one function whose whole job is one credential path, and it is mutation gated
precisely because it is the security boundary. A second source inside it means
every future reader of the gate has to hold two rules at once, and every mutant
the gate generates for the branch that picks between them is a mutant somebody
has to reason about. Refused.

**Chosen: a second gate function, `authorize_admin`, that tries the bootstrap
comparison first and then delegates to `authorize` unchanged.** It lives beside
`authorize` in `llmwiki_service/auth.py`, which the mutation gate already
covers and `tests/test_service_auth.py` already exercises.

```python
def authorize_admin(
	header: str | None,
	bootstrap: str | None,
	access: Mapping[str, str],
	store: TokenStore,
	limiter: FailureLimiter,
	client: str,
) -> Principal | Refusal:
	"""Bootstrap credential first, then the token database."""
```

Four properties make it worth the extra function.

- `authorize` keeps exactly one credential source. Its body does not change,
  and the slice that adds this is expected to show an empty diff for it.
- A minted `admin` token still works on all three routes, because the delegate
  call is `authorize(header, "admin", ...)`. That call is what finally gives
  `ROLE_FOR_OPERATION`'s `admin` entry a caller.
- The two paths share one `FailureLimiter` and one client key, so ten failed
  guesses per minute per caller is a total across both, not ten each.
- The comparison is `secrets.compare_digest` on the two values encoded to
  bytes, never on the strings. `compare_digest` raises `TypeError` on a string
  holding a non-ASCII character, which is exactly why `token_id` compares its
  checksum with `==` instead (`llmwiki_service/tokens.py:90-93`). A
  caller controls the header, so a string comparison here turns a junk
  credential into a 500 rather than a 401.

The bootstrap caller resolves to a constant principal, declared beside
`ANONYMOUS` at `llmwiki_service/auth.py:58`:

```python
BOOTSTRAP = Principal(token_id="bootstrap", label="bootstrap", role="admin")
```

`token_id` is `"bootstrap"` rather than the empty string on purpose. The empty
string is the anonymous principal's value, and `llmwiki_service/routes.py:173`
reads it as the sentinel for "key the search limit on the address instead of
on a token".
A real `<id>` is sixteen hex characters, matched by `_ID_PATTERN` in
`llmwiki_service/tokens.py:66`, so the literal `bootstrap` can never collide
with one.

**What this gives up, stated plainly.** The bootstrap credential is not
revocable at runtime. Revoking it means unsetting the variable and restarting
the process. The user's directive that a revoke is needed is satisfied for
minted tokens, which is where it bites: a minted token is the one that gets
pasted into a repository, handed to a colleague, or baked into a client
config. The bootstrap value is held by the operator who owns the process, and
that operator can already restart it.

The lifecycle that makes this cheap is already in the deployment file. Refusal
row 5 accepts either the environment variable or an existing live admin row
(`llmwiki_service/deployment.py:183-194`), so the intended sequence is
to start with the variable, mint a real admin token, unset the variable, and
restart. From then on the only admin credential is one `DELETE` can take away.

## Request and response shapes

Named here because two implementers, one per side of the wire, cannot agree on
a table of column headings. That is phase 3's reasoning
([phase 3](phase-03-read-endpoints.md), lines 63 to 64), and it applies with
more force to a route whose response holds a secret.

```json
// POST /admin/tokens, request
{"label": "nicole-laptop", "role": "reader"}

// POST /admin/tokens, 201
{"token": "llmwiki_<id>_<secret><crc>", "id": "<id>",
 "label": "nicole-laptop", "role": "reader",
 "created_at": "2026-09-02T11:04:31Z"}

// GET /admin/tokens, 200
{"tokens": [
  {"id": "<id>", "label": "nicole-laptop", "role": "reader",
   "created_at": "2026-09-02T11:04:31Z",
   "last_used_at": "2026-09-02T11:31:09Z", "revoked_at": null}
]}

// DELETE /admin/tokens/nicole-laptop, 204, no body
```

`token` appears in the 201 body and in no other response, ever. `mint` returns
the only copy of the plaintext that will exist, and the store keeps a keyed
hash (`llmwiki_service/tokens.py:197-230`). `TokenRow`, the type
`list_tokens` returns, holds no hash and no plaintext at all
(`llmwiki_service/tokens.py:169-180`), so the listing route cannot
leak one even by accident. There is no route that returns a token by id, and
adding one later would undo the property the 201 body is written to hold.

`GET /admin/tokens` returns every row, revoked ones included, oldest first,
with no `n` and no `after`. That is deliberately unlike phase 3's `list`, which
is bounded because a wiki holds thousands of pages. This table holds one row
per time a human ran `mint`. If a deployment ever outgrows one response, the
fix is the `after` and `n` pair phase 3 already specifies, copied rather than
invented.

`DELETE` answers 204 with no body. There is nothing to say: the label was in
the request and the caller knows it.

## What a bad request gets

The closed error code set for this phase, in the same shape phase 3 uses,
`{"error": "<code>"}` and nothing else:

| Case | Status | Code |
|---|---|---|
| absent, malformed, unknown, or revoked credential, or a role below `admin` | 401 | `unauthorized` |
| too many failed authentications from this caller in the window | 429 | `rate_limited` |
| body is not a JSON object, or `label` is absent or fails the label contract | 400 | `invalid_label` |
| `role` is absent or is not one of `tokens.ROLES` | 400 | `invalid_role` |
| the label already names a row | 409 | `duplicate_label` |
| `DELETE` on a label that is unknown or already revoked | 404 | `not_found` |
| anything else raised | 500 | `internal_error` |

Four of these need their reasoning stated.

**Duplicate label is 409, not a 500.** The unique constraint on `label` makes
`mint` raise `sqlite3.IntegrityError` (`llmwiki_service/tokens.py:197-208`).
Nothing catches it today, so without this the operator's second use of a
name they already used gets `{"error": "internal_error"}` from `app.py`'s
`Exception` handler and no idea what went wrong. The route catches
`sqlite3.IntegrityError` around the `mint` call and only there.

**A 409 is not an enumeration oracle here.** Phase 3 collapses statuses so an
unauthenticated caller cannot tell one kb from another. Every caller who
reaches this code has already presented an admin credential and can call `GET
/admin/tokens` to read the whole table, so there is nothing left to enumerate.
The distinctions on this route are for the operator's benefit and cost nothing.

**`invalid_label` and `invalid_role` are separate codes.** Phase 3 splits
`invalid_query` from `invalid_n` for the same reason: the caller is a person
with curl, and one code for "your request was wrong somehow" makes them guess.
The route checks `role in tokens.ROLES` itself before calling `mint`, so
`mint`'s `ValueError` can then only be a label fault, and the mapping from
exception to code stays one line with no message parsing.

**`DELETE` on an unknown label and on an already revoked one both answer 404.**
`revoke` returns one boolean and cannot tell them apart
(`llmwiki_service/tokens.py:241-250`), and both mean the same thing to
the caller: nothing changed. The alternative is an idempotent `DELETE` that
answers 204 every time, which reads better as REST and tells an operator less.
Nothing in this plan retries a request. Phase 4's client reports a failure and
never retries ([phase 3](phase-03-read-endpoints.md), lines 281 to 283), so
idempotence buys nothing here and the informative answer wins.

## The label contract belongs to `mint`, not to the route

`mint` already refuses an empty label and one holding a control character
(`llmwiki_service/tokens.py:209-214`). Two holes are left in it.

A 100 KB label is accepted today, and it would be written into every log line
that names the token instead of naming the secret. Cap it at 128 characters,
counted as characters and not bytes. The number is chosen against the label's
job rather than a buffer size: it is the friendly name a human types to revoke,
so anything past a long hostname plus a person's name is a paste accident.

A label with leading or trailing whitespace is accepted and stored unstripped,
while the emptiness guard tests `label.strip()`. So `"ops"` and `"ops "` are
two rows that look identical in a listing and revoke separately. Refuse a label
that differs from its own `strip()`.

The recorded U+2028 footgun is closed at this commit. `flatten` collapses U+2028
and U+2029 along with the C0 controls, DEL, and NEL (`core.flatten` at
`core.py:111-115`), so `mint`'s `flatten(label) != label` guard now catches a
label carrying either. It did not always, which is why the footgun was
recorded; it does now, and no new guard is needed for it.

Both new guards go in `mint`, not in the route. `mint` is where the other two
already live, and a guard in the route leaves every other caller of `mint`
unprotected, including the tests that mint fixtures and any future local
command.

The full contract, after this phase: a label is at most 128 characters, is not
empty or whitespace only, has no leading or trailing whitespace, and holds no
character `flatten` would collapse.

## Limits: admin shares the failure budget and gets no new one

Admin routes are counted by the existing `FailureLimiter` and are outside
`SearchLimiter`. Neither limiter is copied, subclassed, or configured.

`SearchLimiter` exists because `search` calls a paid embed on every request
([phase 3](phase-03-read-endpoints.md), lines 258 to 268), and `[limits]
search_per_minute` is documented in the deployment file's own comment as a
budget guard and not a defense. Minting a token costs one sqlite insert and no
money, so putting admin routes under a model budget would misname what the
budget is for.

Sharing one `FailureLimiter` across the read gate and the admin gate is the
restrictive reading. A caller who burns its ten failures per minute guessing at
`/kb/x/search` has none left for `/admin/tokens` in that window. Two separate
buckets would hand the same caller twenty.

Both limiters are per process and are not thread safe, which is why the
deployment runs exactly one replica ([phase 3](phase-03-read-endpoints.md),
lines 295 to 296, and [phase 5](phase-05-packaging.md), lines 290 to 299). This
phase adds no state that changes that and no reason to revisit it.

## Logging, so phase 1's mint bullet becomes checkable

Phase 1 asks that every log line produced during a mint be searched for the
minted secret, and that the secret appear exactly once, in the mint response
([phase 1](phase-01-tokens.md), lines 142 to 144). That bullet is not checkable
against a design that has not said what gets logged. Here is the contract.

The admin module emits at most one line per request, on the
`llmwiki_service` logger, with these formats and no others:

```
admin mint label=%s role=%s id=%s by=%s
admin revoke label=%s by=%s
admin refused code=%s by=%s
```

Three rules hold it together.

- No format argument is ever the value `mint` returned, the `Authorization`
  header, or the bootstrap value. The mint line names the `<id>` segment, which
  phase 1 states is not secret ([phase 1](phase-01-tokens.md), lines 118 to
  119).
- `by` is `principal.label`, which is `bootstrap` for the bootstrap caller and
  the row's own label for a minted admin. On the refused line, where there is
  no principal, `by` is the client key the limiter counts on, which is an
  address or a token id and never a credential.
- A label cannot forge a second line, because `mint` rejects any label
  `flatten` would change (`core.flatten` at `core.py:111-115`).

Nothing else in the request path logs a credential either. The one existing
per-request line is in `ForwardedHeaderMiddleware._resolve`
(`llmwiki_service/app.py:168-170`), which formats the scheme and the
client and reads no header value into the message.

## Binding and TLS: one listener, and the operator draws the line in front

The three routes are served on the same listener as the read routes and the
health check. No second bind address, no second TLS context, no new deployment
key for either.

The argument for a separate admin listener is that the admin credential is the
highest value one, so it should be reachable from fewer places. The argument
against is what a second listener actually buys inside one process. The
deployment runs exactly one replica, so a second listener shares the process,
the `TokenStore`, the `FailureLimiter`, and under `mode = "terminate"` the same
`ContextHolder`. What it adds is a bind key, a startup refusal for that key, a
second socket to reason about in the "nothing is listening after a refusal"
proof, and a second address to get wrong. What it protects is a boundary the
operator can already draw, and can draw better: a firewall rule on the existing
bind, or a proxy that does not route `/admin` from outside.

Under `mode = "upstream"` the admin credential crosses the proxy hop in
cleartext, exactly as a reader token does today. That is already bounded by
refusal row 10, which requires the plaintext listener to be bound to a private
interface (`phase-02-tls.md:172`).

What the restrictive default demands instead is that `[access]` cannot be
written to open this surface. It already cannot: `authorize` opens only `read`,
and only on the exact word `open` (`llmwiki_service/auth.py:254-258`),
so `admin = "open"` in a deployment file is silently inert today. Silently is
the problem, and phase 2's table gains one row for it below.

## Phase 2 amendments

Three surgical changes, all of them in
[phase-02-tls.md](phase-02-tls.md), because that file is the single source of
truth for the deployment file and for every startup refusal.

**Route count, at line 32.** A sibling change corrected it from seven to "five
routes total, four read and one health check", which was right at that commit.
This phase makes it eight: four read, one health check, and three admin. The
read-route count of four is unchanged and stays as the sibling wrote it.

**What the bootstrap variable does, at lines 141 to 149.** That paragraph names
`LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN` beside the refusal that reads it and says
nothing about what the value is for, which was accurate while nothing read the
value. It gains one sentence: the value authenticates the three admin routes in
phase 6, on a path that never consults the token database, and revoking it
means unsetting the variable and restarting.

**One new refusal row, taking the table from 13 rows to 14.** The table now
runs from line 163 to line 176 and states its own count. The new row:

| Condition | Why refusing beats starting |
|---|---|
| `[access] admin = "open"` | there is no deployment where an unauthenticated mint is intended, and the gate ignores the value, so a file that sets it describes a service that does not exist |

It is appended as row 14 rather than placed beside row 6, its natural
neighbour. `REGISTRY` in `llmwiki_service/deployment.py:381-424` pairs
each check with the row number it carries in that table, and its tests name
those numbers, so inserting in the middle would renumber seven rows and their
checks for a cosmetic gain.

No other phase defines a key or a refusal, and this phase defines none outside
that table.

## Mutation gate consequences

One new module means one new entry in each of two lists, in the same change.

- `llmwiki_service/admin.py` is added to `only_mutate` in `pyproject.toml`.
- `tests/test_service_admin.py` is added to
  `pytest_add_cli_args_test_selection` in the same commit.

Adding the module without the test file drops every mutant of the new routes
into mutmut's `no tests` state, where the gate reports green while measuring
nothing on them. There is no baseline for that state and the count must always
be zero. This is the exact hole `SearchLimiter` fell into, and this phase must
not reopen it.

Everything else this phase touches lands in a module the gate already mutates,
covered by a test file already in the selection, so it costs nothing new:
`auth.py` gains `authorize_admin` and `BOOTSTRAP`, `tokens.py` gains two label
guards and `MAX_LABEL_CHARS`, `deployment.py` gains row 14's check and the
reader that hands the bootstrap value to the app, and `app.py` gains the wiring
and one parameter on `build_app`.

**Do not add `tests/test_service_main.py` to the selection.** Its
`RunningServiceTest` boots real uvicorn servers with real TLS contexts, and the
gate reruns the selected files once per mutant. It would turn a run that costs
about seven seconds today into one nobody waits for. Nothing in `only_mutate`
needs it: every mutated line has a covering unit test file already.

New survivors are expected on a module this size. Regenerate the baseline only
with `--update-baseline`, and only for a mutant proven equivalent, with its
`# reason` written.

## Implementation slices

Five, each ending with `.venv/bin/python -m unittest discover tests` green.
Nothing in the order needs a network or a paid credential.

1. **The label contract.** `MAX_LABEL_CHARS` and the two new guards in
   `tokens.mint`, with their cases in `tests/test_service_tokens.py`. No route
   yet, no auth change. The suite proves the guards before anything can call
   them.
2. **Refusal row 14.** `_check_admin_open` and its `Check(14, ...)` entry in
   `deployment.REGISTRY`, plus the reader that returns the bootstrap value from
   the environment, with cases in `tests/test_service_deployment.py`. Phase 2's
   table is amended in the same commit, so the numbering in the code and the
   numbering in the spec never disagree.
3. **The admin gate.** `BOOTSTRAP` and `authorize_admin` in `auth.py`, with
   cases in `tests/test_service_auth.py`. Still no route. The reviewable
   property of this slice is that `git diff` shows no change inside
   `authorize`.
4. **The routes.** `llmwiki_service/admin.py`, its three handlers, the closed
   error set, the log lines, `tests/test_service_admin.py`, the wiring in
   `build_app`, and both `pyproject.toml` list entries. This is the slice the
   mutation gate must pass with zero `no tests` mutants.
5. **The observed sweep.** Drive the verification cases below against a real
   process, and settle phase 1's mint bullet with a captured log.

## Verification

Observed, never argued, and each test seen failing before its code exists.
None of these needs a network or a paid credential: no admin route calls a
model, so the whole phase is testable against a temporary state directory and a
fixture kb.

- A `POST /admin/tokens` with the bootstrap value in the `Authorization` header
  returns 201, and the returned token then authenticates a read route on a
  deployment with `[access] read = "token"`. This is the whole point of the
  phase and the first thing to break if the gate is wired wrong.
- The same `POST` with a minted `admin` token returns 201. The bootstrap
  credential is not the only key that works.
- The same `POST` with a minted `writer` token returns 401, and with a minted
  `reader` token returns 401. The body is byte identical to the body an absent
  credential gets.
- A `POST` with a credential that is one byte off the bootstrap value returns
  401, and eleven such attempts in one window return 429 on the eleventh with a
  `Retry-After` header. A twelfth attempt against `/kb/<name>/search` from the
  same caller is also refused, proving the two gates share one budget.
- A credential holding a non-ASCII character returns 401 and not 500. The
  comparison runs on bytes.
- The 201 body is searched for the minted secret and holds it exactly once.
  Every record captured from the `llmwiki_service` logger for that request is
  searched for the same string and holds it zero times. This is phase 1's
  open bullet, driven.
- `GET /admin/tokens` returns the minted row, and the raw response body is
  searched for the plaintext token and for the hex hash read directly out of
  the sqlite file. Neither appears.
- A revoked token still appears in `GET /admin/tokens` with a non-null
  `revoked_at`, and is refused on the next read request.
- `DELETE /admin/tokens/<label>` returns 204, and the same call repeated
  returns 404. A `DELETE` for a label nobody minted returns 404 with the same
  body.
- Minting twice with one label returns 409 and `{"error": "duplicate_label"}`,
  and the second call leaves the table with one row for that label.
- A label of 129 characters, a label of `"  "`, a label of `"ops "`, and a
  label holding U+2028 each return 400 and `{"error": "invalid_label"}`, and
  none of them writes a row.
- A `role` of `"owner"`, an absent `role`, and a request body that is a JSON
  list rather than an object each return 400. The role cases answer
  `invalid_role` and the body case answers `invalid_label`.
- `GET /admin/tokens/<label>` returns 404 and `POST /admin/tokens/<label>`
  returns 405, both from the router, both with a JSON body on the closed set.
  There is no route that returns a token by id, and this is the check that no
  one added one.
- A deployment file with `[access] admin = "open"` exits nonzero with the
  setting named, and nothing is listening on the bind address afterwards.
- `.venv/bin/python scripts/check-mutation-gate.py` passes with zero `no tests`
  mutants after slice 4.

## Considered and not taken

**Disabling the bootstrap credential once a live admin row exists.** It reads
as a tidy self-narrowing rule and it is a trap. An operator who loses their
only minted admin token would find the environment variable silently stopped
working, with the only recovery being a hand edit of the sqlite file. Refusal
row 5 already accepts either source, and keeping both live for as long as the
operator sets both is the reversible choice.

**A separate `[admin]` bind address.** Covered above: it adds a key, a refusal,
and a socket, and protects a boundary the operator draws better in front of the
service.

**Hashing the bootstrap value at startup and comparing hashes.** It would let
the comparison reuse `token_hash`, and it protects nothing: the plaintext is in
the process environment either way, which is where the comparison would have to
read it from.

**A `GET /admin/tokens/<label>` detail route.** There is nothing it could
return that the listing does not, except the plaintext, which does not exist
any more by the time anyone could ask.

**Putting the routes in `routes.py`.** That module is 314 lines and its
docstring defines a closed error set for four read routes that take no lock and
write nothing. Admin routes write the token table, run a different gate, and
carry a different error set. One file, two contracts, is how a docstring starts
lying.

## Open questions for the human

These were left open on purpose. Each has a reversible answer picked for now,
named beside it.

1. **Should `check-plan-citations` cover `llmwiki_service`?** Resolved.
   `find_module_file` now resolves a `llmwiki_service/module.py:line` citation
   directly from the repo root, alongside the `CODE_DIRS` lookup it already
   did for `llmwiki` and `tests`. `CODE_DIRS` itself stays `("llmwiki",
   "tests")`: it never gains `llmwiki_service`, because the bare `module.py`
   form would then be ambiguous about which package it names. Every service
   citation in this document now uses the checked prefixed form.
2. **Is losing runtime revocation of the bootstrap credential acceptable?** The
   picked answer is yes, with the mint-then-unset-then-restart lifecycle as the
   mitigation. If it is not, the shape that fixes it is a bootstrap row written
   by an explicit operator command rather than at boot, which is a phase of its
   own.
3. **Is 128 characters the right label cap?** Picked because it is far past any
   human name and far under anything that bloats a log line. The number is
   arbitrary within an order of magnitude.
4. **Should `DELETE` be idempotent?** Picked: 404 on the second call, because
   nothing retries and the operator learns more. A caller that wraps this in a
   script may disagree.
5. **Should `GET /admin/tokens` page?** Picked: no, because the table holds one
   row per human mint. If a deployment ever mints programmatically, this needs
   phase 3's `after` and `n` pair.
