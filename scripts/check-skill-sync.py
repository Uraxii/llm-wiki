#!/usr/bin/env python3
"""Fail if the shipped skill or the plugin version numbers have drifted.

In plain words: this skill lives in two places on disk, and its version
number is written in four files. This script compares them and names the
one that has fallen out of step, so nobody ships a plugin whose skill text
is older than the copy in this repo.
"""

import json
import os
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = Path("skills/llm-wiki")
MANIFESTS = (
    Path(".claude-plugin/plugin.json"),
    Path(".codex-plugin/plugin.json"),
    Path("plugin.json"),
)

failures = 0


def passed(message: str) -> None:
    print(f"PASS  {message}")


def failed(message: str) -> None:
    global failures
    failures += 1
    print(f"FAIL  {message}")


def skipped(message: str) -> None:
    print(f"SKIP  {message}")


def files_under(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def check_skill_copy(dotai_root: Path) -> None:
    ours_root = REPO_ROOT / SKILL_DIR
    theirs_root = dotai_root / SKILL_DIR
    if not theirs_root.is_dir():
        skipped(f"skill copy: no dotai checkout at {theirs_root}")
        return
    if not ours_root.is_dir():
        failed(f"skill copy: repo skill missing at {ours_root}")
        return

    ours = files_under(ours_root)
    theirs = files_under(theirs_root)
    drifted = False
    for name in sorted(ours - theirs):
        drifted = True
        failed(f"skill copy: {name} is in {ours_root} but not {theirs_root}")
    for name in sorted(theirs - ours):
        drifted = True
        failed(f"skill copy: {name} is in {theirs_root} but not {ours_root}")
    for name in sorted(ours & theirs):
        if (ours_root / name).read_bytes() != (theirs_root / name).read_bytes():
            drifted = True
            failed(f"skill copy: {name} differs between {ours_root} and {theirs_root}")
    if not drifted:
        passed(f"skill copy: {ours_root} matches {theirs_root}, {len(ours)} file(s)")


def check_manifest_versions() -> None:
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.is_file():
        failed("manifest versions: pyproject.toml missing")
        return
    want = tomllib.loads(pyproject.read_text()).get("project", {}).get("version")
    if not want:
        failed("manifest versions: no [project] version in pyproject.toml")
        return

    for rel in MANIFESTS:
        path = REPO_ROOT / rel
        if not path.is_file():
            failed(f"manifest versions: {rel} is missing")
            continue
        got = json.loads(path.read_text()).get("version")
        if not got:
            failed(f"manifest versions: {rel} has no version field")
        elif got != want:
            failed(f"manifest versions: {rel} is {got}, pyproject.toml is {want}")
        else:
            passed(f"manifest versions: {rel} is {want}")


def main() -> int:
    check_skill_copy(Path(os.environ.get("DOTAI") or Path.home() / "dotai"))
    check_manifest_versions()

    print("---")
    if failures:
        print(f"summary: {failures} check(s) failed")
        return 1
    print("summary: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
