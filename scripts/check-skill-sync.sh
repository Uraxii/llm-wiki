#!/usr/bin/env bash
set -eu

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)

dotai_root="${DOTAI:-$HOME/dotai}"
skill_rel="skills/llm-wiki/SKILL.md"
repo_skill="$repo_root/$skill_rel"
dotai_skill="$dotai_root/$skill_rel"
pyproject="$repo_root/pyproject.toml"

manifests=(
  ".claude-plugin/plugin.json"
  ".codex-plugin/plugin.json"
  "plugin.json"
)

failures=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }

read_pyproject_version() {
  grep -m1 -E '^version[[:space:]]*=' "$pyproject" \
    | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/'
}

read_manifest_version() {
  grep -m1 '"version"' "$1" \
    | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/'
}

check_skill_copy() {
  if [ ! -f "$dotai_skill" ]; then
    skip "skill copy: no dotai checkout at $dotai_skill"
    return
  fi
  if [ ! -f "$repo_skill" ]; then
    fail "skill copy: repo file missing at $repo_skill"
    return
  fi
  if diff -q "$repo_skill" "$dotai_skill" >/dev/null; then
    pass "skill copy: $repo_skill matches $dotai_skill"
  else
    fail "skill copy: $repo_skill differs from $dotai_skill"
  fi
}

check_manifest_versions() {
  if [ ! -f "$pyproject" ]; then
    fail "manifest versions: pyproject.toml missing"
    return
  fi
  local want
  want=$(read_pyproject_version)
  if [ -z "$want" ]; then
    fail "manifest versions: no version line in pyproject.toml"
    return
  fi
  local rel path got
  for rel in "${manifests[@]}"; do
    path="$repo_root/$rel"
    if [ ! -f "$path" ]; then
      fail "manifest versions: $rel is missing"
      continue
    fi
    got=$(read_manifest_version "$path")
    if [ -z "$got" ]; then
      fail "manifest versions: $rel has no version field"
    elif [ "$got" != "$want" ]; then
      fail "manifest versions: $rel is $got, pyproject.toml is $want"
    else
      pass "manifest versions: $rel is $want"
    fi
  done
}

check_skill_copy
check_manifest_versions

echo "---"
if [ "$failures" -eq 0 ]; then
  echo "summary: all checks passed"
  exit 0
fi
echo "summary: $failures check(s) failed"
exit 1
