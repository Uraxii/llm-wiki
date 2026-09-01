[overview](overview.md)

# Phase 2: TLS, certificate lifecycle, and the deployment file

**Goal.** The service listens only on TLS, an operator can renew a
certificate without dropping the service, and a misconfigured deployment
refuses to start instead of starting insecurely.

**Problem.** Phase 1 puts a bearer token on every write and every closed read.
That token crosses the network on every request, so without TLS the whole auth
design is decoration: anyone on the path reads the token and becomes the
caller. This phase is what makes phase 1 mean anything.

Three things usually go wrong here and each gets an explicit answer below: a
certificate renews on disk and the service keeps serving the old one until it
expires, a deployment sits behind a proxy and starts trusting forwarded
headers a client can forge, and a bad config starts a service that looks
healthy and is not.

**Approach.**

*Do not serve plain HTTP at all, not even to redirect.* A redirect from HTTP to
HTTPS is a usability affordance for browsers. This is an API with programmatic
clients, and a redirect means the first request already went out in the clear
with the token in it. There is no port 80 listener. A client that gets the
scheme wrong gets a connection refused, which is the correct and loud outcome.

*Reconsider `http.server`.* Plan 01 would have forced it, but the standard
library's own documentation says `http.server` is not recommended for
production because it implements only basic security checks. That constraint
is lifted for this package, so the argument for it is gone. The service runs on
a real server: `uvicorn` with a small `starlette` app, seven routes total, four
read and three admin. The application stays framework-light on purpose, so the
dependency is a server and a router, not an ecosystem.

*Terminate TLS in-process by default, and support upstream termination
explicitly.* Most real deployments end up behind something, whether that is a
reverse proxy in a homelab or an ingress in a cluster. Supporting both is
nearly free; discovering later that only one works is not. `[tls] mode` picks
between them, and the whole `[tls]` section is listed with the rest of the
deployment file below.

*Trust forwarded headers only when told to.* Under `mode = "upstream"` the
service still needs the caller's scheme and address for logging and rate
limiting, and those arrive in `X-Forwarded-Proto` and `X-Forwarded-For`. A
client can forge both. They are read only when `trusted_proxy` is set, the
request's real peer is that address, and the value read is the rightmost
element that parses as an IP address. Rightmost because the mainstream proxy
configuration appends the peer it observed to whatever the client sent, so
every element to the left of the last one is still client-supplied. A deployment that sets `mode = "upstream"` without
`trusted_proxy` gets addresses it can trust and no scheme, which is the safe
default rather than the convenient one.

*Do not hand-write a cipher list.* `ssl.create_default_context(
ssl.Purpose.CLIENT_AUTH)` gives better defaults than almost any list written by
hand, and it improves as Python does. Set the minimum version and stop there.

**Certificate lifecycle, the part that actually bites.**

Certificates are operator-supplied files on a mounted volume. Renewal is
external: certbot on the host, cert-manager in a cluster, or a private CA for a
homelab deployment, which is the realistic case for the user's own use. In-
process ACME is deliberately out of scope: it needs port 80 or a DNS challenge,
writable state, and rate-limit handling, and it would be the largest thing in
this package by a wide margin.

What the service owes the operator is **reload without downtime**. The failure
this prevents is the common one: certbot renews the file at 3am, the service
keeps the certificate it loaded at boot, and it serves an expired certificate
until a human notices.

- The service watches the certificate and key file paths.
- On change, it builds a new `SSLContext` and swaps it in. Existing connections
  finish on the old one; new connections get the new one.
- A reload that fails to build a valid context is logged loudly and the
  previous context is kept. A broken renewal must not take the service down.
- `SIGHUP` forces the same reload, so an operator never has to wait for or
  trust the watch.

**The deployment file, defined here and nowhere else.** One file, supplied by
the maintainer, holding everything that describes this deployment rather than
any wiki. It is the answer to the split recorded in plan 02: `config.toml`
describes a wiki, this file describes a machine.

Every key below is defined here, and so is every startup refusal in the table
that follows. Later phases argue for the keys they need and point back at this
listing. None of them adds a key or a refusal of its own, because a deployment
file specified in four documents is a deployment file nobody can read.

*The service is invoked as `python -m llmwiki_service <deployment-file>`.* Not
a console script. `pyproject.toml` does declare one for the CLI, but every
documented invocation of `llmwiki` is `.venv/bin/python -m llmwiki`, so the
module form is the one an operator has already read. It also works the same
from a checkout and from an install, where a console script needs the
distribution installed before the name exists.

The deployment file is a required positional argument with no default path. A
service that falls back to `/etc/llmwiki/service.toml` starts against a file
nobody named, which is the restrictive-default rule read backwards: nothing
should ever serve a deployment the operator did not point at. There is no
search path, no working-directory lookup, and no environment variable holding
the path.

```toml
[server]
bind  = "0.0.0.0:8443"
state = "/var/lib/llmwiki"      # holds tokens.sqlite; mount it or lose every token

[tls]
mode        = "terminate"       # or "upstream"
cert        = "/etc/llmwiki/tls/fullchain.pem"
key         = "/etc/llmwiki/tls/privkey.pem"
min_version = "1.2"             # 1.3 where every client supports it
# trusted_proxy = "10.0.0.1"    # unset by default, and forwarded headers are ignored while it is

[access]
read  = "open"                  # or "token"
write = "token"                 # always "token"; "open" is refused at startup
admin = "token"

[limits]
search_per_minute = 30          # a budget guard, not a defense

[kbs.homelab]
path = "/srv/kb/homelab"

[kbs.recipes]
path = "/srv/kb/recipes"
```

`[kbs]` is what phase 3's routes hang off: one service serves several kbs, and
the table name is the name in the URL, so `/kb/homelab/search`. Defining it
here rather than in phase 3 keeps the deployment file settled in one place.
`[limits] search_per_minute` caps what one caller can spend on `search`, and
phase 3 argues why a search read needs a cap at all. `[server] state` is where
the token database lives, and phase 5 argues what it costs to leave it
unmounted.

Secrets are not in this file. The pepper and the bootstrap admin token arrive
from the environment, unchanged from phase 1: `LLM_WIKI_PEPPER` and
`LLM_WIKI_BOOTSTRAP_ADMIN_TOKEN`. Phase 1 promised the second one without
naming it, which left every later phase free to guess a different name, so it
is named here beside the refusal that reads it. `[endpoint]` is not in this file
either. It lives in the environment in both modes, per phase 5, so it has one
home rather than two and no precedence rule between them.

**Fail closed at startup.** The service refuses to start, with a message naming
the setting or the path at fault, when any of these hold. None of them are
warnings, and every one of them is checked before a socket is bound. The first
three fire before the file is parsed at all.

| Condition | Why refusing beats starting |
|---|---|
| no deployment file argument | the alternative is a default path, and a service that starts against a file nobody named |
| the deployment file path does not exist | otherwise it reads as an empty deployment, and an empty deployment is every default at once |
| the deployment file exists and cannot be read | a permissions fault at boot is a fixable mistake; the same fault discovered as a missing setting is not |
| no pepper supplied | every token would fail to verify, silently, at request time instead of at boot |
| no bootstrap admin token and no existing admin row | an unadministrable service |
| `[access] write = "open"` | there is no deployment where this is intended |
| `[tls] mode` absent, or not exactly `terminate` or `upstream` | four of the rows below only fire under a named mode, so any other value switches them all off and leaves the service on the plaintext branch, which is what the private-interface row below exists to prevent |
| `mode = "terminate"` and the certificate or key is missing or unreadable | otherwise the failure surfaces on the first request |
| certificate is expired at boot | it will not fix itself, and it is better known now |
| `mode = "upstream"` and no listener address is bound to a private interface | prevents accidentally exposing the plaintext port to the network |
| `[access] read = "open"` and `mode = "upstream"` and `trusted_proxy` is unset or is not an IP address | every caller then arrives from the proxy's address, so `[limits]` collapses into one global bucket. A hostname is refused with it: forwarded headers are trusted by comparing this value against the transport peer, which is always a literal, so a name would pass the check and be ignored by the service. Phase 3 argues it |
| `[server] state` missing, or not writable by the running user | the token database is silently recreated empty, and every token minted before the restart reads as unknown. Phase 5 argues it |
| any `[kbs.<name>] path` not writable by the running user | the first `search` against a never-embedded kb fails on `mkdir`, and nothing else in the deployment reports it. Phase 5 argues it |
| a kb in `[kbs]` whose `config.toml` still holds a legacy `[endpoint]` section | the kb would keep working against whatever model the ambient environment names, and a summary written by the wrong model is a valid summary. Phase 5 argues it |

**An unauthenticated health endpoint.** `/health` returns liveness only: no kb
names, no version, no counts, nothing that describes the deployment. Containers
need it, and it is the one route that must work before auth does.

**Changes.** `llmwiki_service`: the `__main__` entry point and its one
positional argument, the TLS context builder and its reload watch, the
deployment file parser, the startup refusal checks, and `/health`. Nothing in
`llmwiki` changes.

**Verification.** Observed on a running service, never argued. Each test is
seen failing before its code exists.

- A plain HTTP request to the TLS port fails, and nothing is listening on 80.
- A certificate replaced on disk is served to a new connection without a
  restart and without dropping an in-flight request. This is the reload proof
  and it is the reason the phase exists; prove it by swapping in a certificate
  with a different subject and reading the subject back from a fresh client.
- A renewal that writes a malformed certificate leaves the service up on the
  previous context, and logs it.
- With `mode = "upstream"` and no `trusted_proxy`, a request carrying a forged
  `X-Forwarded-Proto: https` and a forged `X-Forwarded-For` is not believed.
- Each startup refusal in the table above is driven once, and the process exits
  nonzero with the setting or the path named in the message. The three argument
  refusals are driven too: no argument at all, a path that does not exist, and
  a path that exists and cannot be read. Confirm nothing is listening on the
  bind address afterwards, because a refusal that fires after the socket is
  bound is a refusal that already accepted a connection.
- `/health` answers before any token exists, and its body is checked to contain
  no kb name, no path, and no version string.
