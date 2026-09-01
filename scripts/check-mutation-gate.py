#!/usr/bin/env python3
"""Run mutmut against the modules named in pyproject.toml's [tool.mutmut]
only_mutate (today: auth.py, tokens.py, deployment.py, app.py, tls.py,
routes.py) and fail when a mutant survives that
scripts/mutation-survivor-baseline.txt does not name.

mutmut's own exit code does not reflect survivors (`mutmut run` always
returns 0), so this script runs it, reads `mutmut results` itself, and
compares the survived set against the baseline. A survivor already in the
baseline is known debt and does not fail the gate; killing it is separate
follow-up work. A survivor NOT in the baseline means a mutant that used to
die now lives, which means the tests protecting that line got weaker, and
that is what this gate exists to catch.

The gate also fails on any mutant in mutmut's `no tests` state: a mutated
line that no test in pytest_add_cli_args_test_selection even ran against.
That is worse than a plain survivor, since the gate would otherwise stay
green while measuring nothing for that line. There is no baseline for
this state; the count must always be zero.

Every run deletes mutants/ first and regenerates from scratch. mutmut's
incremental cache does not notice an edit to a test file (it tracks source
files, not test content), so a weakened assertion would otherwise leave a
stale "killed" verdict in place and the gate would stay green while the
tests protecting that mutant got weaker, which is exactly the failure this
gate exists to catch. A full run costs about 7 seconds here, so paying it
every time is cheap next to that hole.

Configuration (source paths, mutated files, test selection) lives in
pyproject.toml under [tool.mutmut]. See scripts/mutation-survivor-baseline.txt
for how stable the mutant identifiers are and how to regenerate the file.

Run with `--update-baseline` to regenerate the file in place: it rewrites
the survivor list to match the current run while keeping the header above
and every entry's trailing `# reason` comment intact. A newly surviving
mutant is added bare, with no reason, and printed to stderr as needing
one; nothing gets committed with an unexplained entry.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = REPO_ROOT / "scripts" / "mutation-survivor-baseline.txt"
MUTANTS_DIR = REPO_ROOT / "mutants"


def _entry_id(line: str) -> str:
    """The mutant id on a baseline entry line, with any trailing
    `# reason` comment stripped."""
    return line.split("#", 1)[0].strip()


def load_baseline() -> set[str]:
    lines = BASELINE_FILE.read_text(encoding="utf-8").splitlines()
    return {
        _entry_id(line)
        for line in lines
        if line.strip() and not line.startswith("#")
    }


def split_header_and_entries(lines: list[str]) -> tuple[list[str], dict[str, str]]:
    """The leading `#`-and-blank header block, and the entry lines that
    follow it as `{mutant_id: full_line}`, so a rewrite can carry each
    entry's trailing reason comment forward unchanged."""
    header_end = 0
    for header_end, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            break
    else:
        header_end = len(lines)
    header = lines[:header_end]
    entries = {
        _entry_id(line): line for line in lines[header_end:] if line.strip()
    }
    return header, entries


def update_baseline(survivors: set[str]) -> None:
    """Rewrite the baseline to exactly `survivors`, keeping the header and
    every carried-over entry's `# reason` comment. A mutant id with no
    prior entry is written bare and reported on stderr: it still needs a
    reason before this file is trustworthy again."""
    old_lines = BASELINE_FILE.read_text(encoding="utf-8").splitlines()
    header, old_entries = split_header_and_entries(old_lines)

    unexplained = sorted(mutant_id for mutant_id in survivors if mutant_id not in old_entries)
    new_lines = [
        old_entries.get(mutant_id, mutant_id) for mutant_id in sorted(survivors)
    ]
    BASELINE_FILE.write_text(
        "\n".join(header + new_lines) + "\n", encoding="utf-8"
    )
    print(
        f"baseline updated: {len(survivors)} entr{'y' if len(survivors) == 1 else 'ies'}, "
        f"header preserved",
        file=sys.stderr,
    )
    if unexplained:
        print(
            f"{len(unexplained)} entr{'y' if len(unexplained) == 1 else 'ies'} "
            "added with no reason, add one before committing:",
            file=sys.stderr,
        )
        for mutant_id in unexplained:
            print(f"  {mutant_id}", file=sys.stderr)


def run_mutmut() -> None:
    # Force a full regeneration; see the module docstring for why the
    # incremental cache cannot be trusted here.
    shutil.rmtree(MUTANTS_DIR, ignore_errors=True)
    # mutmut prints a live spinner with carriage returns; capture it and
    # only show it when something went wrong, so a green run stays quiet.
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "run"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        print(f"mutmut run exited {result.returncode}", file=sys.stderr)
        raise SystemExit(result.returncode)


def current_results() -> tuple[set[str], set[str]]:
    """Survived and no-tests mutant ids from `mutmut results`.

    `no tests` means no test in pytest_add_cli_args_test_selection ran
    against that mutant at all (mutmut exit code 5 or 33), so the mutant
    was never given a chance to be killed. That is worse than a survivor:
    a survivor was tested and beat the tests, a `no tests` mutant proves
    the line has zero mutation coverage while the gate stays quiet.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    survivors = set()
    no_tests = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.endswith(": survived"):
            survivors.add(line[: -len(": survived")])
        elif line.endswith(": no tests"):
            no_tests.add(line[: -len(": no tests")])
    return survivors, no_tests


def main(argv: list[str]) -> int:
    update = "--update-baseline" in argv
    run_mutmut()
    survivors, no_tests = current_results()

    if no_tests:
        for mutant_id in sorted(no_tests):
            print(mutant_id)
        print(
            f"{len(no_tests)} mutant(s) in \"no tests\" state: no test in "
            "pytest_add_cli_args_test_selection ran against them. There is "
            "no baseline for this state; the correct count is always zero. "
            "Add the test file that covers the mutated line to "
            "pytest_add_cli_args_test_selection in pyproject.toml.",
            file=sys.stderr,
        )
        return 1

    if update:
        update_baseline(survivors)
        return 0

    baseline = load_baseline()
    new_survivors = sorted(survivors - baseline)

    for mutant_id in new_survivors:
        print(mutant_id)

    if new_survivors:
        print(
            f"{len(new_survivors)} new mutant(s) survived, "
            f"not in {BASELINE_FILE.name}",
            file=sys.stderr,
        )
        return 1

    killed_from_baseline = len(baseline - survivors)
    note = (
        f", {killed_from_baseline} baseline survivor(s) now killed "
        f"(baseline not auto-updated)"
        if killed_from_baseline
        else ""
    )
    print(
        f"mutation gate green: {len(survivors)} survivor(s), "
        f"all in baseline{note}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
