"""Bearer tokens for the service: the wire format, the keyed hash and
its pepper versions, and the sqlite store behind mint, list, revoke,
and verify.

The format is `llmwiki_<id>_<secret><crc>`. It is settled here because
changing it invalidates every token ever minted.

- `llmwiki_` makes a leaked token machine detectable, so secret
  scanning catches one pasted into a repository.
- `<id>` keeps verification to a single indexed lookup.
- `<crc>` is a checksum and never a signature. It proves a string is
  well formed, so a scanner can reject a candidate without calling the
  service and `verify` can reject one before any database work.

The store holds no plaintext token, so a stolen copy of the database
yields nothing usable and needs no encryption.
"""
from __future__ import annotations

import binascii
import contextlib
import hashlib
import re
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from llmwiki.core import flatten, utc_timestamp

PREFIX = "llmwiki"
ID_BYTES = 8  # 16 hex chars of row id, which is not a secret
SECRET_BYTES = 32  # 256 bits, the whole strength of a token
CHECKSUM_CHARS = 6  # a 32 bit CRC needs 6 base62 digits, zero padded
DIGEST_BYTES = 32  # blake2b output width
MAX_PEPPER_BYTES = 64  # blake2b's key limit
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ROLES = ("reader", "writer", "admin")

# The label is the friendly name a human types to revoke a token, so
# anything past a long hostname plus a person's name is a paste
# accident. Counted in characters, not bytes: the cap exists to keep a
# log line readable, and a log line is characters.
MAX_LABEL_CHARS = 128

PEPPER_ENV_VAR = "LLM_WIKI_PEPPER"

# Explicit, not sqlite3's implicit default: a concurrent writer gets
# this long to finish before sqlite raises "database is locked".
BUSY_TIMEOUT_MS = 5000

TABLE = (
    "CREATE TABLE IF NOT EXISTS tokens ("
    "id TEXT PRIMARY KEY, "
    "token_hash TEXT NOT NULL, "
    "pepper_version INTEGER NOT NULL, "
    "label TEXT NOT NULL UNIQUE, "
    "role TEXT NOT NULL, "
    "created_at TEXT NOT NULL, "
    "last_used_at TEXT, "
    "expires_at TEXT, "
    "revoked_at TEXT)"
)

_ID_PATTERN = re.compile(f"[0-9a-f]{{{ID_BYTES * 2}}}")


def _base62(value: int) -> str:
    digits = ""
    while value:
        value, index = divmod(value, 62)
        digits = BASE62[index] + digits
    return digits.rjust(CHECKSUM_CHARS, "0")


def checksum(body: str) -> str:
    """The base62 CRC32 of `body`, the token's last CHECKSUM_CHARS."""
    return _base62(binascii.crc32(body.encode()))


def new_token() -> tuple[str, str]:
    """A fresh `(id, token)` pair. The token is the only copy that will
    ever exist: `TokenStore.mint` returns it once and stores its hash."""
    row_id = secrets.token_hex(ID_BYTES)
    body = f"{PREFIX}_{row_id}_{secrets.token_urlsafe(SECRET_BYTES)}"
    return row_id, body + checksum(body)


def token_id(token: str) -> str | None:
    """The `<id>` segment of a well formed token, else `None`.

    Every rejection here happens before any database work, so malformed
    input never reaches the lookup path.

    The checksum compares with `==`, unlike the hash. It is public and
    carries no secret, and `secrets.compare_digest` raises `TypeError`
    on non-ASCII strings, which would turn a junk token into a crash
    instead of a refusal.
    """
    if len(token) <= CHECKSUM_CHARS:
        return None
    body, crc = token[:-CHECKSUM_CHARS], token[-CHECKSUM_CHARS:]
    if crc != checksum(body):
        return None
    parts = body.split("_", 2)
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    row_id, secret = parts[1], parts[2]
    if not _ID_PATTERN.fullmatch(row_id) or not secret:
        return None
    return row_id


def token_hash(token: str, pepper: bytes) -> str:
    """Keyed BLAKE2b of the whole token. Fast on purpose: verification
    runs on every request, so a memory hard hash would be both a
    latency tax and a way for an unauthenticated caller to exhaust the
    service. A 256 bit random token has no low entropy to protect."""
    return hashlib.blake2b(
        token.encode(), key=pepper, digest_size=DIGEST_BYTES
    ).hexdigest()


@dataclass(frozen=True, repr=False)
class Peppers:
    """The hash keys in force, by version. The highest version hashes
    every new token; the lower ones verify tokens minted before a
    rotation and can be dropped once every row has been re-hashed.

    Its repr names versions and never keys, because a repr reaches a
    log line or a traceback.
    """

    keys: Mapping[int, bytes]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("at least one pepper is required")
        if any(len(key) > MAX_PEPPER_BYTES for key in self.keys.values()):
            raise ValueError(f"a pepper is over {MAX_PEPPER_BYTES} bytes")

    @property
    def current(self) -> int:
        return max(self.keys)

    def __repr__(self) -> str:
        return f"Peppers(versions={sorted(self.keys)})"


def peppers_from_env(environ: Mapping[str, str]) -> Peppers:
    """Read `LLM_WIKI_PEPPER`, whitespace separated `<version>:<secret>`
    entries with the highest version current. A rotation window is
    `LLM_WIKI_PEPPER="2:<new> 1:<old>"`.

    Raises `ValueError` when the variable is unset, empty, or malformed.
    No message quotes any part of the value, because the value is a
    credential.
    """
    keys: dict[int, bytes] = {}
    for entry in environ.get(PEPPER_ENV_VAR, "").split():
        version, separator, secret = entry.partition(":")
        if not separator or not version.isdecimal() or not secret:
            raise ValueError(
                f"{PEPPER_ENV_VAR}: each entry must be <version>:<secret>"
            )
        if int(version) in keys:
            raise ValueError(f"{PEPPER_ENV_VAR}: duplicate pepper version")
        keys[int(version)] = secret.encode()
    if not keys:
        raise ValueError(f"{PEPPER_ENV_VAR} is unset or empty")
    return Peppers(keys)


@dataclass(frozen=True)
class TokenRow:
    """What the store may disclose about a token: never its hash, and
    never its plaintext."""

    id: str
    label: str
    role: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class TokenStore:
    """The token table at `path`, verified against `peppers`."""

    def __init__(self, path: Path, peppers: Peppers) -> None:
        self.path = path
        self.peppers = peppers
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(TABLE)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def mint(self, label: str, role: str) -> str:
        """Create a token for `label` and return its plaintext.

        This return value is the only copy of the plaintext that ever
        exists. No other method returns it, `expires_at` is left null
        because expiry is not a policy yet, and a duplicate label
        raises `sqlite3.IntegrityError` from the unique constraint.

        A label holding a control character is refused, because the
        label is what a log line names in place of the token, and a
        label carrying a newline can forge a second line.

        The whole label contract lives here rather than in a caller:
        every caller of `mint` gets it, including a route, a test that
        mints a fixture, and any later local command. A label is at
        most `MAX_LABEL_CHARS` characters, is not empty or whitespace
        only, has no leading or trailing whitespace, and holds no
        character `flatten` would collapse. The whitespace rule is not
        cosmetic: "ops" and "ops " are otherwise two rows that read
        identically in a listing and revoke separately.
        """
        if role not in ROLES:
            raise ValueError(f"unknown role: {role}")
        if not label.strip():
            raise ValueError("a token needs a label to be revoked by")
        if len(label) > MAX_LABEL_CHARS:
            raise ValueError(f"a label is at most {MAX_LABEL_CHARS} characters")
        if label.strip() != label:
            raise ValueError("a label must not begin or end with whitespace")
        if flatten(label) != label:
            raise ValueError("a label must hold no control characters")
        row_id, token = new_token()
        version = self.peppers.current
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO tokens(id, token_hash, pepper_version, label, "
                "role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    token_hash(token, self.peppers.keys[version]),
                    version,
                    label,
                    role,
                    utc_timestamp(),
                ),
            )
        return token

    def list_tokens(self) -> list[TokenRow]:
        """Every token, revoked ones included, oldest first."""
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, label, role, created_at, last_used_at, revoked_at "
                "FROM tokens ORDER BY created_at, label"
            ).fetchall()
        return [TokenRow(*row) for row in rows]

    def revoke(self, label: str) -> bool:
        """Revoke by friendly name. `True` when a live row was revoked,
        `False` when the label is unknown or already revoked."""
        with contextlib.closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE tokens SET revoked_at = ? "
                "WHERE label = ? AND revoked_at IS NULL",
                (utc_timestamp(), label),
            )
        return cursor.rowcount == 1

    def verify(self, token: str, now: str | None = None) -> TokenRow | None:
        """The row `token` belongs to, or `None`.

        The `None` is the same whether the token failed its checksum,
        the id was unknown, the secret was wrong, the row was revoked,
        or the row had expired. A caller cannot tell those apart, so
        neither can a caller of the caller.

        A successful verification stamps `last_used_at` and, when the
        row was hashed under an older pepper, re-hashes it under the
        current one.
        """
        row_id = token_id(token)
        if row_id is None:
            return None
        stamp = now or utc_timestamp()
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT token_hash, pepper_version, label, role, created_at "
                "FROM tokens WHERE id = ? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (row_id, stamp),
            ).fetchone()
            if row is None:
                return None
            stored_hash, version, label, role, created_at = row
            pepper = self.peppers.keys.get(version)
            if pepper is None:
                return None
            if not secrets.compare_digest(stored_hash, token_hash(token, pepper)):
                return None
            self._stamp_use(conn, row_id, token, version, stamp)
        return TokenRow(row_id, label, role, created_at, stamp, None)

    def _stamp_use(
        self,
        conn: sqlite3.Connection,
        row_id: str,
        token: str,
        version: int,
        stamp: str,
    ) -> None:
        """Record the use, and finish a pepper rotation for this row
        while it is in hand. Re-hashing on use is what lets an operator
        retire the old pepper without invalidating every token."""
        current = self.peppers.current
        with conn:
            if version == current:
                conn.execute(
                    "UPDATE tokens SET last_used_at = ? WHERE id = ?",
                    (stamp, row_id),
                )
                return
            conn.execute(
                "UPDATE tokens SET last_used_at = ?, token_hash = ?, "
                "pepper_version = ? WHERE id = ?",
                (
                    stamp,
                    token_hash(token, self.peppers.keys[current]),
                    current,
                    row_id,
                ),
            )
