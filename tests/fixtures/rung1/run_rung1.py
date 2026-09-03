#!/usr/bin/env python3
"""Rung 1 acceptance harness for the llm-wiki PoC (phase 10 evidence).

Drives the real `llmwiki` CLI end to end over the rung1 recipe fixture in a
fresh temp kb: ingest, re-ingest, summarize, dedup, lint, then three
retrieval questions run as real shell commands. Prints raw CLI output for
every step and asserts the phase 10 acceptance bar. Talks to a real model
endpoint through the CLI subprocess; this script makes no HTTP calls of its
own.

Run:
    LLM_WIKI_API_KEY_HOSTED=... LLM_WIKI_ENDPOINT_URL=... .venv/bin/python run_rung1.py
    LLM_WIKI_API_KEY_HOSTED=... LLM_WIKI_ENDPOINT_URL=... .venv/bin/python run_rung1.py --vocab

`--vocab` also runs the vocab-appendix pass (phase 10's second half): it
resummarizes every source and rebuilds dedup, doubling the paid calls.

The temp kb is never deleted. Its path is printed at the start and the end
so the lead can inspect it after the run.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]  # tests/fixtures/rung1 -> repo root
INBOX_DIR = FIXTURE_DIR / "inbox"
OVERLAY_DIR = FIXTURE_DIR / "kb"

sys.path.insert(0, str(REPO_ROOT))
from llmwiki.core import as_list, parse_frontmatter, slugify  # noqa: E402

# Fixed composition of the fixture: 12 readable recipes, 1 unreadable file.
# Hardcoded (not derived from the inbox listing) so a fixture that quietly
# grows or shrinks makes this assertion fail instead of silently adapting.
EXPECTED_NEW = 12
EXPECTED_EXISTS = 0
EXPECTED_FAILED = 1

RECORD_RE = re.compile(r"^(?:[0-9a-f]{64}|-)\t(?:new|exists|failed)\t\S")
SUFFIX_RE = re.compile(r"-[0-9a-f]{12}$")
PLANNED_RE = re.compile(r"summarize: (\d+) planned")

QUESTIONS = [
    (
        "which recipes use eggs",
        # The word boundary is load-bearing: a bare .*egg also matches
        # eggplant, and this box holds one.
        r"grep -ilE '^main_ingredients:.*\beggs?\b' *.md",
    ),
    (
        "which recipe has the shortest cook time",
        "grep -H '^cook_time_minutes:' *.md | sort -t: -k3 -n | head -5",
    ),
]
THIRD_QUESTION = (
    "which two sources describe the same dish",
    # Count members, not every block-list item: a story's identifiers list
    # uses the same "  - " marker, so a bare grep -c overcounts as soon as a
    # vocabulary is declared. `members` is written last, so everything from
    # that key to the closing --- is a member.
    """for f in $(grep -l '^kind: story' *.md); do """
    """printf '%s\t%s\n' "$f" """
    """"$(awk '/^members:/{f=1;next} /^---$/{f=0} f&&/^  - /{c++} END{print c+0}' "$f")"; """
    """done | sort -k2 -rn | head -20""",
)

STEP = itertools.count(1)
RESULTS: list[tuple[str, bool, str]] = []
ALL_STDOUT: list[str] = []


def record(name: str, passed: bool, reason: str) -> None:
    print(f"ASSERT {name}: {'PASS' if passed else 'FAIL'}")
    print(f"  {reason}")
    RESULTS.append((name, passed, reason))


def _print_raw(header: str, text: str) -> None:
    print(header)
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def replace_file(path: Path, content: str, announce: bool = True) -> None:
    path.write_text(content, encoding="utf-8")
    if announce:
        print(f"--- {path.name} (full):")
        sys.stdout.write(content)


def run_cli(kb_root: Path, label: str, verb_args: list[str]) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "llmwiki", "--kb", str(kb_root), *verb_args]
    print(f"=== STEP {next(STEP)}: {label}")
    print(f"$ {shlex.join(argv)}")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, env=env)
    print(f"--- exit: {result.returncode}")
    _print_raw("--- stdout:", result.stdout)
    _print_raw("--- stderr:", result.stderr)
    ALL_STDOUT.append(result.stdout)
    return result


def run_shell(label: str, cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    print(f"=== {label}")
    print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    print(f"--- exit: {result.returncode}")
    _print_raw("--- stdout:", result.stdout)
    _print_raw("--- stderr:", result.stderr)
    return result


def classify_records(stdout_text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Split ingest stdout into (records, other lines). A record is a line
    matching RECORD_RE that splits on tab into exactly 3 fields; anything
    else (a `planned` line, a `push` warning, blank lines) is "other". Never
    classify by "the line has a tab": a `push` line has two tabs too, but
    its first field is the literal word `push`, not a digest."""
    records, other = [], []
    for line in stdout_text.splitlines():
        parts = line.split("\t")
        if RECORD_RE.match(line) and len(parts) == 3:
            records.append((parts[0], parts[1], parts[2]))
        else:
            other.append(line)
    return records, other


def snapshot(kb_root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(kb_root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(kb_root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_summary_pages(wiki_dir: Path) -> list[tuple[Path, str]]:
    pages = []
    for path in sorted(wiki_dir.glob("*.md")):
        parsed = parse_frontmatter(read_text(path))
        if parsed is not None and parsed[0].get("kind") == "summary":
            pages.append((path, str(parsed[0].get("title", ""))))
    return pages


def load_story_pages(wiki_dir: Path) -> list[tuple[Path, list[str]]]:
    stories = []
    for path in sorted(wiki_dir.glob("*.md")):
        parsed = parse_frontmatter(read_text(path))
        if parsed is not None and parsed[0].get("kind") == "story":
            stories.append((path, as_list(parsed[0].get("members"))))
    return stories


def check_env_or_exit() -> None:
    missing = [
        n for n in ("LLM_WIKI_API_KEY_HOSTED", "LLM_WIKI_ENDPOINT_URL")
        if not os.environ.get(n)
    ]
    if missing:
        print(
            f"rung1: refusing to start, missing environment variable(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(2)


def print_intro(inbox_files: list[Path], vocab: bool) -> None:
    print("=== INBOX")
    for path in inbox_files:
        print(f"  {path.name}")
    planned = (len(inbox_files) - 1) * (2 if vocab else 1)
    print(f"PLANNED PAID MODEL CALLS: {planned}")


def overlay_base(kb_root: Path, endpoint_url: str) -> None:
    print(f"=== STEP {next(STEP)}: overlay config.toml, SCHEMA.md, SUMMARIZE.md")
    config = (
        read_text(OVERLAY_DIR / "config.toml")
        + f'\n[providers.hosted]\nurl = "{endpoint_url}"\n'
        + 'key_env = "LLM_WIKI_API_KEY_HOSTED"\n'
    )
    replace_file(kb_root / "config.toml", config)
    replace_file(kb_root / "SCHEMA.md", read_text(OVERLAY_DIR / "SCHEMA.md"), announce=False)
    replace_file(kb_root / "SUMMARIZE.md", read_text(OVERLAY_DIR / "SUMMARIZE.md"), announce=False)


def is_allowed_change(rel: str, kb_root: Path) -> bool:
    if rel.startswith("sources/") or rel.startswith("vectors/") or rel == "log.md":
        return True
    if rel.startswith("wiki/") and rel.endswith(".md"):
        parsed = parse_frontmatter(read_text(kb_root / rel))
        return parsed is not None and parsed[0].get("kind") in ("summary", "story")
    return False


def assert_ownership(kb_root: Path, baseline: dict[str, str]) -> None:
    current = snapshot(kb_root)
    changed = [rel for rel, h in current.items() if baseline.get(rel) != h]
    offending = [rel for rel in changed if not is_allowed_change(rel, kb_root)]
    index_exists = (kb_root / "wiki" / "index.md").exists()
    passed = not offending and not index_exists
    reason = f"offending paths: {offending or 'none'}; wiki/index.md exists: {index_exists}"
    record("ownership", passed, reason)


def assert_ingest_record_shape(records3, other3, records4, other4, n_inbox: int) -> None:
    print("-- step 3 classified records:")
    for r in records3:
        print("  " + "\t".join(r))
    print("-- step 3 non-record lines:")
    for line in other3:
        print("  " + line)
    print("-- step 4 classified records:")
    for r in records4:
        print("  " + "\t".join(r))
    print("-- step 4 non-record lines:")
    for line in other4:
        print("  " + line)
    passed = len(records3) == n_inbox and len(records4) == 1
    reason = f"step3 records={len(records3)} (expected {n_inbox}), step4 records={len(records4)} (expected 1)"
    record("ingest-record-shape", passed, reason)


def assert_ingest_counts(records3) -> None:
    statuses = [r[1] for r in records3]
    counts = (statuses.count("new"), statuses.count("exists"), statuses.count("failed"))
    expected = (EXPECTED_NEW, EXPECTED_EXISTS, EXPECTED_FAILED)
    record(
        "ingest-counts",
        counts == expected,
        f"got (new, exists, failed)={counts}, expected {expected}",
    )


def assert_ingest_log_line(kb_root: Path) -> None:
    log_text = read_text(kb_root / "log.md")
    pat3 = re.compile(
        rf"^## \[ingest\] manual {EXPECTED_NEW} new, {EXPECTED_EXISTS} exists, {EXPECTED_FAILED} failed",
        re.M,
    )
    pat4 = re.compile(r"^## \[ingest\] manual 0 new, 1 exists, 0 failed", re.M)
    passed = bool(pat3.search(log_text)) and bool(pat4.search(log_text))
    record(
        "ingest-log-line",
        passed,
        f"step3 pattern found: {bool(pat3.search(log_text))}, "
        f"step4 pattern found: {bool(pat4.search(log_text))}",
    )


def assert_readd(records3, records4) -> None:
    first_digest = records3[0][0] if records3 else None
    ok_shape = len(records4) == 1
    status4 = records4[0][1] if ok_shape else None
    digest4 = records4[0][0] if ok_shape else None
    passed = ok_shape and status4 == "exists" and digest4 == first_digest
    record(
        "readd-exists",
        passed,
        f"step4 record={records4}, step3 first file digest={first_digest}",
    )


def assert_readd_no_pipeline(step4_stdout: str) -> None:
    passed = "planned" not in step4_stdout
    record("readd-no-pipeline", passed, f'"planned" in step4 stdout: {"planned" in step4_stdout}')


def assert_failed_source_kept(kb_root: Path, records3) -> None:
    failed = [r for r in records3 if r[1] == "failed"]
    png_files = sorted(INBOX_DIR.glob("*.png"))
    if len(failed) != 1 or len(png_files) != 1:
        record(
            "failed-source-kept",
            False,
            f"expected exactly 1 failed record and 1 .png in inbox, "
            f"got {len(failed)} failed, {len(png_files)} png files",
        )
        return
    digest, _status, url = failed[0]
    real_digest = hashlib.sha256(png_files[0].read_bytes()).hexdigest()
    byte_files = [
        p for p in (kb_root / "sources").glob(f"{digest}.*") if p.suffix != ".toml"
    ]
    sidecar = kb_root / "sources" / f"{digest}.toml"
    passed = (
        url.endswith(png_files[0].name)
        and digest == real_digest
        and len(byte_files) == 1
        and sidecar.is_file()
    )
    record(
        "failed-source-kept",
        passed,
        f"failed url={url}, digest matches sha256 of {png_files[0].name}: "
        f"{digest == real_digest}, byte file present: {len(byte_files) == 1}, "
        f"sidecar present: {sidecar.is_file()}",
    )


def assert_slug_collision(summary_pages) -> list[Path]:
    groups: dict[str, list[Path]] = {}
    for path, title in summary_pages:
        groups.setdefault(slugify(title), []).append(path)
    collisions = {slug: paths for slug, paths in groups.items() if len(paths) > 1}
    if len(collisions) != 1:
        record("slug-collision", False, f"found {len(collisions)} collision groups, expected 1")
        return []
    [(slug, paths)] = collisions.items()
    suffixed = [p for p in paths if SUFFIX_RE.search(p.stem)]
    passed = len(paths) == 2 and len(suffixed) == 1
    record(
        "slug-collision",
        passed,
        f"slug {slug!r}: files={[p.name for p in paths]}, suffixed={[p.name for p in suffixed]}",
    )
    return sorted(paths)


def assert_lint_clean(lint_result: subprocess.CompletedProcess) -> None:
    passed = lint_result.returncode == 0 and lint_result.stdout == ""
    record(
        "lint-clean",
        passed,
        f"exit={lint_result.returncode}, stdout empty={lint_result.stdout == ''}",
    )


def run_base_pass(kb_root: Path, endpoint_url: str, inbox_files: list[Path]):
    run_cli(kb_root, "init kb", ["init"])
    overlay_base(kb_root, endpoint_url)
    baseline = snapshot(kb_root)

    step3 = run_cli(kb_root, "ingest all inbox files", ["ingest", *[str(p) for p in inbox_files]])
    step4 = run_cli(kb_root, "ingest re-add first file", ["ingest", str(inbox_files[0])])
    run_cli(kb_root, "summarize", ["summarize"])
    run_cli(kb_root, "dedup", ["dedup"])
    lint_result = run_cli(kb_root, "lint", ["lint"])
    return baseline, step3, step4, lint_result


def run_base_assertions(kb_root: Path, baseline, step3, step4, lint_result, n_inbox: int):
    records3, other3 = classify_records(step3.stdout)
    records4, other4 = classify_records(step4.stdout)
    assert_ingest_record_shape(records3, other3, records4, other4, n_inbox)
    assert_ingest_counts(records3)
    assert_ingest_log_line(kb_root)
    assert_readd(records3, records4)
    assert_readd_no_pipeline(step4.stdout)
    assert_failed_source_kept(kb_root, records3)
    summary_pages = load_summary_pages(kb_root / "wiki")
    collision_paths = assert_slug_collision(summary_pages)
    assert_lint_clean(lint_result)
    assert_ownership(kb_root, baseline)
    return summary_pages, collision_paths


def run_questions(kb_root: Path, questions) -> None:
    for text, cmd in questions:
        run_shell(f"QUESTION: {text}", cmd, kb_root / "wiki")


def dump_story_with_most_members(kb_root: Path, suffix: str = "") -> None:
    stories = load_story_pages(kb_root / "wiki")
    if not stories:
        print(f"=== DUMP: no story pages found{suffix}")
        return
    path, members = max(stories, key=lambda pair: len(pair[1]))
    print(f"=== DUMP: story page with most members{suffix}: {path.name} ({len(members)} members)")
    sys.stdout.write(read_text(path))


def dump_for_lead(kb_root: Path, summary_pages, collision_paths: list[Path]) -> None:
    run_shell("DUMP: ls -la .kb/wiki", "ls -la", kb_root / "wiki")
    if summary_pages:
        first_path = summary_pages[0][0]
        print(f"=== DUMP: first summary page alphabetically: {first_path.name}")
        sys.stdout.write(read_text(first_path))
    non_suffixed = [p for p in collision_paths if not SUFFIX_RE.search(p.stem)]
    collision_pick = (non_suffixed or collision_paths)[0] if collision_paths else None
    if collision_pick:
        print(f"=== DUMP: collision summary page: {collision_pick.name}")
        sys.stdout.write(read_text(collision_pick))
    dump_story_with_most_members(kb_root)
    print("=== DUMP: full log.md")
    sys.stdout.write(read_text(kb_root / "log.md"))


def assert_vocab_two_member_story(kb_root: Path) -> None:
    lengths = [len(members) for _path, members in load_story_pages(kb_root / "wiki")]
    passed = lengths.count(2) == 1 and all(n == 1 for n in lengths if n != 2)
    record("vocab-two-member-story", passed, f"story member counts: {lengths}")


def run_vocab_pass(kb_root: Path) -> None:
    print(f"=== STEP {next(STEP)}: append vocab overlay to config.toml and SUMMARIZE.md")
    cfg_path, sum_path = kb_root / "config.toml", kb_root / "SUMMARIZE.md"
    replace_file(cfg_path, read_text(cfg_path) + read_text(OVERLAY_DIR / "config.vocab-appendix.toml"))
    replace_file(sum_path, read_text(sum_path) + read_text(OVERLAY_DIR / "SUMMARIZE.vocab-appendix.md"))

    run_cli(kb_root, "summarize (vocab pass)", ["summarize"])
    run_cli(kb_root, "dedup --rebuild (vocab pass)", ["dedup", "--rebuild"])
    run_cli(kb_root, "lint (vocab pass)", ["lint"])

    print(f"=== STEP {next(STEP)}: re-run retrieval question 3 and story dump (vocab pass)")
    text, cmd = THIRD_QUESTION
    run_shell(f"QUESTION: {text} (vocab pass)", cmd, kb_root / "wiki")
    dump_story_with_most_members(kb_root, suffix=" (vocab pass)")
    assert_vocab_two_member_story(kb_root)


def paid_calls_observed() -> int:
    return sum(int(n) for text in ALL_STDOUT for n in PLANNED_RE.findall(text))


def print_final_summary(tmp_dir: Path) -> None:
    print("=== SUMMARY")
    for name, passed, _reason in RESULTS:
        print(f"{name}: {'PASS' if passed else 'FAIL'}")
    print(f"temp dir: {tmp_dir}")
    print(f"paid calls observed (summarize planned lines): {paid_calls_observed()}")


def main() -> int:
    check_env_or_exit()
    vocab = "--vocab" in sys.argv[1:]
    endpoint_url = os.environ["LLM_WIKI_ENDPOINT_URL"]

    tmp_dir = Path(tempfile.mkdtemp(prefix="rung1-"))
    print(f"TEMP DIR: {tmp_dir}")
    kb_root = tmp_dir / ".kb"

    inbox_files = sorted(INBOX_DIR.glob("*"), key=lambda p: p.name)
    print_intro(inbox_files, vocab)

    baseline, step3, step4, lint_result = run_base_pass(kb_root, endpoint_url, inbox_files)
    summary_pages, collision_paths = run_base_assertions(
        kb_root, baseline, step3, step4, lint_result, len(inbox_files)
    )
    run_questions(kb_root, QUESTIONS + [THIRD_QUESTION])
    dump_for_lead(kb_root, summary_pages, collision_paths)

    if vocab:
        run_vocab_pass(kb_root)

    print(f"TEMP DIR: {tmp_dir}")
    print_final_summary(tmp_dir)
    return 0 if all(passed for _n, passed, _r in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
