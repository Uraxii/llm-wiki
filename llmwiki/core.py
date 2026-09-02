"""Shared primitives every llm-wiki module imports: kb paths, config
loading, frontmatter parse/render, slugify, atomic write, log entries.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import re
import tempfile
import time
import tomllib
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from os import (
    O_CREAT,
    O_RDWR,
    close as os_close,
    fchmod,
    fdopen,
    open as os_open,
    replace as os_replace,
    umask,
)
from pathlib import Path

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_SLUG_LEN = 120
SLUG_HASH_LEN = 12  # hex chars of fallback hash when a title strips to nothing

LOCK_FILE_NAME = ".lock"
LOCK_WAIT_TIMEOUT_SEC = 30  # bounded wait for the kb lock, see kb_lock()
LOCK_POLL_INTERVAL_SEC = 0.05  # flock has no native timeout; poll for it

FrontmatterValue = str | list[str]


@dataclass
class Page:
    path: Path
    fields: dict[str, FrontmatterValue]
    body: str


class Kb:
    """A knowledge base rooted at `root` (a `.kb` directory)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.wiki = root / "wiki"
        self.sources = root / "sources"
        self.vectors = root / "vectors"
        self.log = root / "log.md"
        self.config = load_config(root)


class KbBusy(Exception):
    """Raised when the kb lock is not free within LOCK_WAIT_TIMEOUT_SEC.
    Never carries retry or recovery logic: the caller's process exits
    nonzero and whatever retried it decides what to do next."""


@contextlib.contextmanager
def kb_lock(root: Path) -> Iterator[None]:
    """Exclusive lock over the kb at `root`, held by the caller for the
    span it names. `fcntl.flock` on one file in the kb root, standard
    library only; the kernel releases it automatically if this process
    dies, so no stale-lock reaping is needed. The wait is bounded: past
    LOCK_WAIT_TIMEOUT_SEC this raises KbBusy rather than blocking
    forever, since an unbounded wait would outlive an agent's tool
    timeout and the resulting retry would only pile onto the same
    contention.
    """
    fd = os_open(str(root / LOCK_FILE_NAME), O_CREAT | O_RDWR, 0o666)
    try:
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SEC
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise KbBusy(
                        f"{root}: kb busy, timed out after "
                        f"{LOCK_WAIT_TIMEOUT_SEC}s waiting for the lock"
                    ) from None
                time.sleep(LOCK_POLL_INTERVAL_SEC)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os_close(fd)


def load_config(root: Path) -> dict:
    """Load `root/config.toml` as a dict, unmodified. `{}` if the file is
    absent; a malformed file raises with its path named in the message."""
    config_path = root / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"malformed config: {config_path}: {exc}") from exc


def flatten(text: str) -> str:
    """Collapse every character `str.splitlines()` treats as a line
    break, plus DEL, to a space, so a value never breaks a one-line
    record: C0 controls, `\\x85` NEL, U+2028, and U+2029."""
    return re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]", " ", text)


def parse_frontmatter(
    text: str,
) -> tuple[dict[str, FrontmatterValue], str] | None:
    """Split a `---` frontmatter block off `text`.

    A scalar line is `key: value`. A list is either inline (`key: [a,
    b]`) or block (`key:` followed by `  - item` lines). Returns `None`
    on any malformed block: no closing `---`, a line that is neither
    `key: value` nor a list-item continuation, or a list-item line with
    no key owning it.
    """
    if not text.startswith("---\n"):
        return None
    lines = text.split("\n")
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            end = index
            break
    if end is None:
        return None

    fields: dict[str, FrontmatterValue] = {}
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(" ") or line.startswith("\t"):
            return None  # list item with no preceding key
        if ":" not in line:
            return None
        key, _, rest = line.partition(":")
        key = key.strip()
        if not key:
            return None
        rest = rest.strip()
        index += 1
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            fields[key] = [item.strip() for item in inner.split(",")] if inner else []
        elif rest == "":
            items = []
            while index < end and lines[index].startswith("  - "):
                items.append(lines[index][4:])
                index += 1
            fields[key] = items if items else ""
        else:
            fields[key] = rest

    body_lines = lines[end + 1 :]
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)
    return fields, body


def render_frontmatter(fields: dict[str, FrontmatterValue], body: str) -> str:
    """Render `fields` plus `body` as a full page: a `---` block (a
    non-empty list always in block form, even if parsed from inline;
    an empty list as inline `key: []`, since a bare `key:` line reads
    back from `parse_frontmatter` as the empty string, not a list) then
    a blank line then `body`."""
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def as_list(value: FrontmatterValue | None) -> list[str]:
    """Read a frontmatter list field defensively: `value` itself if it
    is already a list, `[value]` if it is a truthy scalar (a legacy
    page written before `render_frontmatter` learned to emit `key: []`,
    whose empty list round-tripped to a bare string), or `[]` for
    anything else (missing key, empty string)."""
    if isinstance(value, list):
        return value
    return [value] if value else []


def slugify(title: str) -> str:
    """Kebab-case a title for use as a filename, ASCII-normalized and
    length-capped so a long or accented title stays a valid path. NFKD
    decomposition means an NFC and an NFD spelling of the same accented
    title fold to the same slug. A title that normalizes to nothing
    ASCII (most non-Latin scripts) falls back to a hash of the original
    title, so distinct titles never collapse onto the same slug."""
    normalized = unicodedata.normalize("NFKD", title)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    slug = slug[:MAX_SLUG_LEN].strip("-")
    if slug:
        return slug
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:SLUG_HASH_LEN]
    return f"untitled-{digest}"


def read_page_text(path: Path) -> str:
    """Read a wiki page as text, replacing any byte that is not valid
    UTF-8. A page the CLI did not write can hold anything, and one bad
    byte must not take down a whole run."""
    return path.read_bytes().decode("utf-8", errors="replace")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` without ever exposing a partially written
    file: build it in a same-directory temp file, then atomically
    replace. A crash mid-write leaves only the temp file behind, never a
    half-written `path`. Raises `OSError` on failure (caller's problem)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        current_umask = umask(0)
        umask(current_umask)
        fchmod(fd, 0o666 & ~current_umask)
        with fdopen(fd, "wb") as handle:
            handle.write(data)
        os_replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` without ever exposing a partially
    written file. Encodes as UTF-8 and delegates to
    `atomic_write_bytes`; see that docstring for the write mechanics."""
    atomic_write_bytes(path, content.encode("utf-8"))


def utc_timestamp() -> str:
    """ISO 8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def append_log_entry(log_path: Path, kind: str, title: str) -> None:
    """Append one `## [kind] title - <utc timestamp>` line to `log_path`.
    Raises `FileNotFoundError` if the log file is missing; the caller
    decides whether that is fatal."""
    if not log_path.is_file():
        raise FileNotFoundError(log_path)
    entry = f"## [{flatten(kind)}] {flatten(title)} - {utc_timestamp()}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
