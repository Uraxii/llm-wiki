"""Tests for the skeptic-gate findings on llm_wiki.py: slug collisions
(1), whitespace-only stdin wiping a body (2), the sources/ add/add TOCTOU
(3), index drift under concurrency (4), closed/silent stdin (7, 8),
frontmatter field loss on re-stamp (9), and control characters leaking
into a flattened field (10). Body-preservation coverage doubles as the
regression test for finding 5 (no path.write_text truncation on crash)
and finding 15 (leading blank lines in a body).

Anything that touches stdin or real concurrency runs the CLI as a real
subprocess: select() needs an actual file descriptor, which an
io.StringIO stand-in for sys.stdin cannot provide. Pure functions are
exercised directly. Every kb lives under tmp_path; nothing here ever
touches a real store.
"""
from __future__ import annotations

import concurrent.futures
import email.message
import os
import subprocess
import sys
import time
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


def test_stdin_whitespace_only_does_not_wipe_existing_body(kb: Path) -> None:
    run_cli(["page", "Ws Page"], kb=kb, input_text="original body\n")
    path = kb / "wiki" / "ws-page.md"
    before_body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1]

    result = run_cli(["page", "Ws Page"], kb=kb, input_text="   \n\t  \n")

    assert result.returncode == 0
    after_body = llm_wiki.parse_frontmatter(path.read_text(encoding="utf-8"))[1]
    assert before_body == after_body == "original body\n"


def test_stdin_open_silent_pipe_does_not_hang(kb: Path) -> None:
    read_fd, write_fd = os.pipe()
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "page", "Silent Pipe Page", "--kb", str(kb)],
            stdin=read_fd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    elapsed = time.monotonic() - start
    assert elapsed < 4, "read_stdin_body blocked on a pipe that never produced data"
    assert result.returncode != 0  # new page, no body ever arrived


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

    # Re-stamp with empty stdin must not touch the body at all.
    result2 = run_cli(["page", "Tricky Body Page"], kb=kb, input_text="")
    assert result2.returncode == 0
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

    result = run_cli(["page", "Tagged Page", "--summary", "updated summary"], kb=kb)
    assert result.returncode == 0

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


def test_slugify_same_title_is_idempotent() -> None:
    title = "日本語ソース"
    assert llm_wiki.slugify(title) == llm_wiki.slugify(title)


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


# --- concurrency (findings 3, 4) ---------------------------------------


def test_concurrent_add_same_title_all_survive(kb: Path) -> None:
    n = 10

    def do_add(i: int) -> subprocess.CompletedProcess[str]:
        return run_cli(["add", "Same Title"], kb=kb, input_text=f"body {i}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(do_add, range(n)))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert len(list((kb / "sources").glob("*.md"))) == n


def test_concurrent_page_index_matches_files_on_disk(kb: Path) -> None:
    n = 12

    def do_page(i: int) -> subprocess.CompletedProcess[str]:
        return run_cli(["page", f"Concurrent Page {i}"], kb=kb, input_text=f"body {i}\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(do_page, range(n)))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert len(wiki_pages(kb)) == n
    assert len(index_data_rows(kb)) == n


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


# --- add --url safety (finding 6), stubbed, no network -----------------


def test_url_file_scheme_is_rejected(kb: Path) -> None:
    result = run_cli(["add", "--url", "file:///etc/hostname"], kb=kb)
    assert result.returncode != 0
    assert not list((kb / "sources").glob("*.md"))


def test_reject_unsafe_url_blocks_loopback() -> None:
    with pytest.raises(SystemExit):
        llm_wiki._reject_unsafe_url("http://127.0.0.1/secret")


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
