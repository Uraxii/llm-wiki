[overview](overview.md)

# Phase 5: container packaging and the mode 1 deployment

**Goal.** One image runs the service, an operator starts it with a deployment
file, a kb volume, and three environment variables, and `[endpoint]` stops
living in `config.toml` in both modes.

**Problem.** Phases 1 to 4 describe a program nobody can run. There is no
image, no place for the token database to live, no ingesting process behind a
remote kb, and no answer to the constraint the [overview](overview.md) has
carried since it was written: `config.toml` describes a wiki, `[endpoint]`
describes a machine, and it has to move out. Phase 4 declined the move as out
of scope and was right to. Nothing after phase 4 exists, so this phase owns it.

Five things go wrong here and each gets an answer below: a secret gets baked
into an image layer, the token database is written inside the container and
every token dies on the next restart, the kb volume is mounted read-only and
the first `search` fails on a directory it has to create, `[endpoint]` gets a
second home and then a precedence rule, and mode 1 ships a service over a kb
that nobody ever ingests into, which phase 3 already flagged: a remote kb
nobody sweeps answers 503 from `search` forever.

## `[endpoint]` moves to the environment, in both modes

The table is small and has exactly two readers. `model._endpoint_url`
(`model.py:88`) reads `[endpoint].url`, and `summarize._pdf_part`
(`summarize.py:132`) reads `[endpoint].pdf_part`. Both become environment
reads:

| Was | Becomes | Absent |
|---|---|---|
| `[endpoint] url` | `LLM_WIKI_ENDPOINT_URL` | the same `ModelError` raised today, naming the variable instead of the file |
| `[endpoint] pdf_part` | `LLM_WIKI_ENDPOINT_PDF_PART` | `"file"`, unchanged from today's default |

**Why not the deployment file, which is the obvious candidate.** It is the
right shape and the wrong scope. The deployment file exists only where the
service does, and `summarize`, `dedup`, and `embed` need an endpoint url in
mode 2, where there is no service and no such file. Putting `[endpoint]` there
means the value has two homes, which means a precedence rule between them,
which means an operator debugging a wrong model has two files and one rule to
check. One home in both modes costs one variable and no rule at all.

**Why the environment and not a new machine-level file.** A file would need a
path convention, a discovery walk, a parse, and a refusal for every malformed
case, all for two scalars. The environment already carries every other
machine-scoped value this project has: the model credential
(`model.API_KEY_VAR`, `model.py:17`), phase 1's pepper, and phase 1's bootstrap
admin token. Rung 1 has already been running this exact arrangement for the
url, injecting `LLM_WIKI_ENDPOINT_URL` at run time so no host is checked in.
This phase deletes that workaround by making it the design.

A machine-level file would also be the second file this plan has refused. The
[overview](overview.md) rejects a `.remote-context` file for remotes on the
same reasoning, and a file introduced here for `[endpoint]` is exactly where a
`[remotes]` table would land next. There is no such file, so remotes cannot
drift into one.

**No `LLM_WIKI_ENDPOINT_URL_FILE` variant.** The url is not a credential. The
file variant exists for the API key because a key in the environment is
readable from a process listing and a crash dump, and a url is neither secret
nor worth a second code path.

### A legacy `[endpoint]` refuses, loudly, in `core.load_config`

`core.load_config` (`core.py:98`) raises `ValueError` when the parsed table
holds an `endpoint` key, naming the config path and the two variables that
replace it. It already raises on a malformed file, so this is one more refusal
on a path every caller shares rather than a new mechanism.

Refusing beats ignoring, and the reason is the failure it prevents. A silently
ignored `[endpoint]` leaves a kb that keeps working while pointing at whatever
model the ambient environment names, or at nothing. The pages it writes are
wrong in a way no lint check sees, because a summary written by the wrong model
is a valid summary. The operator finds out weeks later, from the prose.

The cost is real and accepted: every verb fails on such a kb, `lint` and
`status` included, until two lines are deleted from a file. That is loud, it
is one edit, and the message says which edit. A refusal narrowed to `model.py`
would let `lint` pass on a kb whose next `summarize` cannot run, which trades
a clear failure now for a confusing one later.

The service inherits this for free. It imports `llmwiki`, so a kb in `[kbs]`
carrying a legacy section fails at startup, alongside every other
misconfiguration in [phase 2](phase-02-tls.md)'s refusal table, which is where
that refusal is defined.

**`core.load_config` refuses and never rewrites.** The ownership boundary makes
`config.toml` the user's file. No verb edits it, and this phase adds none.

### Migrating a `.kb` that exists on disk

Real ones exist. The path is three steps and no tooling:

1. Run any verb. The refusal names the file and the two variables.
2. Read the url out of `config.toml`, export it as `LLM_WIKI_ENDPOINT_URL` in
   the shell profile, the OS scheduler entry, or the container's environment,
   next to `LLM_WIKI_API_KEY`. Do the same for `pdf_part` if the file set one.
3. Delete the `[endpoint]` section.

No migration script. Two scalars, a handful of kbs, and a refusal that names
the edit is under the bar where a script pays for itself, and a script that
rewrites `config.toml` would cross the ownership boundary to save one deletion.

`CONFIG_TOML` (`cli.py:29-30`) loses its commented `[endpoint]` block and gains
one comment line naming the two variables, so a kb created after this phase
never carries the section that would refuse.

## The image

One image, one process, one architecture.

**Base.** A slim Python 3.14 base built on glibc. Not a musl one, and the
reason is stronger than a slow build. `sqlite-vec` 0.1.9 publishes five files
and every one of them is a wheel: macOS x86_64, macOS arm64, manylinux
aarch64, manylinux x86_64, and Windows x86_64. There is no musl wheel, and
there is no source distribution either, so on a musl base the dependency does
not install and there is nothing to fall back to, not even a local compile.
Verified against the package index rather than assumed.

Those wheels are tagged `py3-none-<platform>`, which binds an architecture and
a C library and does not bind a Python ABI. So the base has to be 3.14 because
`pyproject.toml` requires it, not because the wheel does. Build for the
architecture you run. A cross-architecture build needs the wheel for that
architecture and is not specified here.

**One build stage.** Every dependency resolves to a prebuilt wheel on this
base, so there is no compiler in the image to drop and nothing for a second
stage to leave behind. Add one the day a dependency needs a toolchain, not
before.

**One distribution, installed with an extra.** The service ships as
`llmwiki_service` inside this repo's existing distribution, reached with
`pip install '.[service]'`, rather than as a second distribution that depends
on `llmwiki` by version. That is the packaging half of the constraint the whole
plan rests on: one install, one copy of the seven lint checks and the vector
store, and no version pin that can drift because there is no second version.

**In the image:** the venv at a fixed path, `llmwiki`, `llmwiki_service`, and
their pinned dependencies.

**Not in the image:** any kb, any certificate, any deployment file, the pepper,
the bootstrap admin token, the model key, `tests/`, `docs/`, the repo's `.git`
directory, and the developer `.venv`. A build-context ignore file keeps the
last four out of the context itself, not just out of the layers. A build
context is copied wholesale before anything is selected from it, and a
developer checkout can hold a real `.kb` full of the user's own notes.

**Runs as an unprivileged user**, created in the image, with a fixed uid so the
volume ownership on the host is stateable. The image never switches back to
root, and nothing in it needs to: the service binds 8443, which is above 1024.

**Entry point, in exec form**, running the venv interpreter as
`python -m llmwiki_service <deployment-file>`, which is the invocation
[phase 2](phase-02-tls.md) pins. The mounted path of the deployment file is
that one required argument, and there is nothing else on the command line. No
shell wrapper and no init process. The reason is phase 2: `SIGHUP` forces a
certificate reload, and a shell as PID 1 that does not forward signals makes
that route dead and the reload untestable. Exec form makes the interpreter PID
1, so `SIGHUP` and `SIGTERM` reach it unchanged.

No entry point script that migrates, seeds, or mints anything at boot. The
token table is created on first open, and the bootstrap admin token comes from
the environment, per phase 1.

**Health check.** The image declares one, running the same module the same way
with one added flag: `python -m llmwiki_service --healthcheck
<deployment-file>`. It takes the deployment file as the same required
positional argument [phase 2](phase-02-tls.md) pins, so the address and the TLS
mode are never duplicated into the image. It connects to the bind address over
loopback and requires a 200 from `/health`.

Two deliberate limits on it. It sends no token, so `/health` stays the only
route reachable without one, and no credential lives in a probe. It does not
verify the certificate chain and does not check the expiry, because the probe
never leaves the container's network namespace and because liveness is all
`/health` reports. A probe that failed on an expired certificate would restart
a container that phase 2 then refuses to start, turning a serving deployment
into a loop it cannot exit.

## What the container mounts, and why the kb is read-write

The image holds no data. Four mounts, and the deployment file's paths are the
mount points.

| Mount | Mode | Holds |
|---|---|---|
| kb roots, one per `[kbs.<name>] path` | read-write | the wikis |
| `[server] state` | read-write | the token database |
| `[tls] cert` and `key` | read-only | the certificate and its key |
| the deployment file | read-only | every section in [phase 2](phase-02-tls.md)'s listing |

**The kb mount is read-write even for a read-only deployment.** Phase 3 records
the one write that survives on the read path: `_connect` creates `vectors/` if
it is absent (`vectors.py:76`), so the first `search` against a kb that has
never been embedded fails on a read-only mount. The sweeping job below writes
there in earnest anyway. A read-only kb mount is not a supported deployment,
and the startup check below refuses it rather than letting it fail on the first
request.

**`[server] state` is what phase 1 needs, and it is defined in
[phase 2](phase-02-tls.md).** Phase 1 defines the token table and says nothing
about where the file lives. Left unstated, it lands inside the container's own
filesystem, and every minted token dies on the next image update with no error
anywhere: the table is created empty on first open, so the service starts clean
and every existing token reads as unknown, which phase 3 makes
indistinguishable from revoked. That is the failure the mount above prevents,
and it is why the key exists.

**Two of phase 2's startup refusals are here for this container's sake.**
`[server] state` missing or unwritable, and any `[kbs.<name>] path` unwritable,
both refuse at boot. Both are boot-time detections of a fault that otherwise
surfaces as a confusing request-time failure, which is the standard phase 2
already set: an unwritable state directory loses every token on the next
restart, and an unwritable kb root fails the first `search` against a
never-embedded kb on `mkdir`, where nothing else in the deployment reports it.

## How a credential reaches the container without being stored in it

Three secrets: the model key, phase 1's pepper, and phase 1's bootstrap admin
token. All three arrive at process start from the container runtime's
environment, from the operator's own secret store, and none of them is written
in the repo, the image, or the deployment file.

- **Never a build argument.** A build argument is recorded in image metadata
  and survives in the layer history, so a key passed at build time is a key
  published with the image.
- **Never an image environment declaration**, for the same reason: it is a
  layer.
- **Never an environment file committed to this repo.** An operator's file
  lives outside the checkout, and the build-context ignore file above keeps a
  stray one out of the context.
- **Prefer the file form for the model key.** `model.API_KEY_FILE_VAR`
  (`model.py:18`) names a path, so a secret mounted as a file never enters the
  process environment, where a process listing, a crash dump, and every child
  process can read it. The path is a mount, not a layer.
- The pepper and the bootstrap admin token have no file form in phase 1 and
  none is added here. Phase 1 already forbids logging them, and phase 2 already
  refuses to start without a pepper.

Nothing here weakens the standing rule. Credentials come from the environment
only, never from `config.toml`, never from the deployment file, never printed,
and never in an error message.

## Mode 1 needs a writer, and it is the same image on a schedule

Phase 3 left this owed: mode 1 has no ingesting process unless the deployment
ships one, and a remote kb nobody sweeps answers 503 from `search` forever,
because pages accumulate with no vector.

The writer is the CLI, in the same image, run as a separate short-lived
container against the same kb volume, started by the host's own scheduler:

```
<runtime> run --rm \
  --env-file <operator's env file, outside the repo> \
  --volume /srv/kb/homelab:/srv/kb/homelab \
  <image> /opt/llmwiki/.venv/bin/python -m llmwiki --kb /srv/kb/homelab ingest --job nightly
```

**What replaces the OS scheduler entry: nothing.** There is still an OS
scheduler, on the host, exactly as in mode 2. What changes is one field. The
entry named the venv interpreter path on the host, per the standing rule that
`[jobs]` holds `feed` XOR `urls` plus `mode` and never an interpreter. Now it
names the image and the verb, and the interpreter path is baked into the image
where it cannot drift from the venv it belongs to. `[jobs]` is unchanged, still
read from each kb's `config.toml` by `ingest.run_job` (`ingest.py:173`).

Four consequences, stated rather than discovered:

- **No scheduler inside the service.** A thread that ingests inside the service
  process makes the read path a writer, has to hold phase 14's lock while
  serving reads, and turns a model endpoint outage into a service that looks
  unhealthy. One image with two invocations costs nothing and keeps the reader
  and the writer in separate processes with separate exit codes.
- **The job container and the service container both need
  `LLM_WIKI_ENDPOINT_URL` and the model key.** The service needs them because
  `search` embeds the query. This is the same environment for both, which is
  the point of the section above.
- **Concurrency is already handled.** The job writes while the service reads.
  Phase 14's `core.kb_lock` (`core.py:66`) serializes writers, and phase 3
  establishes that read routes take no lock and see content at most one write
  stale. A job run that finds the kb busy exits nonzero and the scheduler's
  next tick runs it again. Nothing retries in process.
- **A deployment with no scheduler is a read-only archive**, and that is a
  legitimate deployment as long as the operator knows it: pages already
  embedded answer, and any page added by hand later makes `search` return 503
  until something runs `embed`.

## One replica, inherited as a known cost

Phase 3's `search` rate limit is an in-process counter, and it says phase 5
must either run one replica or move the counter out. This phase runs one
replica. N replicas behind one address means N times the model budget the
limit was written to cap, and moving the counter out means a shared store, a
second network dependency, and a failure mode where the limiter's outage takes
the read path down.

So: one container per deployment, no replica count, no load balancer, and no
orchestrator. The limit stays honest because there is one process to count in.
A deployment that outgrows one process has outgrown this plan, and the fix is a
shared counter designed then, against a measured load.

## What phase 5 does not do

- No orchestration platform, no service mesh, no autoscaling, no replica set,
  and no second service.
- No image publishing pipeline, no registry, no signing, and no
  multi-architecture build matrix.
- No in-process scheduler, and no job queue.
- No certificate issuance. Phase 2 keeps renewal external and this phase mounts
  the result.
- No change to any phase 3 route, request shape, response shape, or status
  code, and no write route.
- No new `[remotes]` home. Remotes stay in `config.toml`, per phase 4.
- No token expiry, and no change to phase 1's token format or table.
- No migration script, and no verb that writes `config.toml`.
- No `[endpoint]` section in the deployment file, and no fallback to a
  `config.toml` value for compatibility. The refusal is the compatibility
  story.

## Changes

`llmwiki`: `model._endpoint_url` and `summarize._pdf_part` read the
environment; `core.load_config` refuses a `config.toml` holding `[endpoint]`;
`CONFIG_TOML` drops the commented section and names the two variables;
`pyproject.toml` gains a `service` extra. `tests/fixtures/rung1/run_rung1.py`
loses its url injection, which this phase makes redundant.

`llmwiki_service`: the `--healthcheck` flag. The `[server] state` key and the
two startup refusals this phase depends on are defined in
[phase 2](phase-02-tls.md).

Packaging: a container build file, a build-context ignore file, and a
deployment example holding the file, the mounts, the environment, and the
scheduler entry for the job container. No secret in any of them.

## Verification

Observed on a running container, never argued. Each test is seen failing before
its code exists.

**The `[endpoint]` move.**

- A `config.toml` holding `[endpoint]` makes every verb exit nonzero with a
  message naming the file and both variables. Drive `lint`, `status`, and
  `summarize`, because a refusal wired only into the model path passes on
  `summarize` alone.
- With the section deleted and `LLM_WIKI_ENDPOINT_URL` exported, `summarize`
  reaches the fake endpoint, and the request goes to that url. Read the url
  back off the recorded request rather than off the config.
- `LLM_WIKI_ENDPOINT_URL` unset raises the same error class as today, naming
  the variable and not the file.
- `LLM_WIKI_ENDPOINT_PDF_PART` unset behaves as `pdf_part = "file"` did, and an
  unrecognized value refuses with the same message shape.
- A kb created by `init` after this phase holds no `[endpoint]` section, and
  loading its config raises nothing.
- The service refuses to start when a kb in `[kbs]` holds a legacy section, and
  names that kb in the log and in nothing else.

**The image.**

- The built image runs `llmwiki search` and `import sqlite_vec` under its own
  interpreter, proving the compiled wheel matches the base and the
  architecture. A pure-Python fallback would pass a smoke test and fail this.
- The image contains no `.kb`, no `tests/`, no `.git`, and no file holding any
  of the three secrets. Search the built layers, not the build context.
- Image metadata and layer history contain no build argument and no environment
  declaration naming a credential.
- `SIGHUP` to the container reloads the certificate, per phase 2's reload
  proof. A shell wrapper as PID 1 fails this and only this.
- `SIGTERM` stops the container without the runtime's kill timeout expiring.
- The health check reports healthy on a running service and unhealthy within
  one interval after the process is stopped.
- The health check makes no authenticated request: with `read = "token"` set
  and no token in the container's environment, it still reports healthy.

**State and mounts.**

- A token minted before a container restart still verifies after it, with
  `[server] state` mounted. Without the mount, the same test fails, and it is
  run both ways.
- A missing or unwritable `[server] state` refuses at startup, naming the
  setting.
- A read-only kb mount refuses at startup. Without the refusal, the fault
  appears as a 503 or a 500 from the first `search` against a never-embedded
  kb, so drive that case too and confirm the startup refusal fires first.
- The service, running as the unprivileged user, reads a kb owned by the
  mounted uid and writes `vectors/` in it.

**The writer.**

- A job container run against the same volume ingests, and a `search` through
  the running service returns the new page afterwards, with no restart. This is
  the whole point of the section and nothing else observes it.
- Starting the job container while a second job container is mid-write gives a
  nonzero exit and no damage on disk, per phase 14.
- A `search` served while a job container is writing returns a complete
  ranking, never a partial page and never a 500.
