# Multi-provider model configuration

Status: implemented. Written against `ba3ae6e` with the suite green at 760
tests, and landed by `b9e191d` and `515fe38` with the suite green at 790.

Today one `[endpoint]` table serves every model in `[models]`. This design
replaces it with a `[providers]` table and makes each model id name the
provider that serves it. A summarize model on a hosted endpoint and an embed
model on the machine under your desk stop being mutually exclusive.

This is a rewrite of the config boundary, not a second path beside the old
one. There is no back-compat shim: a config still holding `[endpoint]` is
refused with a message saying what to write instead. Migrating existing kbs
is a later ticket.

`docs/plans/02-llmwiki-service/phase-05-packaging.md` lines 24 to 105 planned
the opposite move, `[endpoint].url` out to a `LLM_WIKI_ENDPOINT_URL`
environment variable. That plan is unbuilt and cancelled by user directive.
Do not design toward it. Providers are wiki configuration and live in
`config.toml`; only credential values live in the environment.

## The new schema

```toml
# One model per paid pipeline step. Each id is "<provider>:<model>",
# naming a table under [providers] below and a model that provider
# serves. The prefix is always required.
[models]
summarize = "hosted:your-summarize-model"
embed = "desktop:your-embed-model"
dedup = "hosted:your-judge-model"
# summarize_image = "hosted:your-vision-model"

# One table per endpoint the ids above name.
#
# key_env names the environment variable holding that provider's API
# key. key_file_env names one holding a path to read the key from. Set
# neither and no Authorization header is sent, which is what a server
# on your own machine usually wants. A key value never appears here.
#
# pdf_part is how this endpoint takes a PDF: "file", "image_url", or
# "none" to never send one. It defaults to "file".
[providers.hosted]
url = "https://api.example.com/v1"
key_env = "LLM_WIKI_API_KEY_HOSTED"
pdf_part = "file"

[providers.desktop]
url = "http://127.0.0.1:1234/v1"

[identifiers.isbn]
pattern = "^\\d{13}$"
describe = "13-digit ISBN without hyphens"
```

`[identifiers]`, `[jobs]`, `[remotes]`, and `[endpoint_service]` are
untouched.

Recognized keys in a provider table: `url`, `key_env`, `key_file_env`,
`pdf_part`. Any other key is an error, matching how `remotes._remote`
rejects an unknown key rather than ignoring it.

### Why `pdf_part` moves onto the provider

`pdf_part` says which content-part shape one endpoint accepts a PDF as. That
is a fact about the endpoint, not about the kb, so with more than one
endpoint in play a single global value is wrong. Moving it also deletes the
duplicated read: `model.py:136` and `summarize.py:132` each parse the same
key with their own copy of the validation today, and after this change
neither exists.

## Splitting a model id

`str.partition(":")` splits on the first colon and nothing else. That is the
whole rule, and it gives the colon-inside-the-model case for free, which
matters because real model ids carry colons (`some-family:8b`).

| `[models].<step>` value | Result |
|---|---|
| `"hosted:some-model"` | provider `hosted`, model `some-model` |
| `"hosted:some-family:8b"` | provider `hosted`, model `some-family:8b` |
| `"some-model"` | `ModelError`: no provider prefix |
| `":some-model"` | `ModelError`: empty provider |
| `"hosted:"` | `ModelError`: empty model |
| `"nope:some-model"`, no `[providers.nope]` | `ModelError`: unknown provider |
| not a string (a number, a table) | `ModelError`: not a string |

Error text, all raised by `model.resolve_target`:

```
[models].summarize: "some-model" has no provider prefix; write
"<provider>:some-model" naming a table under [providers]
[models].summarize: "" is an empty provider name
[models].summarize: "hosted:" names no model
[models].summarize names provider "nope", but [providers.nope] is not in
config.toml
[models].summarize: value is not a string
```

No stripping, no case folding, no trimming. A value is used exactly as
written, and every message quotes with `!r` so a stray space is visible in
the error rather than silently absorbed.

The prefix is required with no exceptions. An "optional when only one
provider is configured" rule would mean the meaning of an id changes when an
unrelated table is added, and every reader would have to count providers
before knowing what an id says. One rule.

## The core type

```python
class ModelTarget(NamedTuple):
    """Everything one paid call needs: which endpoint, which model,
    which environment variable holds the key. Parsed out of `[models]`
    and `[providers]` at the boundary; holds no key value."""

    provider: str
    url: str
    model: str
    key_env: str | None
    key_file_env: str | None
    pdf_part: str

    @property
    def id(self) -> str:
        """The `[models].<step>` value this was parsed from."""
        return f"{self.provider}:{self.model}"
```

### Why a value object, and why this does not break the plain-dict rule

The project rule is that `config.toml` is read as the dict `tomllib` returns
and passed around as that dict, and that the config is not re-typed into
dataclasses. This design keeps that: `Kb.config` stays a plain dict, every
function still receives it as one, and nothing builds a `Config` type.

`ModelTarget` is not the config. It is the result of a parse over two of the
config's tables, in the same shape `remotes.py` already uses:
`remotes.Remote` at `llmwiki/remotes.py:47` is a `NamedTuple` holding `name`,
`url`, and `token_env`, produced by `parse_remotes` from the `[remotes]`
table, and `remotes._request` reads `remote.token_env` rather than walking
the dict again. `ModelTarget` is the same idea applied to the endpoint
`model.py` owns. If the precedent is good for `[remotes]` it is good here,
and the two now read alike.

A plain tuple was the alternative. Six positional fields threaded through
`_post`, `_api_key`, `_attachment_content_part`, `chat`, `embed`, and three
caller modules is a misordering waiting to happen, and the fields have no
natural order. Named fields cost five lines.

### Known wart

`pdf_part` is meaningless on a target for the `embed` step. Accepted: it is a
provider fact, and the alternative shape, a nested `Provider` inside
`ModelTarget`, buys nothing but `target.provider.pdf_part` chains at every
read. Flat wins on reader load.

### Why `model.py` owns resolution

`model.py` already owns both halves being merged: `model_name` reads
`[models]`, `_endpoint_url` reads `[endpoint]`, and `ModelError` is its
entire error contract. Every caller of `model_name` already imports
`model.py`.

Not `core.py`: it owns paths, config loading, frontmatter, and atomic
writes. `pdf_part` is a wire-format fact about an HTTP endpoint, and putting
it in `core.py` would widen a filesystem module into network semantics.

Not `remotes.py`: its own docstring says `model.py` owns a different
endpoint, with a different credential and a different error set, and that
the two clients stay apart. That still holds.

Not a new `providers.py`: everything it would contain already lives in
`model.py`, and every consumer already imports `model.py`. A new module here
would be a second registry for the same concept, which is the exact bolt-on
shape this design exists to avoid.

## Credentials

`key_env` and `key_file_env` name environment variables. They never hold a
value, and no key ever appears in `config.toml`.

`_api_key(target)` resolves in the same precedence today's `_api_key()` uses,
now per provider:

1. `target.key_env` set and that variable holds a non-empty value: use it.
2. `target.key_file_env` set and that variable holds a path: read the file,
   strip one trailing newline, use it.
3. Neither field set: return `None`, and `_post` sends no `Authorization`
   header. This is what a server on the operator's own machine wants, and it
   matches `remotes._request`, which sends no header when `token_env` is
   `None`.
4. A field is set but its variable is unset or empty: `ModelError` naming the
   variable, never its value.

The key is read fresh on every call and is never assigned to module state.
`_post` puts it straight into the headers dict for one `Request` and drops
it. No `ModelError` raised anywhere on this path interpolates the key: the
messages carry the variable name, the provider name, the url, or an HTTP
status, and nothing else. `OPENER` still refuses proxies and redirects, so
the header cannot be walked to a host `config.toml` never named.

### `LLM_WIKI_API_KEY` and `LLM_WIKI_API_KEY_FILE`

Both are deleted, along with the `API_KEY_VAR` and `API_KEY_FILE_VAR`
constants and the `no API key: set ...` error. With more than one provider a
single global variable cannot say which endpoint it authenticates, so
keeping it would mean either a silent default or a precedence rule between a
global and a per-provider name. Neither earns its place.

What survives is the capability, not the names. `key_env` replaces
`LLM_WIKI_API_KEY` and `key_file_env` replaces `LLM_WIKI_API_KEY_FILE`, now
declared per provider instead of assumed. Reading a key from a file is kept
rather than dropped because it keeps the secret out of the process
environment, which is a real property the repo already spends effort on. The
path itself stays out of `config.toml`, since a path is a fact about one
machine and the config is a wiki config.

An operator using the old variables today rewrites one line:

```
LLM_WIKI_API_KEY=... -> LLM_WIKI_API_KEY_HOSTED=..., plus
key_env = "LLM_WIKI_API_KEY_HOSTED" under [providers.hosted]
```

## Refusing a legacy config

Text, raised by `model._refuse_legacy_endpoint(config)`:

```
config.toml still has [endpoint]; this version reads [providers].
Replace:
    [endpoint]
    url = "https://api.example.com/v1"
    pdf_part = "file"
with:
    [providers.hosted]
    url = "https://api.example.com/v1"
    pdf_part = "file"
    key_env = "LLM_WIKI_API_KEY_HOSTED"
then prefix every id under [models] with "hosted:".
```

`_refuse_legacy_endpoint` is the first statement of both public entry points,
`resolve_target` and `step_is_configured`, so no paid path reaches the
network without passing it.

### The trap this design has to avoid

Three call sites treat `except ModelError` as "this step is not configured"
and carry on:

- `ingest.py:64` skips the embed sweep
- `dedup.py:191` returns `None`, which disables the vector-neighbour gate
- `summarize.py:357` falls back from `summarize_image` to `summarize`

A refusal raised as a `ModelError` inside `resolve_target` would be swallowed
by all three. `ingest` would skip embedding in silence and exit 0, which is
the failure mode the project rules call out by name.

That conflation is already a latent defect at `ba3ae6e`: a typo in a
`[models]` key today reads as "not configured" rather than as an error, and
this design would add four more error shapes behind the same catch. So the
question gets its own function that cannot raise a false negative:

```python
def step_is_configured(config: dict, step: str) -> bool:
    """True when `[models].<step>` is present. Answers only presence,
    never validity: a present but malformed id makes `resolve_target`
    raise rather than making this return False."""
```

All three call sites switch from catching `ModelError` to asking this, and
every other `ModelError` then propagates loudly. `vectors._model_id` gets the
same treatment.

The dedup gate at `dedup.py:391` keeps its exact meaning: vector neighbours
are consulted only when a dedup judge model is configured. Only the way it
asks changes.

## Signatures

### `llmwiki/model.py`

| Old | New |
|---|---|
| `model_name(config: dict, step: str, model: str \| None) -> str` | deleted |
| `_endpoint_url(config: dict) -> str` | deleted |
| `_api_key() -> str` | `_api_key(target: ModelTarget) -> str \| None` |
| `_post(config: dict, path: str, body: dict) -> dict` | `_post(target: ModelTarget, path: str, body: dict) -> dict` |
| `_attachment_content_part(config: dict, media_type: str, data: bytes) -> dict` | `_attachment_content_part(target: ModelTarget, media_type: str, data: bytes) -> dict` |
| `chat(config, step, prompt, model=None, attachment=None, temperature=None) -> str` | `chat(target: ModelTarget, prompt: str, attachment: tuple[str, bytes] \| None = None, temperature: float \| None = None) -> str` |
| `embed(config: dict, texts: list[str], model: str \| None = None) -> list[list[float]]` | `embed(target: ModelTarget, texts: list[str]) -> list[list[float]]` |
| none | `resolve_target(config: dict, step: str) -> ModelTarget` |
| none | `step_is_configured(config: dict, step: str) -> bool` |
| none | `_refuse_legacy_endpoint(config: dict) -> None` |

`chat` and `embed` lose their `config`, `step`, and `model` parameters
because `ModelTarget` carries all three facts. The `model` override existed
so `summarize` could resolve `summarize_image` with a `summarize` fallback
and pass the answer back down; resolving to a target does that job properly,
since the fallback now moves the url and the key too, not just the name.
There is no `--model` CLI flag, so no user-facing surface changes.

`PDF_PART_SHAPES` and `MAX_ATTACHMENT_BYTES` stay as they are.

### `llmwiki/summarize.py`

| Old | New |
|---|---|
| `_pdf_part(config: dict) -> str` | deleted |
| `_image_model(kb: Kb) -> str` | `_image_target(kb: Kb) -> ModelTarget` |
| `_process_digest(kb, digest, prefix, fingerprint, pdf_part: str)` | `_process_digest(kb, digest, prefix, fingerprint, text_target: ModelTarget, visual_target: ModelTarget)` |
| `_resolve_content(kb, digest, prefix, pdf_part: str)` | unchanged signature; `pdf_part` now comes from `visual_target.pdf_part` |

`run` resolves both targets once at the top, so a bad provider name or a
typo'd `pdf_part` is a startup error before the first digest, which is what
`_pdf_part`'s docstring already asks for.

Passing two targets rather than one pair type is deliberate: the pair has no
invariant to protect and no behaviour, so a `NamedTuple` for it would be an
abstraction with one call site. `_process_digest` reaches six parameters,
each named at the call site.

The pdf_part that decides whether a PDF can be sent is now the visual
provider's, not a global. That is a correctness improvement: the endpoint
that will actually receive the PDF is the one whose limits apply.

### `llmwiki/dedup.py`

| Old | New |
|---|---|
| `_dedup_model_id(kb: Kb) -> str \| None` | `_dedup_target(kb: Kb) -> ModelTarget \| None` |

`_pick_target` still returns the id of the model that decided, now
`target.id`, so the recorded value names the provider as well as the model.
That string reaches page frontmatter and log lines, so it is a visible
change: `"judge"` becomes `"hosted:judge"`.

### `llmwiki/ingest.py`

`_sweep_or_fail(kb: Kb) -> bool` keeps its signature. Its body's
`try/except ModelError` around `model_name` becomes
`if not step_is_configured(kb.config, "embed"): return True`.

### `llmwiki/vectors.py`

| Old | New |
|---|---|
| `_model_id(kb: Kb) -> tuple[str \| None, str \| None]` | `_embed_target(kb: Kb) -> ModelTarget \| None` |
| `db_path(kb: Kb, model_id: str) -> Path` | `db_path(kb: Kb, target: ModelTarget) -> Path` |
| `slug(model_id: str) -> str` | unchanged |

The error text that `status` and `search` print stays byte-identical, moved
to a module constant so it is written once:

```python
NO_EMBED_MODEL = "missing [models].embed in config.toml"
```

`db_path` slugs `target.id`, the full `provider:model` string, so two
providers serving the same model name get separate databases. `slug` already
maps `:` to `--`, so `"hosted:some-embed"` becomes
`hosted--some-embed.sqlite`.

**Migration consequence:** every existing `vectors/*.sqlite` is orphaned by
the rename and the next `ingest` re-embeds the whole wiki once, at cost. Call
this out in the migration ticket. The pre-existing collision in `slug`, where
`a:b` and `a/b` produce one filename, is untouched and out of scope.

### `llmwiki/cli.py`

`CONFIG_TOML` is replaced in full (below). The `cmd_init` hint at line 115
changes from

```
edit config.toml and set [endpoint].url and your own [models] before running ingest
```

to

```
edit config.toml and set your own [providers] and [models] before running ingest
```

No `cmd_*` signature changes.

## The new `CONFIG_TOML`

```python
CONFIG_TOML = """\
# One model per paid pipeline step, read from this file at run time.
# Each id is "<provider>:<model>": the prefix names a table under
# [providers] below, and the rest is a model that provider serves. The
# prefix is always required. Replace these with ids your own endpoints
# serve.
[models]
summarize = "hosted:your-summarize-model"
embed = "desktop:your-embed-model"
dedup = "hosted:your-judge-model"

# Optional. A vision model for images and PDFs. Unset, visual sources
# use [models].summarize.
# summarize_image = "hosted:your-vision-model"

# One table per endpoint the ids above name. Uncomment and set your own
# urls; until then every paid step fails with "[models].summarize names
# provider "hosted", but [providers.hosted] is not in config.toml".
#
# key_env names the environment variable holding that provider's API
# key. key_file_env names one holding a path to read the key from. Set
# neither and no Authorization header is sent, which is what a server on
# your own machine usually wants. Never put a key value in this file.
#
# pdf_part is how this endpoint takes a PDF: "file", "image_url", or
# "none" to never send one. It defaults to "file".
# [providers.hosted]
# url = "https://api.example.com/v1"
# key_env = "LLM_WIKI_API_KEY_HOSTED"
# pdf_part = "file"

# [providers.desktop]
# url = "http://127.0.0.1:1234/v1"

# Identifier vocabulary. Each key can appear in a page's "identifiers"
# field as "key:value". The CLI appends this table to the summarizer
# prompt, so SUMMARIZE.md never repeats it. A key is a JOIN key: dedup
# joins two summaries that share one, so declare only keys that
# discriminate one subject from another (an isbn does; an ingredient
# name does not, it joins every page that uses salt). Edit or replace
# this example with your own.
[identifiers.isbn]
pattern = "^\\\\d{13}$"
describe = "13-digit ISBN without hyphens"

# Scheduled ingest jobs. Each job fetches from a feed or a list of
# urls, in "partial" mode (skip urls already seen) or "full" mode
# (fetch everything; unchanged bytes are skipped by their hash).
# [jobs.example]
# feed = "https://example.com/feed.xml"
# mode = "partial"
"""
```

Provider names are `hosted` and `desktop`, which say where the endpoint runs
and name no vendor. `local` is avoided on purpose: `remotes.py` reserves that
label for the kb on disk, and one config file should not teach two meanings
for one word.

`dedup` is now an ordinary uncommented key, closing ticket `agent-kb-8iz`.
The template previously never mentioned it, so an operator had no way to
discover the dedup judge without reading source.

Both provider tables ship commented out, keeping today's property that a
freshly initialized kb fails loudly rather than posting to `example.com`. The
failure names the exact table to uncomment.

## Skeletons

Stubs only. No logic.

```python
# llmwiki/model.py

PROVIDER_KEYS = ("url", "key_env", "key_file_env", "pdf_part")

DEFAULT_PDF_PART = "file"


class ModelTarget(NamedTuple):
    """Everything one paid call needs: which endpoint, which model,
    which environment variable holds the key. Holds no key value."""

    provider: str
    url: str
    model: str
    key_env: str | None
    key_file_env: str | None
    pdf_part: str

    @property
    def id(self) -> str:
        """The `[models].<step>` value this was parsed from."""
        raise NotImplementedError  # TODO: f"{self.provider}:{self.model}"


def _refuse_legacy_endpoint(config: dict) -> None:
    """Raise ModelError naming the replacement when `[endpoint]` is
    still present. Called first by every public entry point here, so no
    paid path reaches the network past a stale config."""
    raise NotImplementedError  # TODO


def _split_model_id(step: str, value: object) -> tuple[str, str]:
    """`"<provider>:<model>"` split on the FIRST colon, so a colon
    inside the model portion survives. Raises ModelError naming `step`
    for: a non-string value, no colon, an empty provider, an empty
    model."""
    raise NotImplementedError  # TODO: str.partition(":")


def _provider_table(config: dict, step: str, provider: str) -> dict:
    """The `[providers.<provider>]` table. Raises ModelError naming
    `step` and `provider` when absent, when not a table, or when it
    holds a key outside PROVIDER_KEYS."""
    raise NotImplementedError  # TODO


def step_is_configured(config: dict, step: str) -> bool:
    """True when `[models].<step>` is present. Answers only presence,
    never validity: a present but malformed id makes `resolve_target`
    raise rather than making this return False. Callers asking "is this
    step turned on" use this instead of catching ModelError, so a real
    config error can never read as "not configured"."""
    raise NotImplementedError  # TODO


def resolve_target(config: dict, step: str) -> ModelTarget:
    """The endpoint, model, credential variable names, and pdf_part for
    one pipeline step. Every failure raises ModelError, and no message
    carries a credential value."""
    raise NotImplementedError  # TODO


def _api_key(target: ModelTarget) -> str | None:
    """The key for `target`, read fresh from the environment on every
    call and never cached. `key_env` wins over `key_file_env`. `None`
    when the provider names neither, which means no Authorization
    header. Raises ModelError naming a variable, never its value, when
    a named variable is unset or unreadable."""
    raise NotImplementedError  # TODO


def _post(target: ModelTarget, path: str, body: dict) -> dict:
    """POST `body` as JSON to `{target.url}{path}` and return the parsed
    JSON response. Sends Authorization only when `_api_key` returns a
    value. HTTPError and URLError become ModelError built from the
    status or reason and the url alone, raised `from None`."""
    raise NotImplementedError  # TODO


def _attachment_content_part(
    target: ModelTarget, media_type: str, data: bytes
) -> dict:
    """One content part carrying `data` as `media_type`, using
    `target.pdf_part` for a PDF. Raises ModelError when pdf_part is
    outside PDF_PART_SHAPES, and when it is "none"."""
    raise NotImplementedError  # TODO


def chat(
    target: ModelTarget,
    prompt: str,
    attachment: tuple[str, bytes] | None = None,
    temperature: float | None = None,
) -> str:
    """One chat completion against `target`. Body shape, the
    MAX_ATTACHMENT_BYTES cap, and the optional temperature are
    unchanged from the pre-provider version."""
    raise NotImplementedError  # TODO


def embed(target: ModelTarget, texts: list[str]) -> list[list[float]]:
    """One embedding vector per entry in `texts`, in input order.
    Index-order checking is unchanged."""
    raise NotImplementedError  # TODO
```

```python
# llmwiki/summarize.py

def _image_target(kb: Kb) -> ModelTarget:
    """`[models].summarize_image` when configured, else
    `[models].summarize`. Resolving rather than naming means the
    fallback moves the url and the credential too, not just the model
    name."""
    raise NotImplementedError  # TODO: step_is_configured, then resolve_target


def _process_digest(
    kb: Kb,
    digest: str,
    prefix: str,
    fingerprint: str,
    text_target: ModelTarget,
    visual_target: ModelTarget,
) -> Literal["inert", "actionable"] | None:
    """Unchanged contract. `_resolve_content` is now gated on
    `visual_target.pdf_part`, the limit of the endpoint that will
    actually receive the PDF."""
    raise NotImplementedError  # TODO
```

```python
# llmwiki/dedup.py

def _dedup_target(kb: Kb) -> ModelTarget | None:
    """The configured dedup judge, or `None` when `[models].dedup` is
    unset (the deterministic-fallback path). A malformed id raises
    rather than reading as unset."""
    raise NotImplementedError  # TODO: step_is_configured, then resolve_target
```

```python
# llmwiki/vectors.py

NO_EMBED_MODEL = "missing [models].embed in config.toml"


def _embed_target(kb: Kb) -> ModelTarget | None:
    """The configured embed target, or `None` when `[models].embed` is
    unset. Callers print NO_EMBED_MODEL for the `None` case."""
    raise NotImplementedError  # TODO


def db_path(kb: Kb, target: ModelTarget) -> Path:
    """Creates no directory. Keyed on `target.id`, so two providers
    serving the same model name get separate databases."""
    raise NotImplementedError  # TODO: kb.vectors / f"{slug(target.id)}.sqlite"
```

## Test migration

Fifteen files write a literal `[models]` or `[endpoint]` config: eleven test
modules, three checked-in fixture configs, and one fixture harness script.
Editing fifteen f-strings by hand is the wrong shape, and it leaves the same
edit waiting the next time the schema moves.

Write the helper first, in a new `tests/kb_config.py`:

```python
def config_toml(
    url: str,
    models: dict[str, str],
    *,
    provider: str = "test",
    pdf_part: str | None = None,
    extra: str = "",
) -> str:
    """The text of a `config.toml` naming one provider at `url`.
    `models` maps a step to a BARE model name; every id is written out
    prefixed with `provider`. `extra` is appended verbatim, for the
    `[identifiers.*]` blocks most callers add."""
```

It goes in its own module, not in `tests/fake_endpoint.py`, because that file
owns the fake HTTP server and a config builder is a different body of
knowledge. Every one of the eleven modules already puts `tests/` on
`sys.path` to import `fake_endpoint`, so importing a sibling costs nothing.

Each call site then collapses. `tests/test_summarize.py:67` goes from

```python
'[models]\nsummarize = "cheap"\n\n' + f'[endpoint]\nurl = "{url}"\n\n' + identifiers
```

to

```python
config_toml(url, {"summarize": "cheap"}, extra=identifiers)
```

The helper deliberately covers one provider only. Multi-provider tests, the
ones that prove `summarize` and `embed` reach different endpoints, write
their TOML literally: there are a handful, two `FakeEndpoint` instances is
already the pattern at `tests/test_model.py:258`, and a `providers` mapping
parameter would complicate every single-provider caller to serve them.

`tests/test_model.py` is the one module that does not use the helper. It
builds config dicts inline today and needs splitting in two after this
change: `resolve_target` and `step_is_configured` get unit tests over plain
dicts covering every row of the split table above, and the `chat` and `embed`
tests build a `ModelTarget` directly with no config dict at all. Budget real
time for it, not a mechanical pass.

The three fixture configs, `tests/fixtures/security/.kb/config.toml`,
`tests/fixtures/recipe/.kb/config.toml`, and
`tests/fixtures/rung1/kb/config.toml`, are hand-edited: prefix the ids, add a
provider table. `tests/fixtures/rung1/run_rung1.py:190` appends
`[endpoint]\nurl = ...` at run time and becomes
`[providers.test]\nurl = ...`.

### New coverage this change owes

- Every row of the split table, each asserting the message names the step.
- A legacy `[endpoint]` config refused, from both `resolve_target` and
  `step_is_configured`.
- Two providers, two `FakeEndpoint` instances, `summarize` hitting one and
  `embed` hitting the other. This is the test the whole feature exists for.
- A provider with no `key_env` and no `key_file_env` sending no
  `Authorization` header.
- A provider naming a `key_env` whose variable is unset, and the resulting
  `ModelError` message asserted not to contain any key value.
- `ingest` with a malformed `[models].embed` failing loudly instead of
  skipping the sweep, which is the regression `step_is_configured` exists to
  prevent.
- Two providers serving the same model name getting separate `vectors/*.sqlite`
  files.

### Sequencing

The suite is red between steps 1 and 5. That is correct here: keeping every
intermediate step green would need a compatibility shim, and decision 5 rules
one out. Prove it at the end, not at every step.

1. `llmwiki/model.py`: `ModelTarget`, `resolve_target`,
   `step_is_configured`, `_refuse_legacy_endpoint`, and the reworked
   `_api_key`, `_post`, `_attachment_content_part`, `chat`, `embed`.
2. `tests/kb_config.py` and the three fixture configs.
3. Callers: `summarize.py`, `dedup.py`, `ingest.py`, `vectors.py`, and
   `cli.py`'s `CONFIG_TOML` and `cmd_init` hint.
4. The eleven test modules, mechanically, through `config_toml`.
5. `tests/test_model.py` rewritten, plus the new coverage above.

`scripts/check-mutation-gate.py` only mutates `llmwiki_service/`, which this
change does not touch, so the gate needs no edit.

## Open questions

Answers I would have asked the user for, settled here so the work is not
blocked. Each is reversible.

1. **Is `key_env` required or optional?** Settled as optional, with no
   `Authorization` header when neither credential field is set. The user's
   own preview writes `key_env` on the local provider too, which may mean
   they intend it required. Optional is what a server on `127.0.0.1` usually
   needs, and it matches `remotes.token_env`. If the user wants it required,
   the change is one guard in `_provider_table`.

2. **Does `LLM_WIKI_API_KEY_FILE` survive as `key_file_env`?** Settled yes.
   Dropping the file form entirely is simpler and I nearly chose it, but
   reading a key from a file keeps the secret out of the process
   environment, and this repo spends real effort on keeping keys out of
   places they can leak. If the user does not use it, delete the field and
   one branch of `_api_key`.

3. **Provider names in the template.** `hosted` and `desktop`. `local` was
   the obvious word and is avoided because `remotes.LOCAL_LABEL` already
   claims it for the kb on disk. If the user prefers other names, it is a
   string edit in `CONFIG_TOML`.

4. **The `vectors/*.sqlite` rename forces a full re-embed** on the first run
   after migration, because `db_path` now keys on `provider:model`. This
   costs real money on a large wiki. The alternative, keying on the bare
   model name, silently shares one database between two providers serving the
   same name, which is worse. The migration ticket should say so out loud.

5. **`dedup` records `"hosted:judge"` where it recorded `"judge"`.** This
   string reaches page frontmatter and `log.md`, so existing pages disagree
   with new ones. I judged naming the provider worth it, since with two
   providers the bare name no longer identifies what ran. Not reconciled with
   existing pages, and not proposed to be.

6. **No `config check` verb.** An operator finds a broken provider table on
   the first paid run, not before. A verb that resolves all four steps and
   prints the result would be small, and it is the natural follow-up. Left
   out as unrequested.

## Out of scope

- Migrating existing kbs off `[endpoint]`. Deferred to its own ticket.
- The `phase-05-packaging.md` endpoint-to-environment plan. Cancelled.
- Per-provider timeouts, retries, or model lists. `REQUEST_TIMEOUT_SEC` stays
  global and the no-retry rule stands.
- Swapping the HTML extractor, resolving `slug`'s `:` and `/` collision, or
  any other change the touched files invite.
