[overview](overview.md)

# Phase 1: tokens and the auth gate

**Goal.** The service can tell who is calling and what they are allowed to do,
the maintainer sets that policy in a deployment file, and an operator can mint
and revoke keys by friendly name without ever handling a raw token twice.

**Problem.** Nothing in plan 01 has a caller. The CLI runs as whoever invoked
it and the filesystem is the whole permission model. A service has callers on
a network, so it needs three things plan 01 never needed: a way to recognise a
caller, a policy saying what each kind of caller may do, and a way to take
access away again.

The failure mode to design against is not a clever attack. It is the ordinary
one: a token pasted into a repository or a log, noticed weeks later, with no
way to tell which key it was or to turn it off without turning everything off.
That is what the friendly names, the `last_used` column, and revoke are for.

**Approach.**

*Three roles, and admin is honestly a superset.* `reader`, `writer`, `admin`.
It is tempting to say admin only mints tokens and cannot write pages, but an
admin can mint itself a writer token, so the separation would be decorative.
State plainly that admin is total, and keep admin tokens few.

*The deployment file sets the policy, not the client.*

`[access]` is defined in phase 2, which holds the whole deployment file. It is quoted here only to
show the shape the gate reads:

```toml
[access]
read  = "open"     # or "token"
write = "token"    # always "token"; "open" is refused at startup
admin = "token"
```

`read = "open"` is the expected setting for a home or team deployment and is
why the reader role exists at all: it is for the deployment that wants reads
closed too.

*Token format, and the prefix earns its place twice.*

```
llmwiki_<id>_<secret><crc>
        ^^^^         ^^^^^^ secrets.token_urlsafe(32)
        row id,                    ^^^^^ base62 CRC32 over everything before it
        not secret
```

Three segments, each earning its place:

- The `llmwiki_` prefix makes a leaked token machine-detectable, which is what
  lets secret scanning catch one pasted into a repository.
- The `<id>` segment keeps verification to a single indexed lookup instead of a
  table scan.
- The `<crc>` suffix lets a scanner reject a candidate without calling the
  service, which is what keeps secret-scanning false positives down. It is a
  checksum and not a signature: it proves the string is well formed, never that
  it is valid. The service still verifies the hash, and a token that fails its
  own checksum is rejected before any database work, which also keeps malformed
  input off the lookup path.

The format is settled here because it cannot be changed later without
invalidating every key ever minted.

*Fast keyed hash, and the pepper is a hash parameter rather than a bolt-on.*

```python
hashlib.blake2b(token.encode(), key=pepper, digest_size=32).hexdigest()
```

BLAKE2b is modern, faster than SHA-256, and keyed natively. `cryptography`'s
HMAC-SHA256 is the same construction if a library is preferred; dependencies
are allowed here and this one is not needed. A slow KDF is the wrong primitive:
verification runs on every request, and a 50 to 100ms memory-hard hash is both
a latency tax and a way for an unauthenticated caller to exhaust the service.
Argon2 is reserved for passwords, which do not exist in this design.

*Comparison is `secrets.compare_digest`.* Never `==`.

*The operator supplies two secrets at startup*, both from the environment: the
pepper, and the first admin token. Minting requires admin, so without the
second there is no way to make the first key. Neither is ever written to disk
in plaintext and neither appears in any log line.

**Changes.** A new `llmwiki_service` package. Nothing in `llmwiki` changes in
this phase.

**Data structures.** One sqlite table. It holds no plaintext token, so a
stolen copy yields nothing usable and needs no encryption.

| column | type | why |
|---|---|---|
| `id` | text, primary key | the `<id>` segment; makes verification one indexed lookup |
| `token_hash` | text | keyed BLAKE2b of the full token |
| `pepper_version` | integer | which pepper produced this hash |
| `label` | text, unique | the friendly name; what an operator types to revoke |
| `role` | text | `reader`, `writer`, or `admin` |
| `created_at` | text | |
| `last_used_at` | text, null | the most useful operational column: it is how a stale or unexpectedly active key gets noticed |
| `expires_at` | text, null | wired now, always null. Checked as `expires_at IS NULL OR expires_at > now`, so turning expiry on later is a policy change and a data write, never a migration |
| `revoked_at` | text, null | |

`pepper_version` is the column that makes rotation possible in practice.
Without it, changing the pepper invalidates every token at once, which means
the pepper never gets rotated. With it, the service accepts two peppers during
a window and re-hashes each token on next use.

**Three admin endpoints, not one.** `mint` returns the plaintext exactly once
and there is no endpoint that can return it again. `list` shows labels, roles,
and `last_used_at`, never hashes. `revoke` takes a label. Mint without revoke
means a leaked key is permanent, which is not an operable system.

**Operational rules that are part of the design, not polish.**

- Never log a token. Log the label or the `<id>` segment. This applies to
  error messages, which is where they usually leak.
- Rate-limit authentication failures.
- A failed auth returns the same response whether the id was unknown or the
  secret was wrong.

**Verification.** Observed, never argued. Each of these is a test that was
seen failing before the code existed, per the standing rule from plan 01 that
a guard never seen failing proves nothing.

- The gate opens only `read`, and only on the exact word `open`. A write is
  refused whatever `[access]` says, because phase 2 offers no way to open one
  and a deployment file that slips past its own startup checks must still not
  get a write through. An earlier version of this bullet asked for a writer
  token to be refused by "a service started without the write flag"; no such
  flag exists in phase 2's `[access]`, and the running-service half of that
  proof belongs to phase 2's startup refusals.
- A revoked token is refused on the next request, and the refusal is
  indistinguishable from an unknown one.
- A token minted under pepper version 1 still verifies after the service is
  restarted with pepper 2 configured as current and 1 as accepted, and its row
  shows `pepper_version = 2` afterwards.
- `list` output contains no hash and no plaintext, checked against the raw
  response body rather than a rendered view.
- The full response body and every log line produced during a mint are
  searched for the minted secret. It appears exactly once, in the mint
  response, and nowhere else.
- `expires_at` set to a past value refuses, proving the column is wired even
  though policy never sets it.

**Considered and not taken.** Encoding the role in the visible prefix, as
`llmwiki_w_` and so on. It would let an operator eyeball a leaked token's blast
radius, but the server is the only authority on a token's role anyway, and a
prefix that disagrees with the database after a role change is worse than no
prefix at all.
