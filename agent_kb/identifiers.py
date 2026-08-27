"""Identifier vocabulary and write-path normalisation."""
from __future__ import annotations

import sqlite3

__all__ = [
    "CANONICAL_ID_TYPES",
    "normalise_identifier",
    "singleton_identifier_types",
    "unknown_identifier_types",
]

CANONICAL_ID_TYPES = (
    "digest",
    "repo",
    "namespace",
    "service",
    "hostname",
    "image",
    "zone_id",
    "rule_id",
    "finding_id",
)


def normalise_identifier(id_type: str, value: str) -> str:
    """Return the join-table value for one verbatim identifier."""
    if not isinstance(value, str):
        raise TypeError("identifier values must be str")
    normalisers = {
        "hostname": _normalise_hostname,
        "digest": _normalise_digest,
        "image": _normalise_image,
        "repo": _normalise_repo,
    }
    return normalisers.get(id_type, _identity)(value)


def unknown_identifier_types(connection: sqlite3.Connection) -> list[str]:
    """Return stored id types outside the declared vocabulary."""
    rows = connection.execute(
        "SELECT DISTINCT id_type FROM identifiers ORDER BY id_type"
    )
    return [
        row["id_type"]
        for row in rows
        if row["id_type"] not in CANONICAL_ID_TYPES
    ]


def singleton_identifier_types(
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Return id types emitted by only one source as ``(id_type, source)``."""
    rows = connection.execute(
        """
        SELECT i.id_type, MIN(w.source) AS source
        FROM identifiers AS i
        JOIN wiki_row AS w ON w.id = i.row_id
        GROUP BY i.id_type
        HAVING COUNT(DISTINCT w.source) = 1
        ORDER BY i.id_type
        """
    )
    return [(row["id_type"], row["source"]) for row in rows]


def _identity(value: str) -> str:
    return value


def _normalise_hostname(value: str) -> str:
    return value.rstrip(".").lower()


def _normalise_digest(value: str) -> str:
    # Canonical digest is bare lowercase hex. The algorithm is carried by
    # id_type, so prefixed and unprefixed values meet on the same join key.
    return value.rsplit(":", 1)[-1].lower()


def _normalise_image(value: str) -> str:
    # Canonical image omits the registry host. Connectors disagree on the
    # default registry, while repo, name, tag and digest are the join key.
    if "/" not in value:
        return value.lower()
    first, rest = value.split("/", 1)
    if "." in first or ":" in first or first == "localhost":
        return rest.lower()
    return value.lower()


def _normalise_repo(value: str) -> str:
    return value.lower()
