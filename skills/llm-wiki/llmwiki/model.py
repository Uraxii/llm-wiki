"""HTTP client for the model endpoints named under `[providers]`. Every
later phase (summarize, dedup, vectors) calls `chat()` or `embed()`
here instead of talking to the network itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError, URLError

REQUEST_TIMEOUT_SEC = 60  # a hung endpoint must not wedge a cron job forever

# Unmeasured default (agent-kb phase 15): no probe has run against a
# real endpoint in this environment, since no credentials exist here.
# Base64 inflates raw bytes by about a third on the wire, so this caps
# the JSON body around 13.3MB. Replace it with the largest attachment
# the probe script (.nikki-agents/probe-attachments.py) finds the
# configured endpoint actually accepts, rounded down.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

# The three settings a provider's pdf_part accepts (decision D2). "file"
# and "image_url" are content-part shapes a probed endpoint may take a
# PDF as; "none" means PDFs are not sent at all.
PDF_PART_SHAPES = frozenset({"file", "image_url", "none"})

DEFAULT_PDF_PART = "file"

# Recognized keys in a [providers.<name>] table. An unknown key is an
# error (agent-kb-9i7), matching how remotes._remote rejects one rather
# than ignoring it.
PROVIDER_KEYS = ("url", "key_env", "key_file_env", "pdf_part")


class ModelError(Exception):
    """Raised for a config or endpoint failure. Never carries the API key."""


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx must not re-send the Authorization header to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Built once at import time. Disables environment-based proxy lookup
# (ProxyHandler({})) and follow-redirect (both would resend the
# Authorization header to a host the config never named). Holds no key;
# the key stays a per-call header.
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
)


def _refuse_legacy_endpoint(config: dict) -> None:
    """Raise ModelError naming the replacement when `[endpoint]` is
    still present. Called first by every public entry point here, so no
    paid path reaches the network past a stale config."""
    if "endpoint" not in config:
        return
    raise ModelError(
        'config.toml still has [endpoint]; this version reads [providers].\n'
        'Replace:\n'
        '    [endpoint]\n'
        '    url = "https://api.example.com/v1"\n'
        '    pdf_part = "file"\n'
        'with:\n'
        '    [providers.hosted]\n'
        '    url = "https://api.example.com/v1"\n'
        '    pdf_part = "file"\n'
        '    key_env = "LLM_WIKI_API_KEY_HOSTED"\n'
        'then prefix every id under [models] with "hosted:".'
    )


def _split_model_id(step: str, value: object) -> tuple[str, str]:
    """`"<provider>:<model>"` split on the FIRST colon, so a colon
    inside the model portion survives. Raises ModelError naming `step`
    for: a non-string value, no colon, an empty provider, an empty
    model."""
    if not isinstance(value, str):
        raise ModelError(f"[models].{step}: value is not a string")
    provider, sep, model = value.partition(":")
    if not sep:
        raise ModelError(
            f"[models].{step}: {value!r} has no provider prefix; write "
            f'"<provider>:{value}" naming a table under [providers]'
        )
    if not provider:
        raise ModelError(f"[models].{step}: {provider!r} is an empty provider name")
    if not model:
        raise ModelError(f"[models].{step}: {value!r} names no model")
    return provider, model


def _provider_table(config: dict, step: str, provider: str) -> dict:
    """The `[providers.<provider>]` table. Raises ModelError naming
    `step` and `provider` when absent, when not a table, or when it
    holds a key outside PROVIDER_KEYS."""
    providers = config.get("providers")
    table = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(table, dict):
        raise ModelError(
            f"[models].{step} names provider {provider!r}, but "
            f"[providers.{provider}] is not in config.toml"
        )
    for key in table:
        if key not in PROVIDER_KEYS:
            raise ModelError(
                f"[models].{step}: [providers.{provider}] holds unrecognized "
                f"key {key!r}"
            )
    return table


def step_is_configured(config: dict, step: str) -> bool:
    """True when `[models].<step>` is present. Answers only presence,
    never validity: a present but malformed id makes `resolve_target`
    raise rather than making this return False. Callers asking "is this
    step turned on" use this instead of catching ModelError, so a real
    config error can never read as "not configured"."""
    _refuse_legacy_endpoint(config)
    models = config.get("models")
    return isinstance(models, dict) and step in models


def resolve_target(config: dict, step: str) -> ModelTarget:
    """The endpoint, model, credential variable names, and pdf_part for
    one pipeline step. Every failure raises ModelError, and no message
    carries a credential value."""
    _refuse_legacy_endpoint(config)
    models = config.get("models", {})
    if not isinstance(models, dict) or step not in models:
        raise ModelError(f"missing [models].{step} in config.toml")
    provider, model = _split_model_id(step, models[step])
    table = _provider_table(config, step, provider)

    url = table.get("url")
    if not isinstance(url, str) or not url:
        raise ModelError(f"[providers.{provider}]: url is missing or not a string")

    key_env = table.get("key_env")
    if key_env is not None and not isinstance(key_env, str):
        raise ModelError(f"[providers.{provider}]: key_env is not a string")

    key_file_env = table.get("key_file_env")
    if key_file_env is not None and not isinstance(key_file_env, str):
        raise ModelError(f"[providers.{provider}]: key_file_env is not a string")

    pdf_part = table.get("pdf_part", DEFAULT_PDF_PART)
    if pdf_part not in PDF_PART_SHAPES:
        raise ModelError(
            f"[providers.{provider}]: unrecognized pdf_part: {pdf_part!r}"
        )

    return ModelTarget(provider, url, model, key_env, key_file_env, pdf_part)


def _api_key(target: ModelTarget) -> str | None:
    """The key for `target`, read fresh from the environment on every
    call and never cached. `key_env` wins over `key_file_env`. `None`
    when the provider names neither, which means no Authorization
    header. Raises ModelError naming a variable, never its value, when
    a named variable is unset or unreadable."""
    if target.key_env is not None:
        value = os.environ.get(target.key_env)
        if not value:
            raise ModelError(f"{target.key_env} is unset or empty")
        return value
    if target.key_file_env is not None:
        path = os.environ.get(target.key_file_env)
        if not path:
            raise ModelError(f"{target.key_file_env} is unset or empty")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ModelError(
                f"cannot read {target.key_file_env}: {exc.strerror}"
            ) from None
        except ValueError:
            raise ModelError(
                f"cannot read {target.key_file_env}: not valid UTF-8"
            ) from None
        return text.removesuffix("\n")
    return None


def _post(target: ModelTarget, path: str, body: dict) -> dict:
    """POST `body` as JSON to `{target.url}{path}` and return the parsed
    JSON response. Sends Authorization only when `_api_key` returns a
    value. HTTPError and URLError become ModelError built from the
    status or reason and the url alone, raised `from None`."""
    url = target.url + path
    headers = {"Content-Type": "application/json"}
    key = _api_key(target)
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with OPENER.open(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        raise ModelError(f"request to {url} failed: HTTP {exc.code}") from None
    except URLError as exc:
        raise ModelError(f"request to {url} failed: {exc.reason}") from None
    except OSError as exc:
        raise ModelError(f"request to {url} failed: {exc}") from None


def _attachment_content_part(
    target: ModelTarget, media_type: str, data: bytes
) -> dict:
    """One content part carrying `data` as `media_type`, using
    `target.pdf_part` for a PDF. Raises ModelError when pdf_part is
    outside PDF_PART_SHAPES, and when it is "none"."""
    encoded = base64.b64encode(data).decode("ascii")
    if media_type != "application/pdf":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
        }
    pdf_part = target.pdf_part
    if pdf_part not in PDF_PART_SHAPES:
        raise ModelError(
            f"unrecognized [providers.{target.provider}].pdf_part: {pdf_part!r}"
        )
    if pdf_part == "none":
        raise ModelError(
            f"PDF attachments disabled: set [providers.{target.provider}].pdf_part"
        )
    if pdf_part == "image_url":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:application/pdf;base64,{encoded}"},
        }
    filename = f"{hashlib.sha256(data).hexdigest()}.pdf"
    return {
        "type": "file",
        "file": {
            "filename": filename,
            "file_data": f"data:application/pdf;base64,{encoded}",
        },
    }


def chat(
    target: ModelTarget,
    prompt: str,
    attachment: tuple[str, bytes] | None = None,
    temperature: float | None = None,
) -> str:
    """One chat completion against `target`. With no `attachment`,
    `content` is the plain prompt string, byte-identical to a call built
    before attachments existed. With one (`media_type`, raw bytes),
    `content` becomes a text part carrying `prompt` plus one attachment
    part. Over `MAX_ATTACHMENT_BYTES`, raises `ModelError` naming the
    size and the cap before any byte is base64-encoded or sent.

    `temperature` is sent only when it is not `None`, so a caller that
    leaves it unset produces the byte-identical body it always did and
    the endpoint keeps applying its own default."""
    if attachment is None:
        content: str | list[dict] = prompt
    else:
        media_type, data = attachment
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ModelError(
                f"attachment too large: {len(data)} bytes exceeds the "
                f"{MAX_ATTACHMENT_BYTES} byte cap"
            )
        content = [
            {"type": "text", "text": prompt},
            _attachment_content_part(target, media_type, data),
        ]
    body: dict[str, object] = {
        "model": target.model,
        "messages": [{"role": "user", "content": content}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    response = _post(target, "/chat/completions", body)
    try:
        return response["choices"][0]["message"]["content"]
    except KeyError as exc:
        raise ModelError(f"chat response missing {exc}") from None


def embed(target: ModelTarget, texts: list[str]) -> list[list[float]]:
    """One embedding vector per entry in `texts`, in input order.
    Index-order checking is unchanged."""
    body = {"model": target.model, "input": texts}
    response = _post(target, "/embeddings", body)
    try:
        ordered = sorted(response["data"], key=lambda item: item["index"])
    except KeyError as exc:
        raise ModelError(f"embed response missing {exc}") from None
    indices = [item["index"] for item in ordered]
    if indices != list(range(len(texts))):
        raise ModelError(
            "embed response indices do not match input texts: "
            f"got {indices} for {len(texts)} texts"
        )
    return [item["embedding"] for item in ordered]
