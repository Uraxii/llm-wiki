[overview](overview.md)

# Phase 5: model client

**Goal.** One HTTP client for chat and embeddings, so summarize, dedup, and
vectors never talk to the network themselves.

**Changes.** `llmwiki/model.py`, `tests/test_model.py`, `tests/fake_endpoint.py`
(a local `http.server` thread answering chat and embeddings, reused by every
later test that needs a model). `chat(config, step, prompt, model=None) -> str`
and `embed(config, texts, model=None) -> list[list[float]]` over the configured
API endpoint (`[endpoint] url`) with `urllib`; model name from
`[models].<step>`, the optional argument is the per-run CLI override (`.8`).
Credentials come from the system environment only (user directive
2026-08-29: "cli tool should use system environment credentials"), per
`docs/design/llm-wiki.md` "Credentials": `LLM_WIKI_API_KEY` as a value or
`LLM_WIKI_API_KEY_FILE` as a path, first hit wins, neither set is an error
naming both variables; never stored in `config.toml`, never printed, stripped
from any re-raised error. No retry policy (none decided; a
failed call is a `failed` line for that source).

**Data structures.** None new; the response is parsed to the one field used.

**Verification.** Tests against the fake endpoint: request shape, model name
from config, override wins, credential from `_FILE` wins over none, the key
never appears in output or log. Runtime: one real chat call with the user's
approval, planned count printed first. Reviewer gate before close (credential
handling).
