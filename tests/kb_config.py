"""Builds a `config.toml` naming one `[models]`/`[providers]` pair, so a
schema change to that boundary costs one edit here instead of a hand
edit in each of the modules that write a config file for a test.
"""
from __future__ import annotations


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
    model_lines = "".join(
        f'{step} = "{provider}:{model}"\n' for step, model in models.items()
    )
    provider_lines = f'[providers.{provider}]\nurl = "{url}"\n'
    if pdf_part is not None:
        provider_lines += f'pdf_part = "{pdf_part}"\n'
    return f"[models]\n{model_lines}\n{provider_lines}\n{extra}"
