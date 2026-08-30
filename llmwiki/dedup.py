"""Story membership for summary pages, and the push warning.

One summary joins or starts a story by shared identifier. A judge model
(or, unconfigured, the first candidate) picks the story; the write is
self-linted before it is kept, same rollback pattern as `summarize`.
`push` then warns, on stdout and in the log, when the placed summary
shares an identifier with a page the CLI does not own.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

from llmwiki.core import (
    FrontmatterValue,
    Kb,
    Page,
    append_log_entry,
    as_list,
    atomic_write_text,
    parse_frontmatter,
    render_frontmatter,
    slugify,
)
from llmwiki.lint import lint_pages
from llmwiki.model import ModelError, chat, model_name

# The judge's own contract (decision agent-kb-0zf.5): a SUMMARIZE.md
# analogue is not needed here, this prompt is never user-editable.
# Wording measured against the live endpoint, same spirit as
# summarize.BUILT_IN_PROMPT: the plain "belongs to an existing story"
# framing let a shared identifier alone justify a match, joining an
# unrelated later event at the same place to an old story. The text
# below states the one-occurrence rule explicitly and was confirmed
# to give a reopening its own story while still joining a same-day
# duplicate update, 3 of 3 trials.
DEDUP_PROMPT = """\
Below is a new wiki summary page, then one or more existing story \
pages that already share at least one identifier with it.

A story covers ONE real-world occurrence. Answer with a candidate \
only when the new summary reports that SAME occurrence: an initial \
report, a later update, or a follow-up investigation into it.

Shared identifiers are NOT evidence of a match. A place, product, or \
person can appear in many unrelated events. A separate occurrence \
involving the same subject is a different story even when every \
identifier is identical.

Default to NONE. Answer with a candidate slug only if you are \
confident the two describe the same occurrence; otherwise answer \
NONE.

Reply with exactly one line: the candidate's slug, as written in its \
"=== CANDIDATE: <slug> ===" heading, or the literal word NONE. No \
other text, no explanation.

Before answering, ask yourself: if both pages were filed under one \
heading, would a reader see one event, or two things that merely \
happened in the same place? Two things means NONE.\
"""

SLUG_SUFFIX_LEN = 8  # hex chars of the first member hash, for a title collision


class Story(NamedTuple):
    path: Path
    fields: dict[str, FrontmatterValue]  # everything except members
    members: list[str]  # summary source hashes, arrival order


def normalise(value: str) -> str:
    """NFKC-normalise, casefold, strip, and collapse internal whitespace
    runs to one space, so an identifier joins on meaning, not spelling.
    The identifier KEY is never run through this."""
    folded = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", folded)


def _joined_form(ident: str) -> tuple[str, str]:
    """An identifier's join key: its key untouched, its value
    normalised. Split on the FIRST colon, same as `lint`."""
    key, _, value = ident.partition(":")
    return key, normalise(value)


def _joined_forms(identifiers: list[str]) -> set[tuple[str, str]]:
    return {_joined_form(ident) for ident in identifiers}


def candidates(
    page: Page, stories: Iterable[Story], extra: Sequence[Story] = ()
) -> list[Story]:
    """Every story sharing at least one identifier with `page`, unioned
    with `extra` (the phase 13 vector seam, empty this phase). Sorted by
    shared count descending, then `first_seen` ascending, then path name
    ascending for a total order."""
    page_forms = _joined_forms(as_list(page.fields.get("identifiers")))
    by_path: dict[Path, Story] = {}
    for story in (*stories, *extra):
        by_path.setdefault(story.path, story)

    scored = []
    for story in by_path.values():
        story_forms = _joined_forms(as_list(story.fields.get("identifiers")))
        shared = len(page_forms & story_forms)
        if shared:
            scored.append((shared, story))
    scored.sort(
        key=lambda pair: (
            -pair[0],
            str(pair[1].fields.get("first_seen") or ""),
            pair[1].path.name,
        )
    )
    return [story for _shared, story in scored]


def _dedup_model_id(kb: Kb) -> str | None:
    """The configured `[models] dedup` id, or `None` when it is unset
    (the deterministic-fallback path)."""
    try:
        return model_name(kb.config, "dedup", None)
    except ModelError:
        return None


def _judge_prompt(page: Page, cands: list[Story]) -> str:
    parts = [DEDUP_PROMPT, "=== NEW SUMMARY ===", page.path.read_text(encoding="utf-8")]
    for story in cands:
        parts.append(f"=== CANDIDATE: {story.path.stem} ===")
        parts.append(story.path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def judge(kb: Kb, page: Page, cands: list[Story]) -> Story | None:
    """Pick one of `cands` for `page`, or `None` for a new story. Makes
    no model call when `cands` is empty. A `ModelError` from `chat`
    propagates; the caller decides what that means for the run."""
    if not cands:
        return None
    if _dedup_model_id(kb) is None:
        return cands[0]

    reply = chat(kb.config, "dedup", _judge_prompt(page, cands))
    first_line = reply.split("\n", 1)[0].strip()
    for story in cands:
        if first_line == story.path.stem:
            return story
    if first_line != "NONE":
        digest = str(page.fields.get("source", ""))
        append_log_entry(
            kb.log, "dedup", f"{digest}: judge reply not a candidate or NONE: {first_line!r}"
        )
    return None


def _union_identifiers(members: list[str], summaries: dict[str, Page]) -> list[str]:
    """Union of the members' identifiers, member order, first spelling
    kept per normalised form."""
    seen: set[tuple[str, str]] = set()
    result: list[str] = []
    for digest in members:
        summary = summaries.get(digest)
        if summary is None:
            continue
        for ident in as_list(summary.fields.get("identifiers")):
            form = _joined_form(ident)
            if form not in seen:
                seen.add(form)
                result.append(ident)
    return result


def _seen_range(members: list[str], summaries: dict[str, Page]) -> tuple[str, str]:
    """min/max of the members' `fetched` values; a missing value, or a
    missing member, contributes the empty string."""
    fetched = [
        str(summaries[d].fields.get("fetched", "")) if d in summaries else ""
        for d in members
    ] or [""]
    return min(fetched), max(fetched)


def _story_body(story: Story, summaries: dict[str, Page]) -> str:
    """`## <member title>` then a blank line then that member's
    abstract, one section per member, in member order. Regenerated on
    every write, never carried over from a previous body."""
    sections = []
    for digest in story.members:
        summary = summaries.get(digest)
        if summary is None:
            continue
        title = str(summary.fields.get("title", ""))
        sections.append(f"## {title}\n\n{summary.body}")
    return "\n\n".join(sections)


def _story_path(kb: Kb, digest: str, title: str) -> Path:
    """Where a brand-new story lands: the slug, or the slug suffixed
    with the first 8 hex chars of the first member hash when the slug
    is already taken by a different page. Decided against the
    filesystem at write time."""
    candidate = kb.wiki / f"{slugify(title)}.md"
    if candidate.exists():
        return kb.wiki / f"{slugify(title)}-{digest[:SLUG_SUFFIX_LEN]}.md"
    return candidate


def _new_fields(
    title: str, identifiers: list[str], first_seen: str, last_seen: str, model_id: str
) -> dict[str, FrontmatterValue]:
    return {
        "kind": "story",
        "title": title,
        "identifiers": identifiers,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "model": model_id,
    }


def _write_story(
    kb: Kb, story: Story, digest: str, summary: Page, sources: dict[str, Page]
) -> bool:
    """Write `story` and the summary's `story:` back-reference, self-lint
    the pair, and roll both back on any finding (previous text restored,
    or the file unlinked when there was none). Returns False, leaving
    the summary story-less, when the write was dropped."""
    story_prev = story.path.read_text(encoding="utf-8") if story.path.is_file() else None
    summary_prev = summary.path.read_text(encoding="utf-8")

    story_fields = dict(story.fields)
    story_fields["members"] = story.members
    atomic_write_text(
        story.path, render_frontmatter(story_fields, _story_body(story, sources))
    )

    summary_fields = dict(summary.fields)
    summary_fields["story"] = story.path.stem
    atomic_write_text(summary.path, render_frontmatter(summary_fields, summary.body))

    findings = lint_pages(kb.root, [story.path, summary.path])
    if not findings:
        return True

    if story_prev is None:
        story.path.unlink()
    else:
        atomic_write_text(story.path, story_prev)
    atomic_write_text(summary.path, summary_prev)
    finding = findings[0]
    append_log_entry(
        kb.log, "dedup", f"{digest}: dropped ({finding.check}: {finding.detail})"
    )
    return False


def push(kb: Kb, page: Page, agent_pages: list[Page]) -> int:
    """Warn, one stdout line and one log line per match, for every
    agent page (kind neither `summary` nor `story`) sharing an
    identifier with `page`. No model call, no page edit."""
    page_forms = _joined_forms(as_list(page.fields.get("identifiers")))
    digest = str(page.fields.get("source", ""))
    warned = 0
    for agent_page in agent_pages:
        agent_idents = as_list(agent_page.fields.get("identifiers"))
        shared_forms = page_forms & _joined_forms(agent_idents)
        if not shared_forms:
            continue
        shared = sorted(
            ident for ident in agent_idents if _joined_form(ident) in shared_forms
        )
        print(f"push\t{agent_page.path}\t{','.join(shared)}", file=sys.stderr)
        append_log_entry(
            kb.log, "push", f"{digest} touches {agent_page.path} ({len(shared)} shared)"
        )
        warned += 1
    return warned


def _load_wiki(kb: Kb) -> tuple[dict[str, Page], dict[Path, Story], list[Page]]:
    """One pass over `wiki/*.md`: summaries keyed by source hash,
    stories keyed by path, and every other (agent-owned) page. An
    unparseable page is skipped, never a crash."""
    summaries: dict[str, Page] = {}
    stories: dict[Path, Story] = {}
    agent_pages: list[Page] = []
    for path in sorted(kb.wiki.glob("*.md")):
        parsed = parse_frontmatter(path.read_text(encoding="utf-8"))
        if parsed is None:
            continue
        fields, body = parsed
        kind = fields.get("kind")
        if kind == "summary":
            source = str(fields.get("source", ""))
            if source:
                summaries[source] = Page(path, fields, body)
        elif kind == "story":
            members = as_list(fields.pop("members", None))
            stories[path] = Story(path, fields, members)
        else:
            agent_pages.append(Page(path, fields, body))
    return summaries, stories, agent_pages


def _place_summary(
    kb: Kb, digest: str, summary: Page, summaries: dict[str, Page], stories: dict[Path, Story]
) -> tuple[Story, str] | None:
    """Join or start a story for `summary`. Returns `(story, action)`
    with `action` "new" or "joined" on success, `None` when self-lint
    dropped the write."""
    cands = candidates(summary, stories.values())
    target = judge(kb, summary, cands)
    model_id = _dedup_model_id(kb) or "none"

    if target is None:
        members = [digest]
        title = str(summary.fields.get("title", ""))
        path = _story_path(kb, digest, title)
        action = "new"
    else:
        members = target.members if digest in target.members else [*target.members, digest]
        title = str(target.fields.get("title", ""))
        path = target.path
        action = "joined"

    identifiers = _union_identifiers(members, summaries)
    first_seen, last_seen = _seen_range(members, summaries)
    fields = _new_fields(title, identifiers, first_seen, last_seen, model_id)
    story = Story(path, fields, members)

    if not _write_story(kb, story, digest, summary, {**summaries, digest: summary}):
        return None
    return story, action


def _target_digests(digests: list[str] | None, summaries: dict[str, Page]) -> list[str]:
    """Every requested digest, or every summary lacking a `story:`
    field when `None`. A digest whose summary already carries a
    non-empty `story:` field is never a target, whether it was named
    explicitly or not: moving a summary between stories is
    `--rebuild`'s job (phase 8), not this one's. A digest with no
    summary page at all still passes through, so `run` logs its own
    drop for that case."""
    pool = list(summaries) if digests is None else digests
    return [
        digest
        for digest in pool
        if digest not in summaries or not summaries[digest].fields.get("story")
    ]


def _ordered(digests: list[str], summaries: dict[str, Page]) -> list[str]:
    def key(digest: str) -> tuple[str, str]:
        page = summaries.get(digest)
        fetched = str(page.fields.get("fetched", "")) if page else ""
        return fetched, digest

    return sorted(digests, key=key)


def _replay(
    kb: Kb,
    targets: list[str],
    summaries: dict[str, Page],
    stories: dict[Path, Story],
    agent_pages: list[Page],
) -> int:
    """Place a story for each digest in `targets`, in order, accumulating
    into `stories`. Returns the number placed; fewer than len(targets)
    means something was dropped or a model error cut the run short."""
    placed = 0
    for digest in targets:
        summary = summaries.get(digest)
        if summary is None:
            append_log_entry(kb.log, "dedup", f"{digest}: dropped (no summary page for source)")
            continue
        try:
            result = _place_summary(kb, digest, summary, summaries, stories)
        except ModelError as exc:
            print(f"llmwiki: dedup: {exc}", file=sys.stderr)
            break
        if result is None:
            continue
        story, action = result
        stories[story.path] = story
        append_log_entry(kb.log, "dedup", f"{digest} -> {story.path.stem} ({action})")
        push(kb, summary, agent_pages)
        placed += 1

    return placed


def rebuild(root: Path) -> int:
    """Discard every story page and replay every summary from scratch
    (decision `.5`): the repair for a placement a lost race or ordering
    got wrong. Returns 1 if anything was dropped on replay, else 0."""
    kb = Kb(root)
    summaries, stories, _agent_pages = _load_wiki(kb)

    for story in stories.values():
        story.path.unlink()

    for summary in summaries.values():
        # A summary that never carried `story:` must come back off this
        # loop byte-identical, so only a non-None pop triggers a write.
        removed = summary.fields.pop("story", None)
        if removed is not None:
            atomic_write_text(
                summary.path, render_frontmatter(summary.fields, summary.body)
            )

    targets = _ordered(list(summaries), summaries)
    print(f"dedup: {len(targets)} planned")

    # `rebuilt` starts empty, never seeded from `stories`: every loaded
    # story page was just deleted above, so there is nothing to carry
    # forward.
    rebuilt: dict[Path, Story] = {}
    # `[]`, not `_agent_pages`: this IS decision `.22`, push does not
    # run at rebuild. Every summary being replayed here already had its
    # push warning emitted the first time it was placed; firing it
    # again on every rebuild would just be noise.
    placed = _replay(kb, targets, summaries, rebuilt, [])

    append_log_entry(
        kb.log, "dedup", f"rebuild {placed} summaries into {len(rebuilt)} stories"
    )
    return 0 if placed == len(targets) else 1


def run(root: Path, digests: list[str] | None) -> int:
    """Place a story for each of `digests`, or every summary lacking a
    `story:` field when `None`, in `(fetched, hash)` order. Returns 1 if
    anything was dropped, else 0."""
    kb = Kb(root)
    summaries, stories, agent_pages = _load_wiki(kb)
    targets = _ordered(_target_digests(digests, summaries), summaries)
    print(f"dedup: {len(targets)} planned")
    placed = _replay(kb, targets, summaries, stories, agent_pages)
    return 0 if placed == len(targets) else 1
