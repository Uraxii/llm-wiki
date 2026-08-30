"""Store raw source bytes by content hash, with a TOML sidecar recording
where they came from. Content-addressing means two fetches of the same
bytes collapse to one file instead of piling up duplicates.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Literal

from llmwiki.core import Kb, utc_timestamp

EXTENSIONS = {"text/markdown": ".md", "application/pdf": ".pdf"}


def _link_exclusive(tmp_path: Path, dest: Path) -> bool:
    """Publish `tmp_path` at `dest` atomically. False means `dest` was
    already there, so this caller lost the race or the content is
    already stored."""
    try:
        os.link(tmp_path, dest)
        return True
    except FileExistsError:
        return False


def store(
    kb: Kb, data: bytes, url: str, content_type: str, job: str
) -> tuple[str, Literal["new", "exists"]]:
    """Write `data` under its sha256 digest and record where it came
    from. Bytes are linked into place before the sidecar is written, so
    a sidecar on disk always implies its bytes are on disk too, even
    across a crash. The sidecar path does not depend on content type,
    so claiming it is what decides `new` versus `exists`: storing the
    same bytes twice, even under a different content type, is a no-op
    the second time, and the existing provenance is left untouched.
    """
    digest = hashlib.sha256(data).hexdigest()
    normalized = content_type.split(";")[0].strip().lower()
    ext = EXTENSIONS.get(normalized, ".txt")
    dest = kb.sources / f"{digest}{ext}"
    sidecar = kb.sources / f"{digest}.toml"

    fd, tmp_name = tempfile.mkstemp(dir=kb.sources, prefix=f".{digest}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        claimed_bytes = _link_exclusive(tmp_path, dest)
    finally:
        tmp_path.unlink(missing_ok=True)

    lines = [
        f"url = {json.dumps(url, ensure_ascii=False)}\n",
        f"fetched = {json.dumps(utc_timestamp(), ensure_ascii=False)}\n",
        f"content_type = {json.dumps(content_type, ensure_ascii=False)}\n",
        f"job = {json.dumps(job, ensure_ascii=False)}\n",
    ]
    fd2, tmp_name2 = tempfile.mkstemp(dir=kb.sources, prefix=f".{digest}.")
    tmp_path2 = Path(tmp_name2)
    try:
        with os.fdopen(fd2, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
        claimed = _link_exclusive(tmp_path2, sidecar)
    finally:
        tmp_path2.unlink(missing_ok=True)

    if not claimed:
        if claimed_bytes:
            # `dest` is keyed on digest+ext, so it is shared by every
            # caller using this same extension: winning its link is
            # permanent, whichever caller's sidecar wins next. Only a
            # caller storing under a *different* extension can have
            # added a genuine duplicate here; unlink just that case.
            other_ext = any(
                p.suffix not in (".toml", ext)
                for p in kb.sources.glob(f"{digest}.*")
            )
            if other_ext:
                dest.unlink()
        return digest, "exists"
    return digest, "new"


def read_provenance(kb: Kb, digest: str) -> dict:
    """Read the sidecar for `digest` back as a dict, unmodified."""
    sidecar = kb.sources / f"{digest}.toml"
    with sidecar.open("rb") as handle:
        return tomllib.load(handle)


def stored_urls(kb: Kb) -> set[str]:
    """Every url with an existing provenance sidecar, read fresh from
    `sources/*.toml` each call: the store is keyed by content hash, not
    url, so there is no url index to consult instead. A sidecar that
    fails to parse is skipped rather than crashing the caller's job."""
    urls: set[str] = set()
    for sidecar in kb.sources.glob("*.toml"):
        try:
            with sidecar.open("rb") as handle:
                provenance = tomllib.load(handle)
        except (tomllib.TOMLDecodeError, OSError):
            continue
        url = provenance.get("url")
        if isinstance(url, str):
            urls.add(url)
    return urls
