"""HTTP client for the configured API endpoint: chat and embeddings.
Every later phase (summarize, dedup, vectors) calls `chat()` or `embed()`
here instead of talking to the network itself.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

REQUEST_TIMEOUT_SEC = 60  # a hung endpoint must not wedge a cron job forever

API_KEY_VAR = "LLM_WIKI_API_KEY"
API_KEY_FILE_VAR = "LLM_WIKI_API_KEY_FILE"

# Unmeasured default (agent-kb phase 15): no probe has run against a
# real endpoint in this environment, since no credentials exist here.
# Base64 inflates raw bytes by about a third on the wire, so this caps
# the JSON body around 13.3MB. Replace it with the largest attachment
# the probe script (.nikki-agents/probe-attachments.py) finds the
# configured endpoint actually accepts, rounded down.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

# The three settings [endpoint].pdf_part accepts (decision D2). "file"
# and "image_url" are content-part shapes a probed endpoint may take a
# PDF as; "none" means PDFs are not sent at all.
PDF_PART_SHAPES = frozenset({"file", "image_url", "none"})


class ModelError(Exception):
    """Raised for a config or endpoint failure. Never carries the API key."""


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


def _api_key() -> str:
    """Resolve the API key from the environment. Read fresh on every
    call, never cached, so nothing holds the key in module state longer
    than one request needs it."""
    value = os.environ.get(API_KEY_VAR)
    if value is not None:
        return value
    path = os.environ.get(API_KEY_FILE_VAR)
    if path is not None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ModelError(
                f"cannot read {API_KEY_FILE_VAR}: {exc.strerror}"
            ) from None
        except ValueError:
            raise ModelError(
                f"cannot read {API_KEY_FILE_VAR}: not valid UTF-8"
            ) from None
        return text.removesuffix("\n")
    raise ModelError(f"no API key: set {API_KEY_VAR} or {API_KEY_FILE_VAR}")


def model_name(config: dict, step: str, model: str | None) -> str:
    """`model` is the per-run CLI override and wins when given; otherwise
    the model comes from `[models].<step>` in `config.toml`."""
    if model is not None:
        return model
    models = config.get("models", {})
    if step not in models:
        raise ModelError(f"missing [models].{step} in config.toml")
    return models[step]


def _endpoint_url(config: dict) -> str:
    url = config.get("endpoint", {}).get("url")
    if not url:
        raise ModelError("missing [endpoint].url in config.toml")
    return url


def _post(config: dict, path: str, body: dict) -> dict:
    """POST `body` as JSON to `{endpoint url}{path}` and return the
    parsed JSON response. `HTTPError`/`URLError` are caught and replaced
    with a `ModelError` built only from the status or reason and the
    configured url, so no request detail (headers included) can reach
    the caller, raised with `from None` to drop the original off the
    traceback the user sees.
    """
    url = _endpoint_url(config) + path
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
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


def _attachment_content_part(config: dict, media_type: str, data: bytes) -> dict:
    """One content part carrying `data` as `media_type`: an `image_url`
    data URL for an image (D1), or the `[endpoint].pdf_part` shape for
    a PDF (D2). "none" reaching here is a config error, not a silent
    send: the caller is expected to have already dropped a PDF digest
    rather than call this for it."""
    encoded = base64.b64encode(data).decode("ascii")
    if media_type != "application/pdf":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
        }
    pdf_part = config.get("endpoint", {}).get("pdf_part", "file")
    if pdf_part not in PDF_PART_SHAPES:
        raise ModelError(f"unrecognized [endpoint].pdf_part: {pdf_part!r}")
    if pdf_part == "none":
        raise ModelError("PDF attachments disabled: set [endpoint].pdf_part")
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
    config: dict,
    step: str,
    prompt: str,
    model: str | None = None,
    attachment: tuple[str, bytes] | None = None,
    temperature: float | None = None,
) -> str:
    """One chat completion for pipeline `step` (e.g. "summarize"). With
    no `attachment`, `content` is the plain prompt string, byte-
    identical to a call built before attachments existed. With one
    (`media_type`, raw bytes), `content` becomes a text part carrying
    `prompt` plus one attachment part. Over `MAX_ATTACHMENT_BYTES`,
    raises `ModelError` naming the size and the cap before any byte is
    base64-encoded or sent.

    `temperature` is sent only when it is not `None`, so a caller that
    leaves it unset produces the byte-identical body it always did and
    the endpoint keeps applying its own default."""
    name = model_name(config, step, model)
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
            _attachment_content_part(config, media_type, data),
        ]
    body: dict[str, object] = {
        "model": name,
        "messages": [{"role": "user", "content": content}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    response = _post(config, "/chat/completions", body)
    try:
        return response["choices"][0]["message"]["content"]
    except KeyError as exc:
        raise ModelError(f"chat response missing {exc}") from None


def embed(
    config: dict, texts: list[str], model: str | None = None
) -> list[list[float]]:
    """One embedding vector per entry in `texts`, in input order. The
    model name always comes from `[models].embed`; embedding has no
    per-call step like `chat` does."""
    name = model_name(config, "embed", model)
    body = {"model": name, "input": texts}
    response = _post(config, "/embeddings", body)
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
