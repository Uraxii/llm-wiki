# Plan 02: llmwiki service

## Context

Plan 01 built a CLI that owns a `.kb` directory on local disk. It assumes the
agent and the files share a filesystem, and that assumption is what makes the
ownership boundary cheap: the agent writes its own page kinds with plain file
tools and the CLI never touches them.

That assumption breaks for three cases the user named: reaching one kb from
another machine, several projects sharing one store, and several agents
ingesting into one kb at once. This plan adds a service for exactly those
cases and changes nothing about the local one.

**Two modes, split by where the wiki lives, not by how it behaves.**

- **Mode 2, local.** The agent calls the CLI, as today. Files on disk, file
  tools work, `grep` works, the ownership boundary is unchanged. Phase 14's
  lock is what makes concurrent agents safe here, so it is required, not
  superseded.
- **Mode 1, remote.** The wiki lives behind an HTTPS endpoint, because there
  is no shared filesystem to fall back on. The agent reads and searches it
  over the API.

A project reaches remote wikis by declaring them in `config.toml`. No new file
and no new discovery path: `resolve_root` already walks up for any `.kb` that
`is_dir()` and checks nothing else (`cli.py:80-83`), and `load_config` returns
`{}` when there is no config file, so a `.kb/` holding only a `config.toml`
with `[remotes]` is already legal and already found.

## Scope

**In:** a `llmwiki-service` package serving one or more kbs over HTTPS; token
auth with reader, writer, and admin roles; `mint`, `list`, and `revoke`;
operator-supplied bootstrap and pepper; the four read endpoints (`search`,
`page`, `list`, `schema`); a `[remotes]` table in `config.toml` and the CLI
side that consumes it; container packaging.

**Out:** writable remotes (the `mode = "write"` field is reserved and rejected
at runtime); merged cross-wiki ranking; OAuth, JWT, and mutual TLS; agent page
authoring over the API; token expiry as a policy (the column is wired and
always null); per-page or per-kind permissions.

## Constraints

- **The service imports `llmwiki`. It never forks it.** Frontmatter parsing,
  the six lint checks, the ownership boundary, and the vector store must have
  exactly one implementation. Two copies drift, and the failure mode is a page
  the service accepts that the CLI rejects.
- **`llmwiki` stays Python 3.14 stdlib plus `sqlite-vec`.** The service may
  take dependencies. This reverses plan 01's blanket rule for the service side
  only, on the user's call: the deployment already ships a venv, so the rule
  was buying less than it cost. The CLI keeps the rule because it is a skill
  tool that must install anywhere.
- **Credentials never appear in `config.toml`.** A remote names the env var
  holding its token, never the token. Unchanged from plan 01.
- **`config.toml` becomes wiki config, not local config.** `[models]`,
  `[identifiers]`, and `[jobs]` are portable and describe the wiki.
  `[endpoint]` is the one section that describes the machine, and it moves
  out. Rung 1 already worked around this by injecting the url from
  `LLM_WIKI_ENDPOINT_URL` at run time so no host was checked in.
- **Restrictive default.** The service refuses writes unless started to allow
  them. Loosening later breaks nobody; tightening later breaks everybody.
- **Only the server decides what a caller may do.** A `mode` in the client's
  config is advisory and is documented as such.
- No em-dashes in prose. The plan 01 ban on security vocabulary is lifted for
  this package, on the user's call, because the subject matter is made of it.

## Alternatives rejected

| Considered | Why not |
|---|---|
| Client-side `mode = "read"` as the control | A client cannot enforce anything about a server. It reads as protection and is not. The server decides; the client field stays as an accident guard |
| A separate `.remote-context` file | `config.toml` is already on the committable side of a kb, and `.kb/` discovery already finds a directory holding nothing else. A second file means a second format and a second parse path |
| Encrypting the token store | Stores a key next to the thing it protects, or moves the secret to an env var that then does all the work. Hashing removes the question |
| Salting token hashes | Salt defeats precomputation and duplicate detection, neither of which exists for a 256-bit random token. It also forces an O(n) scan per request, because a salted hash cannot be looked up by index |
| Argon2 or another slow KDF for tokens | Costs 50 to 100ms and tens of MB per verification, on every request, and hands an unauthenticated caller a cheap way to exhaust the service. Slow hashes are for low-entropy secrets. Reserved for passwords if they ever exist |
| Hiding local files behind the API in both modes | Costs the agent-owned half of the wiki: `grep`, `cat`, and direct authoring of `topic`, `correction`, `ruling`, and `index.md`. Page CRUD over HTTP is a filesystem reimplementation |

## Phases

1. [tokens and the auth gate](phase-01-tokens.md)
2. [TLS, certificate lifecycle, and the deployment file](phase-02-tls.md)
3. read endpoints: `search`, `page`, `list`, `schema`
4. `[remotes]` in `config.toml`, and the CLI client that consumes them
5. container packaging and the mode 1 deployment

Phase 1 is the only one with a nontrivial design. Phases 3 and 4 are
independent of each other once 1 and 2 land.

## Verification

- The service's own suite, plus `llmwiki`'s existing suite unchanged and still
  hermetic under the dead-proxy run.
- A restrictive-default proof: a service started without the write flag
  refuses a writer token's write, and the refusal is observed, not argued.
- A boundary proof: the service and the CLI agree on every page in a real
  corpus copy. Any page one accepts and the other rejects is a defect in the
  shared core, not in either caller.
