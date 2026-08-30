"""The serial per-source ingest pipeline: store bytes and provenance,
then summarize, embed, and dedup each new one. One door for every source
kind (decision agent-kb-0zf.6); the collector itself knows nothing about
page kinds.
"""
from __future__ import annotations

import mimetypes
import sys
from pathlib import Path
from typing import NamedTuple

from llmwiki.core import Kb, append_log_entry, as_list, flatten
from llmwiki.model import ModelError, model_name
from llmwiki import dedup, fetch, feeds, sources, summarize, vectors

JOB = "manual"  # the job name for a plain `ingest`; `run_job` threads a real one
STDIN_ARG = "-"
NO_DIGEST = "-"  # stdout hash column when the bytes never got stored


class Source(NamedTuple):
    data: bytes
    url: str
    content_type: str


def ingest_stdin() -> Source:
    return Source(sys.stdin.buffer.read(), STDIN_ARG, "text/plain")


def ingest_path(arg: str) -> Source:
    """Lets OSError out (missing file, a directory, permissions): the
    caller's broad except turns any of those into one `failed` line."""
    path = Path(arg).resolve()
    content_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    data = path.read_bytes()
    return Source(data, str(path), content_type)


def ingest_url(arg: str) -> Source:
    """Fetch a url under fetch.py's guard and reduce it to storable
    bytes. Lets FetchError out: the caller's broad except turns it into
    one `failed` line, the same as a missing file does.

    Source.url is the CLEANED REQUESTED url, not the fetch's final_url:
    phase 12's `partial` job mode skips urls that already have a
    provenance file, and it looks them up by the url the feed gave, so
    recording a redirect target would make it refetch forever.
    """
    url = fetch.clean_url(arg)
    _final_url, content_type, data = fetch.fetch(url)
    content_type, data = fetch.extract(content_type, data)
    return Source(data, url, content_type)


def _sweep_or_fail(kb: Kb) -> bool:
    """Runs the phase 13 embed sweep, but only when `[models] embed` is
    configured: a kb with no embed model behaves exactly as it did
    before vectors.py existed (no embed step, never a pipeline
    failure). Once configured, a genuine `ModelError` from the sweep
    itself IS a pipeline failure. Returns False only for that case."""
    try:
        model_name(kb.config, "embed", None)
    except ModelError:
        return True
    try:
        vectors.sweep(kb)
    except ModelError:
        return False
    return True


def _pipeline(root: Path, digest: str) -> str | None:
    """Returns the name of the failing step, or `None` when clean."""
    kb = Kb(root)
    if summarize.run(root, [digest]) != 0:
        return "summarize"
    # Phase 13 embeds the new summary page HERE, between summarize and
    # dedup, where dedup's vector-candidate seam (`candidates(..., extra=)`)
    # wants it: the embedding must exist before dedup can consult it.
    #
    # Skipping dedup after a failed summarize is deliberate: with no
    # summary page on disk dedup would log a junk "dropped (no summary
    # page for source)" line into the append-only log.
    if not _sweep_or_fail(kb):
        return "embed"
    if dedup.run(root, [digest]) != 0:
        return "dedup"
    # dedup may write a new story page (a join or a fresh one) and
    # always rewrites the summary's own frontmatter (the `story:`
    # back-reference), so both sweeps here are real, not redundant: the
    # first sweep's vector for the summary is stale the moment dedup
    # touches it. Sweeping again is how "no wiki page lacks a current
    # vector after ingest" holds even for the story the last source in
    # a batch creates.
    if not _sweep_or_fail(kb):
        return "embed"
    return None


def _fail(kb: Kb, url: str, reason: str) -> None:
    # stderr first: if log.md is missing, append_log_entry raises and
    # the real reason must already be visible, not hidden behind that.
    print(f"llmwiki: ingest: {url}: {reason}", file=sys.stderr)
    append_log_entry(kb.log, "ingest", f"{url}: {reason}")


def _ingest_one(kb: Kb, arg: str, job: str = JOB) -> tuple[str, str, str]:
    """Returns (digest_or_NO_DIGEST, "new"|"exists"|"failed", url). `url`
    starts as `arg` and is replaced by the read source's own url once the
    read succeeds, so a failed read still reports what the user typed."""
    url = arg
    digest = NO_DIGEST  # a failure after store still reports the real hash
    try:
        if arg == STDIN_ARG:
            source = ingest_stdin()
        elif fetch.is_url(arg):
            source = ingest_url(arg)
        else:
            source = ingest_path(arg)
        url = source.url
        if not source.data:
            _fail(kb, url, "empty source")
            return NO_DIGEST, "failed", url

        digest, status = sources.store(
            kb, source.data, source.url, source.content_type, job
        )
        if status == "exists":
            return digest, "exists", url

        step = _pipeline(kb.root, digest)
        if step is not None:
            _fail(kb, url, f"{step} failed")
            return digest, "failed", url
        return digest, "new", url
    except Exception as exc:
        # One bad source must never stop the batch; KeyboardInterrupt is
        # a BaseException and is correctly not caught here.
        _fail(kb, url, f"{type(exc).__name__}: {exc}")
        return digest, "failed", url


def run(root: Path, args: list[str], job: str = JOB) -> int:
    kb = Kb(root)
    _sweep_or_fail(kb)  # best-effort: pre-existing stale pages, never blocks the batch

    tally = {"new": 0, "exists": 0, "failed": 0}
    for arg in args:
        digest, status, url = _ingest_one(kb, arg, job)
        # flatten: this is a report line, not the provenance record (the
        # sidecar keeps the raw url); a url holding a tab or newline must
        # not turn one record into more than three fields.
        print(f"{digest}\t{status}\t{flatten(url)}")
        tally[status] += 1

    append_log_entry(
        kb.log,
        "ingest",
        f"{job} {tally['new']} new, {tally['exists']} exists, {tally['failed']} failed",
    )
    return 1 if tally["failed"] else 0


def run_job(root: Path, name: str) -> int:
    """Run one `[jobs.<name>]` declared in config.toml: read its items
    from a feed or a literal url list, apply `partial`/`full` mode, then
    hand the surviving urls to the same per-source path `run` uses."""
    kb = Kb(root)
    job_config = kb.config.get("jobs", {}).get(name)
    if not isinstance(job_config, dict):
        print(f"llmwiki: ingest: no [jobs.{name}] in config.toml", file=sys.stderr)
        return 2

    feed_url = job_config.get("feed")
    url_list = job_config.get("urls")
    if (feed_url is None) == (url_list is None):
        print(
            f"llmwiki: ingest: [jobs.{name}] needs exactly one of feed or urls",
            file=sys.stderr,
        )
        return 2

    if feed_url is not None and not isinstance(feed_url, str):
        print(f"llmwiki: ingest: [jobs.{name}] feed must be a string", file=sys.stderr)
        return 2

    if url_list is not None:
        # as_list: a bare string typo where a list belongs (`urls = "http://x"`)
        # becomes the one obviously intended url, not one ingest attempt per
        # character.
        url_list = as_list(url_list)
        if not all(isinstance(u, str) for u in url_list):
            print(f"llmwiki: ingest: [jobs.{name}] urls must be strings", file=sys.stderr)
            return 2

    mode = job_config.get("mode", "partial")
    if mode not in ("partial", "full"):
        print(
            f"llmwiki: ingest: [jobs.{name}] mode {mode!r} is not partial or full",
            file=sys.stderr,
        )
        return 2

    if feed_url is not None:
        try:
            items = feeds.load(feed_url)
        except (feeds.FeedError, fetch.FetchError) as exc:
            _fail(kb, feed_url, str(exc))
            return 1
    else:
        items = [feeds.Item(u, "") for u in url_list]

    # Provenance stores the cleaned url (ingest_url's own doing), so the
    # partial seen-check must clean before it compares: a raw feed url
    # carrying a utm_source would miss every time and the job would
    # refetch forever.
    cleaned = [fetch.clean_url(item.url) for item in items]
    if mode == "partial":
        seen = sources.stored_urls(kb)
        cleaned = [url for url in cleaned if url not in seen]

    return run(root, cleaned, name)
