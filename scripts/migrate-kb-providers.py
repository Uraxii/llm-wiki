#!/usr/bin/env python3
"""Rewrite a kb's `config.toml` from the retired `[endpoint]` table to the
`[providers]` shape that `llmwiki.model.resolve_target` reads.

Two edits to the config, both textual, so every comment in the file
survives:

  1. the `[endpoint]` header becomes `[providers.<PROVIDER_NAME>]`, keeping
     every key under it (`url`, and `pdf_part` when it is set), and gains a
     `key_env` line naming the environment variable the key is read from
  2. every `[models]` value gains the `<PROVIDER_NAME>:` prefix the new id
     shape requires

The vector database moves with the config. `vectors.db_path` names its file
after the embed id alone, so the prefix would otherwise orphan it and force a
full re-embed. The stored vectors stay correct across the rename because the
url and the model name both survive the migration verbatim, and the vec0
table records the dimension but not the id, with `_ensure_table` already
failing loudly on a dimension it did not expect.

A rewrite is never trusted on its own: the result is re-parsed and every
`[models]` step is put through `resolve_target`, so a kb that would still
fail at run time is refused instead of written.

`--dry-run` is the default and prints the diff and the renames it would
apply. Writing needs an explicit `--apply`. Running it twice is safe: a
config with no `[endpoint]` is reported "already current" and left alone.

Names no credential value and reads none. `key_env` holds the NAME of an
environment variable, which is what the new schema stores.

Usage:

    .venv/bin/python scripts/migrate-kb-providers.py <kb> [<kb> ...]
    .venv/bin/python scripts/migrate-kb-providers.py --apply <kb>
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llmwiki.core import atomic_write_text  # noqa: E402
from llmwiki.model import (  # noqa: E402
    PROVIDER_KEYS,
    ModelError,
    resolve_target,
)
from llmwiki.vectors import slug  # noqa: E402

# The provider name every migrated id is prefixed with. One name, because
# every config on the old shape had exactly one endpoint to name. Rename the
# block by hand afterwards if a kb grows a second provider.
PROVIDER_NAME = "hosted"

# The variable the retired code read the key from, so a migrated kb keeps
# working against the environment the operator already has set. This is a
# variable name, never a key value.
KEY_ENV = "LLM_WIKI_API_KEY"

# The sqlite database and the two files sqlite keeps beside it in WAL
# mode. A `-wal` file whose base name no longer matches its database is
# orphaned, so the three move together.
VECTOR_SUFFIXES = ("", "-shm", "-wal")

HEADER_RE = re.compile(r"\s*\[([^\]]+)\]\s*$")
ENTRY_RE = re.compile(r'(\s*)([A-Za-z0-9_-]+)(\s*=\s*)"([^"]*)"(\s*)$')


class Refused(Exception):
    """This kb is not migrated, and the message says why. Raised before
    anything is written, so a refusal never leaves a half-edited file."""


def config_path(root: Path) -> Path:
    """`root/config.toml`, after checking `root` is a kb at all. A bare
    directory holding an unrelated `config.toml` must not be rewritten."""
    for name, ok in (
        ("config.toml", (root / "config.toml").is_file()),
        ("wiki/", (root / "wiki").is_dir()),
        ("sources/", (root / "sources").is_dir()),
    ):
        if not ok:
            raise Refused(f"not a kb: {root} has no {name}")
    return root / "config.toml"


def _check_migratable(config: dict) -> dict:
    """The `[endpoint]` table, once it is safe to rewrite. Raises Refused
    for a config that is mixed, malformed, or already carries an id shape
    this cannot tell apart from a migrated one."""
    endpoint = config.get("endpoint")
    if not isinstance(endpoint, dict):
        raise Refused("[endpoint] is not a table")
    if "providers" in config:
        raise Refused(
            "config holds both [endpoint] and [providers]; migrate by hand"
        )
    for key in endpoint:
        if key not in PROVIDER_KEYS:
            raise Refused(
                f"[endpoint] holds {key!r}, which no [providers.<name>] "
                "table accepts"
            )
    models = config.get("models")
    if not isinstance(models, dict) or not models:
        raise Refused("no [models] table to prefix")
    for step, value in models.items():
        if not isinstance(value, str) or not value:
            raise Refused(f"[models].{step} is not a model id")
        if ":" in value:
            raise Refused(
                f"[models].{step} = {value!r} already holds a colon, so a "
                "prefix cannot be added without guessing; migrate by hand"
            )
    return endpoint


def migrate_text(text: str, *, add_key_env: bool) -> str:
    """The rewritten file. Line-based on purpose: a TOML writer would
    reformat the file and drop every comment in it."""
    out: list[str] = []
    table = ""
    for line in text.splitlines(keepends=True):
        header = HEADER_RE.match(line)
        if header:
            table = header.group(1).strip()
            if table == "endpoint":
                table = f"providers.{PROVIDER_NAME}"
                out.append(f"[providers.{PROVIDER_NAME}]\n")
                if add_key_env:
                    out.append(f'key_env = "{KEY_ENV}"\n')
                continue
            out.append(line)
            continue
        entry = ENTRY_RE.match(line)
        if table == "models" and entry:
            indent, key, equals, value, trailing = entry.groups()
            line = f'{indent}{key}{equals}"{PROVIDER_NAME}:{value}"{trailing}'
        out.append(line)
    return "".join(out)


def _verify(text: str, path: Path) -> None:
    """Every `[models]` step in the rewritten text resolves. A rewrite that
    would still fail at run time is refused, not written."""
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"rewrite is not valid TOML: {exc}") from exc
    if "endpoint" in config:
        raise Refused(f"[endpoint] survived the rewrite of {path}")
    for step in config.get("models", {}):
        try:
            resolve_target(config, step)
        except ModelError as exc:
            raise Refused(f"rewritten [models].{step} still fails: {exc}")


class Plan(NamedTuple):
    """Everything one kb's migration writes. Indexable, so the config
    text is `plan[2]` as well as `plan.new`."""

    path: Path
    old: str
    new: str
    renames: list[tuple[Path, Path]]


def _vector_db(root: Path, model_id: str) -> Path:
    """The vector database an embed id is stored under. Mirrors
    `vectors.db_path`, which is the authority on the name; only `slug` is
    shared, because building a `Kb` here would load the config this run is
    still deciding whether to rewrite."""
    return root / "vectors" / f"{slug(model_id)}.sqlite"


def _vector_renames(
    root: Path, old_id: str | None, new_id: str | None
) -> list[tuple[Path, Path]]:
    """The vectors/ files the new embed id renames. Empty when the kb has
    no database yet, which is not an error: an unembedded kb has nothing to
    carry over. Raises Refused when both names exist, since only the
    operator can say which of the two databases is the live one."""
    if not old_id or not new_id or old_id == new_id:
        return []
    old_db = _vector_db(root, old_id)
    new_db = _vector_db(root, new_id)
    if not old_db.exists():
        return []
    if new_db.exists():
        raise Refused(
            f"{old_db.name} and {new_db.name} both exist in {old_db.parent}; "
            "remove or move one by hand before migrating"
        )
    moves = []
    for suffix in VECTOR_SUFFIXES:
        source = old_db.with_name(old_db.name + suffix)
        if source.exists():
            moves.append((source, new_db.with_name(new_db.name + suffix)))
    return moves


def _embed_id(config: dict) -> str | None:
    value = config.get("models", {}).get("embed")
    return value if isinstance(value, str) else None


def plan(root: Path) -> Plan | None:
    """The migration for a kb that needs one, `None` for one already on the
    `[providers]` shape. Raises Refused otherwise."""
    path = config_path(root)
    text = path.read_text(encoding="utf-8")
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"malformed config: {path}: {exc}") from exc
    if "endpoint" not in config:
        return None
    endpoint = _check_migratable(config)
    add_key_env = not {"key_env", "key_file_env"} & set(endpoint)
    new = migrate_text(text, add_key_env=add_key_env)
    if new == text:
        raise Refused(
            f"{path} parses with [endpoint] but has no [endpoint] header "
            "line to rewrite"
        )
    _verify(new, path)
    renames = _vector_renames(
        root, _embed_id(config), _embed_id(tomllib.loads(new))
    )
    return Plan(path, text, new, renames)


def _report_vectors(renames: list[tuple[Path, Path]], apply: bool) -> None:
    """Say what happens to the vector database, including when the answer
    is nothing, so a kb that will re-embed cannot look like one that will
    not."""
    if not renames:
        print("    vectors: no database to move")
        return
    verb = "renamed" if apply else "would rename"
    for source, destination in renames:
        print(f"    {verb} vectors/{source.name} -> {destination.name}")


def run(roots: list[Path], apply: bool) -> int:
    failures = 0
    for root in roots:
        try:
            change = plan(root)
        except Refused as exc:
            print(f"{root}: REFUSED: {exc}", file=sys.stderr)
            failures += 1
            continue
        if change is None:
            print(f"{root}: already current")
            continue
        verb = "rewrote" if apply else "would rewrite"
        print(f"{root}: {verb} config.toml")
        for line in difflib.unified_diff(
            change.old.splitlines(keepends=True),
            change.new.splitlines(keepends=True),
            fromfile=f"a/{change.path}",
            tofile=f"b/{change.path}",
        ):
            print("    " + line.rstrip("\n"))
        _report_vectors(change.renames, apply)
        if apply:
            # Vectors first: a crash between the two leaves the old config
            # naming the old database, which no longer exists, and a re-run
            # then finds nothing to rename and finishes the job. The other
            # order leaves a current config no re-run will touch again.
            for source, destination in change.renames:
                source.rename(destination)
            atomic_write_text(change.path, change.new)
    if not apply:
        print("\ndry run: nothing written. Re-run with --apply to write.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate a kb config.toml from [endpoint] to [providers]. "
            "Prints the diff and writes nothing unless --apply is given."
        )
    )
    parser.add_argument("kb", nargs="+", type=Path, help="a kb root")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the migrated config.toml instead of printing the diff",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="the default; accepted so it can be written out explicitly",
    )
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run contradict each other")
    return run(args.kb, args.apply)


if __name__ == "__main__":
    sys.exit(main())
