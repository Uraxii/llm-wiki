"""Tests for the skeptic-gate findings on llm_wiki.py.

Gate 1: slug collisions (1), whitespace-only stdin wiping a body (2),
the sources/ add/add TOCTOU (3), index drift under concurrency (4),
closed/silent stdin (7, 8), frontmatter field loss on re-stamp (9), and
control characters leaking into a flattened field (10). Body-preservation
coverage doubles as the regression test for finding 5 (no
path.write_text truncation on crash) and finding 15 (leading blank
lines in a body).

Gate 2: the index lock is gone (index.md may lag under concurrent
writers but is never torn, and `index` always repairs it); `page`
always reads a real body from stdin, no timeout, and `--touch` is the
only supported re-stamp-without-a-body path; `add --url`'s scheme/host
guard runs before anything optional (lxml) is imported, so a bad scheme
is provably the guard firing and not an accident of import order; and
the title-collision guard normalizes before comparing so genuinely
cosmetic differences (casing, whitespace, NFD/NFC) update the same
page instead of refusing. A shared prefix past MAX_SLUG_LEN is NOT
cosmetic: the compare runs on the full, untruncated title, so two
titles that only agree for the first 120 characters still refuse as a
collision instead of one silently overwriting the other's body (gate
3, finding F1).

Gate 3: F2 -- `--touch` used to sniff whether stdin was a FIFO to
decide if a body arrived, which missed a regular-file-backed heredoc
(shell-dependent, including any heredoc over ~64KB in bash) and let it
silently discard the stored body. `--touch` now reads stdin like any
other call and refuses only if it holds real content, so a pipe, a
`< file` redirect, and a heredoc of any size are refused identically.

Anything that touches stdin or real concurrency runs the CLI as a real
subprocess: reading stdin to EOF needs an actual file descriptor, which
an io.StringIO stand-in for sys.stdin cannot provide. Pure functions
are exercised directly. Every kb lives under tmp_path; nothing here
ever touches a real store or the network.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import email.message
import os
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.error
from pathlib import Path

import pytest

import llm_wiki

SCRIPT = Path(llm_wiki.__file__).resolve()


def run_cli(
    args: list[str],
    kb: Path | None = None,
    input_text: str | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI as a real subprocess. `--kb` is always explicit so
    a test can never fall back to a real store. `--kb` is a per-subcommand
    option, so it must come after the subcommand token, not before it."""
    full_args = [sys.executable, str(SCRIPT), *args]
    if kb is not None:
        full_args += ["--kb", str(kb)]
    return subprocess.run(
        full_args, input=input_text, text=True, capture_output=True, timeout=timeout
    )


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    """A freshly-initialized kb under tmp_path."""
    kb_path = tmp_path / ".kb"
    result = run_cli(["init", str(kb_path)])
    assert result.returncode == 0, result.stderr
    return kb_path


def wiki_pages(kb: Path) -> list[Path]:
    return [p for p in (kb / "wiki").glob("*.md") if p.name != "index.md"]


def index_data_rows(kb: Path) -> list[str]:
    text = (kb / "wiki" / "index.md").read_text(encoding="utf-8")
    return [
        line
        for line in text.splitlines()
        if line.startswith("| ") and not line.startswith("| Page") and "---|" not in line
    ]


# --- stdin matrix (findings 2, 7, 8) ----------------------------------


def test_stdin_tty_errors_without_crashing(kb: Path) -> None:
    pty = pytest.importorskip("pty")
    master_fd, slave_fd = pty.openpty()
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "page", "Tty Page", "--kb", str(kb)],
            stdin=slave_fd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        os.close(slave_fd)
        os.close(master_fd)
    assert "Traceback" not in result.stderr
    assert result.returncode != 0
    assert not (kb / "wiki" / "tty-page.md").exists()


def test_stdin_closed_fd_does_not_crash(kb: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "page", "Closed Fd Page", "--kb", str(kb)],
        stdin=subprocess.DEVNULL,
        preexec_fn=lambda: os.close(0),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert "Traceback" not in result.stderr
    assert result.returncode != 0
    assert not (kb / "wiki" / "closed-fd-page.md").exists()


def test_stdin_empty_does_not_create_a_page(kb: Path) -> None:
    result = run_cli(["page", "Empty Stdin Page"], kb=kb, input_text="")
    assert result.returncode != 0
    assert not (kb / "wiki" / "empty-stdin-page.md").exists()


def test_stdin_whitespace_only_is_a_loud_error_not_a_silent_no_op(kb: Path) -> None:
    """A select()-based read timeout used to treat "no bytes yet" (and,
    separately, whitespace-only input) as "keep the old body", silently
    discarding whatever a slow or nearly-empty producer actually sent.
    `page` now requires a real body on every non-`--touch` call, so
    whitespace-only stdin is a loud error and the file is left alone."""
    run_cli(["page", "Ws Page"], kb=kb, input_text="original body\n")
    path = kb / "wiki" / "ws-page.md"
    before_body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1]

    result = run_cli(["page", "Ws Page"], kb=kb, input_text="   \n\t  \n")

    assert result.returncode != 0
    after_body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1]
    assert before_body == after_body == "original body\n"


def test_stdin_slow_producer_delivers_the_full_body(kb: Path) -> None:
    """Regression for the second skeptic-gate finding: a select() timeout
    used to read "no bytes yet" as "empty stdin" and silently re-stamp
    the old body while reporting success, with a body that arrived a
    moment late simply gone. `page` now reads stdin to EOF with no
    timeout, so a producer that pauses before writing still lands."""
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "page", "Slow Producer Page", "--kb", str(kb)],
        stdin=read_fd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(read_fd)
    try:
        time.sleep(1.5)  # longer than the old (now-deleted) poll timeout
        os.write(write_fd, b"THE REAL NEW BODY\n")
    finally:
        os.close(write_fd)
    _, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0, stderr
    path = kb / "wiki" / "slow-producer-page.md"
    body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1]
    assert body == "THE REAL NEW BODY\n"


# --- --touch (finding 2's replacement contract) ------------------------


def test_touch_restamps_without_reading_stdin(kb: Path) -> None:
    run_cli(["page", "Touch Page"], kb=kb, input_text="original body\n")
    path = kb / "wiki" / "touch-page.md"
    before_fields, before_body = llm_wiki.parse_frontmatter(
        path.read_text(encoding="utf-8")
    )

    time.sleep(1.1)  # `updated` has second precision
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "page", "Touch Page", "--touch", "--kb", str(kb)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    after_fields, after_body = llm_wiki.parse_frontmatter(
        path.read_text(encoding="utf-8")
    )
    assert after_body == before_body == "original body\n"
    assert after_fields["updated"] != before_fields["updated"]


def test_touch_with_piped_stdin_is_a_loud_error(kb: Path) -> None:
    """`input_text=` on `subprocess.run` is backed by an anonymous pipe,
    the one fd type the old `S_ISFIFO` sniff caught."""
    run_cli(["page", "Touch Conflict Page"], kb=kb, input_text="original body\n")
    result = run_cli(
        ["page", "Touch Conflict Page", "--touch"], kb=kb, input_text="new body\n"
    )
    assert result.returncode != 0
    path = kb / "wiki" / "touch-conflict-page.md"
    assert llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1] == "original body\n"


@pytest.mark.parametrize("size", [1_000, 70_000])
def test_touch_with_regular_file_stdin_is_refused_regardless_of_size(
    kb: Path, tmp_path: Path, size: int
) -> None:
    """Finding F2: a plain `< file` redirect is a regular file, not a
    FIFO, and so is a shell heredoc once it crosses that shell's
    pipe-buffering threshold (bash: ~64KB). The old `S_ISFIFO` sniff let
    both of those through and silently discarded the stored body.
    `--touch` must refuse a non-empty body the same way at 1KB and at
    70KB, since it no longer looks at fd type at all."""
    run_cli(["page", "Touch File Page"], kb=kb, input_text="original body\n")
    body_file = tmp_path / "body.txt"
    body_file.write_text("x" * size)
    with body_file.open(encoding="utf-8") as handle:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "page", "Touch File Page", "--touch", "--kb", str(kb)],
            stdin=handle,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode != 0
    path = kb / "wiki" / "touch-file-page.md"
    assert llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1] == "original body\n"


def test_touch_requires_an_existing_page(kb: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "page", "New Touch Page", "--touch", "--kb", str(kb)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert not (kb / "wiki" / "new-touch-page.md").exists()


def test_stdin_real_body_creates_page(kb: Path) -> None:
    result = run_cli(["page", "Real Body Page"], kb=kb, input_text="hello\n")
    assert result.returncode == 0
    assert (kb / "wiki" / "real-body-page.md").exists()


# --- page body preservation (findings 5, 15, binding invariant) -------


def test_page_body_survives_dashes_fake_frontmatter_unicode_crlf(kb: Path) -> None:
    body = (
        "intro line\n"
        "\n"
        "---\n"
        "not: real frontmatter\n"
        "unicode: 日本語 \U0001f389\n"
        "a line with CRLF\r\n"
        "\n"
        "\n"
        "two blank lines above, then this\n"
    )
    result = run_cli(["page", "Tricky Body Page"], kb=kb, input_text=body)
    assert result.returncode == 0
    path = kb / "wiki" / "tricky-body-page.md"
    with path.open(encoding="utf-8", newline="") as handle:
        stored = handle.read()
    stored_body = llm_wiki.parse_frontmatter(stored)[1]
    assert stored_body == body

    # --touch must not touch the body at all.
    result2 = subprocess.run(
        [sys.executable, str(SCRIPT), "page", "Tricky Body Page", "--touch", "--kb", str(kb)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result2.returncode == 0, result2.stderr
    with path.open(encoding="utf-8", newline="") as handle:
        stored2 = handle.read()
    assert llm_wiki.parse_frontmatter(stored2)[1] == body


def test_page_no_trailing_newline_round_trips_exactly(kb: Path) -> None:
    run_cli(["page", "No Trailing Newline"], kb=kb, input_text="no newline at the end")
    path = kb / "wiki" / "no-trailing-newline.md"
    with path.open(encoding="utf-8", newline="") as handle:
        body = llm_wiki.parse_frontmatter(handle.read())[1]
    assert body == "no newline at the end"


def test_page_preserves_unknown_frontmatter_fields_on_restamp(kb: Path) -> None:
    path_str = run_cli(["page", "Tagged Page"], kb=kb, input_text="body\n").stdout.strip()
    path = Path(path_str)
    original = path.read_text(encoding="utf-8")
    fields, body = llm_wiki.parse_frontmatter(original)
    fields["tags"] = "alpha,beta"
    fields["owner"] = "nicole"
    llm_wiki.atomic_write_text(path, llm_wiki.render_frontmatter(fields) + "\n" + body)

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), "page", "Tagged Page", "--touch",
            "--summary", "updated summary", "--kb", str(kb),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr

    new_fields, new_body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert new_fields["tags"] == "alpha,beta"
    assert new_fields["owner"] == "nicole"
    assert new_fields["summary"] == "updated summary"
    assert new_body == body
    assert list(new_fields)[:4] == list(llm_wiki.PAGE_FIELDS)


# --- slug collisions (finding 1) ---------------------------------------


def test_slugify_distinct_non_ascii_titles_get_distinct_slugs() -> None:
    assert llm_wiki.slugify("日本語ソース") != llm_wiki.slugify(
        "한국어"
    )


def test_page_same_title_twice_updates_the_same_file(kb: Path) -> None:
    run_cli(["page", "Same Title"], kb=kb, input_text="first\n")
    result = run_cli(
        ["page", "Same Title", "--summary", "s2"], kb=kb, input_text="second\n"
    )
    assert result.returncode == 0
    pages = wiki_pages(kb)
    assert len(pages) == 1
    assert llm_wiki.parse_frontmatter(pages[0].read_text(encoding="utf-8"))[1] == "second\n"


def test_page_ascii_slug_collision_is_a_loud_error_not_an_overwrite(kb: Path) -> None:
    assert llm_wiki.slugify("C++") == llm_wiki.slugify("C#")  # the actual collision

    first = run_cli(["page", "C++"], kb=kb, input_text="cpp body\n")
    assert first.returncode == 0

    second = run_cli(["page", "C#"], kb=kb, input_text="csharp body\n")
    assert second.returncode != 0

    path = kb / "wiki" / f"{llm_wiki.slugify('C++')}.md"
    assert llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1] == "cpp body\n"
    assert len(wiki_pages(kb)) == 1


# --- title-collision guard normalizes before comparing (gate 2, item D) -


def test_page_title_case_difference_updates_the_same_page(kb: Path) -> None:
    run_cli(["page", "widget catalog"], kb=kb, input_text="v1\n")
    result = run_cli(["page", "Widget Catalog"], kb=kb, input_text="v2\n")
    assert result.returncode == 0, result.stderr
    assert len(wiki_pages(kb)) == 1


def test_page_title_doubled_whitespace_updates_the_same_page(kb: Path) -> None:
    run_cli(["page", "Widget Catalog"], kb=kb, input_text="v1\n")
    result = run_cli(["page", "Widget  Catalog"], kb=kb, input_text="v2\n")
    assert result.returncode == 0, result.stderr
    assert len(wiki_pages(kb)) == 1


def test_page_title_nfd_vs_nfc_updates_the_same_page(kb: Path) -> None:
    nfc = unicodedata.normalize("NFC", "Café Notes")
    nfd = unicodedata.normalize("NFD", "Café Notes")
    run_cli(["page", nfc], kb=kb, input_text="v1\n")
    result = run_cli(["page", nfd], kb=kb, input_text="v2\n")
    assert result.returncode == 0, result.stderr
    assert len(wiki_pages(kb)) == 1


def test_page_title_shared_120_char_prefix_is_a_collision_not_a_merge(kb: Path) -> None:
    """Finding F1: the compare guard used to truncate both titles to
    MAX_SLUG_LEN before comparing, so two different titles sharing a
    120-char prefix compared equal and the second write silently
    overwrote the first body. Both bodies must survive: the first write
    lands, the second is refused, and the first body is untouched."""
    prefix = (
        "Widget Catalog: Regional Distribution and Supply Chain Analysis "
        "for the North American Market, Full Year Report, Volume One"
    )
    assert len(prefix) >= llm_wiki.MAX_SLUG_LEN

    first_title = f"{prefix}: Introduction"
    second_title = f"{prefix}: Appendices"
    assert llm_wiki.slugify(first_title) == llm_wiki.slugify(second_title)

    first = run_cli(["page", first_title], kb=kb, input_text="IRREPLACEABLE intro body\n")
    assert first.returncode == 0, first.stderr

    second = run_cli(["page", second_title], kb=kb, input_text="TOTALLY DIFFERENT appendix body\n")
    assert second.returncode != 0

    path = kb / "wiki" / f"{llm_wiki.slugify(first_title)}.md"
    assert llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1] == (
        "IRREPLACEABLE intro body\n"
    )
    assert len(wiki_pages(kb)) == 1


def test_page_title_genuine_mismatch_errors_with_a_recovery_hint(kb: Path) -> None:
    run_cli(["page", "C++"], kb=kb, input_text="cpp body\n")
    result = run_cli(["page", "C#"], kb=kb, input_text="csharp body\n")
    assert result.returncode != 0
    assert "C++" in result.stderr
    assert "C#" in result.stderr
    assert "exact title" in result.stderr  # actionable, not just a report


def test_page_with_no_stored_title_adopts_the_new_one(kb: Path) -> None:
    """Finding F5: a page with missing/unterminated frontmatter has no
    `title` to compare against. The old guard demanded the caller "pass
    its exact title back", quoting an empty string -- impossible to
    satisfy. It must instead just adopt the new title."""
    path = kb / "wiki" / "untitled-page.md"
    path.write_text(
        llm_wiki.render_frontmatter({"summary": "", "category": "", "updated": "x"})
        + "\nold body\n",
        encoding="utf-8",
    )
    result = run_cli(["page", "Untitled Page"], kb=kb, input_text="new body\n")
    assert result.returncode == 0, result.stderr
    fields, body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fields["title"] == "Untitled Page"
    assert body == "new body\n"


# --- concurrency (findings 3, 4) ---------------------------------------


def test_concurrent_add_same_title_all_survive(kb: Path) -> None:
    n = 10

    def do_add(i: int) -> subprocess.CompletedProcess[str]:
        return run_cli(["add", "Same Title"], kb=kb, input_text=f"body {i}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(do_add, range(n)))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert len(list((kb / "sources").glob("*.md"))) == n


def test_concurrent_page_index_never_tears_and_repair_matches_disk(kb: Path) -> None:
    """No lock guards index regeneration (see docs/design/llm-wiki.md:
    a racing regenerate produces the same bytes and "the loser loses
    nothing"). So concurrent writers CAN leave `index.md` momentarily
    behind the page that just landed; what must never happen is a torn
    or malformed file, and `llm-wiki index` afterward must always land
    on exactly what's on disk."""
    n = 12

    def do_page(i: int) -> subprocess.CompletedProcess[str]:
        return run_cli(["page", f"Concurrent Page {i}"], kb=kb, input_text=f"body {i}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(do_page, range(n)))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert len(wiki_pages(kb)) == n

    text = (kb / "wiki" / "index.md").read_text(encoding="utf-8")
    assert text.startswith(llm_wiki.INDEX_BANNER)
    for row in index_data_rows(kb):
        assert row.count("|") == 6  # 5 columns: leading + 4 internal + trailing bar

    repair = run_cli(["index"], kb=kb)
    assert repair.returncode == 0
    assert len(index_data_rows(kb)) == n


# --- log.md failure after a successful write (finding F3) -------------
#
# `append_log_entry` used to exit 1 if log.md was missing, but it always
# ran after the file already landed on disk. A caller seeing a nonzero
# exit had every reason to retry, and a retry against `sources/`
# (immutable, never deleted) created a second file next to the first.
# The file on disk is truth; log.md is bookkeeping, so a failure here is
# a warning on stderr with exit 0, never a reason to retry.


def test_add_survives_missing_log_with_a_warning_not_a_failure(kb: Path) -> None:
    (kb / "log.md").unlink()
    result = run_cli(["add", "Some Source"], kb=kb, input_text="body\n")
    assert result.returncode == 0
    assert "warning" in result.stderr.lower()
    assert len(list((kb / "sources").glob("*.md"))) == 1


def test_page_survives_missing_log_with_a_warning_not_a_failure(kb: Path) -> None:
    (kb / "log.md").unlink()
    result = run_cli(["page", "Log-Free Page"], kb=kb, input_text="body\n")
    assert result.returncode == 0
    assert "warning" in result.stderr.lower()
    assert (kb / "wiki" / "log-free-page.md").exists()


def test_log_verb_itself_still_fails_loudly_when_log_md_missing(kb: Path) -> None:
    (kb / "log.md").unlink()
    result = run_cli(["log", "query", "test"], kb=kb)
    assert result.returncode != 0


# --- frontmatter round-trip (finding 10 plus general fidelity) --------


def test_flatten_strips_all_control_chars_not_just_newline() -> None:
    assert llm_wiki.flatten("alpha\rbeta") == "alpha beta"
    assert llm_wiki.flatten("a\nb\tc") == "a b c"
    assert "\r" not in llm_wiki.flatten("x\r\ny")


def test_render_parse_roundtrip_colon_pipe_cr_unicode_quote() -> None:
    fields = {
        "title": "ns:widget:v2",
        "summary": "a | b",
        "category": "line\rbreak",
        "note": 'He said "hi" to me',
    }
    rendered = llm_wiki.render_frontmatter(fields)
    parsed, body = llm_wiki.parse_frontmatter(rendered + "\nbody text: café-世界")

    assert parsed["title"] == "ns:widget:v2"
    assert parsed["summary"] == "a | b"
    assert parsed["category"] == "line break"
    assert "\r" not in parsed["category"]
    assert parsed["note"] == 'He said "hi" to me'
    assert body == "body text: café-世界"


# --- add --url safety (finding 6, gate 2 item C), stubbed, no network --
#
# `test_url_file_scheme_is_rejected` used to pass even with
# `_reject_unsafe_url` neutered to `return`, for two reasons: `add --url`
# imported lxml before the guard ever ran, so a missing lxml install
# failed the test for the wrong reason; and `/etc/hostname` has no
# extension, so `mimetypes.guess_type` returns `(None, None)` and the
# unrelated content-type check would have rejected it anyway. The fix
# (in `fetch_url_text`) moved the lxml import to after a successful
# fetch, and this test now targets a `.html`-suffixed `file://` path,
# which content-type sniffing would happily call `text/html` -- so this
# can only be rejected by the scheme guard, and asserts on that guard's
# own error text rather than just a nonzero exit code.


def test_url_file_scheme_is_rejected(kb: Path) -> None:
    result = run_cli(["add", "--url", "file:///nonexistent-x7f/article.html"], kb=kb)
    assert result.returncode != 0
    assert "only http and https are allowed" in result.stderr
    assert not list((kb / "sources").glob("*.md"))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/hostname",
        "ftp://example.com/file.txt",
        "data:text/html,<p>hi</p>",
    ],
)
def test_reject_unsafe_url_blocks_disallowed_schemes(url: str) -> None:
    with pytest.raises(SystemExit):
        llm_wiki._reject_unsafe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",  # loopback
        "http://169.254.169.254/latest/meta-data",  # link-local
        "http://10.0.0.1/",  # private
        "http://192.168.1.1/",  # private
        "http://172.16.0.1/",  # private
    ],
)
def test_reject_unsafe_url_blocks_non_global_addresses(url: str) -> None:
    with pytest.raises(SystemExit):
        llm_wiki._reject_unsafe_url(url)


def test_no_auto_redirect_never_follows() -> None:
    handler = llm_wiki._NoAutoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://x/") is None


class _FakeOpener:
    """Stands in for the opener `fetch_url_text` builds, so its redirect
    loop (each hop re-checked against `_reject_unsafe_url`, capped at
    `MAX_REDIRECTS`) can be exercised with zero network calls."""

    def __init__(self, responses: list[BaseException]) -> None:
        self._responses = list(responses)

    def open(self, request: object, timeout: float | None = None) -> object:
        raise self._responses.pop(0)


def _redirect_error(location: str) -> urllib.error.HTTPError:
    headers = email.message.Message()
    headers["Location"] = location
    return urllib.error.HTTPError("http://8.8.8.8/start", 302, "Found", headers, None)


def test_fetch_url_text_rechecks_each_redirect_hop_against_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_opener = _FakeOpener([_redirect_error("http://127.0.0.1/secret")])
    monkeypatch.setattr(
        llm_wiki.urllib.request, "build_opener", lambda *a, **k: fake_opener
    )
    with pytest.raises(SystemExit) as exc_info:
        llm_wiki.fetch_url_text("http://8.8.8.8/start")
    assert "private or local address" in str(exc_info.value.code)


def test_fetch_url_text_too_many_redirects_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        _redirect_error("http://8.8.8.8/next") for _ in range(llm_wiki.MAX_REDIRECTS + 1)
    ]
    fake_opener = _FakeOpener(responses)
    monkeypatch.setattr(
        llm_wiki.urllib.request, "build_opener", lambda *a, **k: fake_opener
    )
    with pytest.raises(SystemExit) as exc_info:
        llm_wiki.fetch_url_text("http://8.8.8.8/start")
    assert "too many redirects" in str(exc_info.value.code)


class _FakeResponse:
    """Stands in for an `http.client.HTTPResponse` far enough for
    `_read_html`, so its size/content-type checks can be tested without a
    network call."""

    def __init__(self, content_type: str, body: bytes) -> None:
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n and n > 0 else self._body


def _fake_response(content_type: str, body: bytes) -> _FakeResponse:
    return _FakeResponse(content_type, body)


def test_fetch_response_oversized_is_rejected() -> None:
    body = b"x" * (llm_wiki.MAX_FETCH_BYTES + 10)
    response = _fake_response("text/html; charset=utf-8", body)
    with pytest.raises(SystemExit):
        llm_wiki._read_html(response, "http://example.com")


def test_fetch_response_non_html_is_rejected() -> None:
    response = _fake_response("application/json", b'{"a": 1}')
    with pytest.raises(SystemExit):
        llm_wiki._read_html(response, "http://example.com")


class _FakeHtmlOpener:
    """Like `_FakeOpener`, but its single `.open()` call succeeds with a
    real HTML response, so `fetch_url_text` reaches the point where it
    imports lxml/readability."""

    def open(self, request: object, timeout: float | None = None) -> object:
        return contextlib.nullcontext(_fake_response("text/html", b"<p>hi</p>"))


def test_fetch_url_text_missing_lxml_is_a_loud_pip_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`add --url` used to raise a raw ImportError traceback when
    readability-lxml/lxml weren't installed; SKILL.md promises a loud
    error instead. `sys.modules["lxml.html"] = None` is the standard way
    to force `import lxml.html` to raise ImportError without actually
    uninstalling anything."""
    monkeypatch.setattr(
        llm_wiki.urllib.request, "build_opener", lambda *a, **k: _FakeHtmlOpener()
    )
    monkeypatch.setitem(sys.modules, "lxml.html", None)
    with pytest.raises(SystemExit) as exc_info:
        llm_wiki.fetch_url_text("http://8.8.8.8/start")
    assert "pip install" in str(exc_info.value.code)


# --- file permissions (gate 2, item E) ----------------------------------


def test_generated_files_are_world_readable_like_log_md(tmp_path: Path) -> None:
    """`atomic_write_text`/`write_source_file` build through `mkstemp`,
    which always makes its temp file 0600 regardless of umask. Without
    an explicit chmod that leaks into every file this tool writes, while
    `log.md` (written with a plain `open`) stays at the umask-derived
    default -- an inconsistency between files in the same kb.

    Finding F4: the explicit chmod used base 0o644, but a plain `open()`
    (what `log.md` goes through) is 0o666 base. Under umask 002 that put
    `log.md`/`SCHEMA.md` at 0664 and every mkstemp-built file at 0644 --
    still an inconsistency, just a quieter one. This test used to pass
    at 022 only by coincidence (0o644 == 0o666 & ~0o022); pinning the
    umask here makes that coincidence load-bearing instead of assumed."""
    old_umask = os.umask(0o022)
    try:
        kb_path = tmp_path / ".kb"
        result = run_cli(["init", str(kb_path)])
        assert result.returncode == 0, result.stderr
        index_mode = stat.S_IMODE((kb_path / "wiki" / "index.md").stat().st_mode)
        log_mode = stat.S_IMODE((kb_path / "log.md").stat().st_mode)
    finally:
        os.umask(old_umask)
    assert index_mode == log_mode == 0o644
