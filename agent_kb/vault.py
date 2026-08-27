"""Where the bytes live. Four methods, one local implementation.

The store is pluggable by design: a local directory today, an object
store or a shared drive later. The abstraction is deliberately four
methods wide, because a wider one would encode a local filesystem's
semantics (rename, append, lock, mtime) that no remote backend gives for
free.

`LocalVault` is the trust boundary for keys. A key becomes a path, so it
is validated by resolution, not by pattern matching: whatever a key looks
like, the resolved result must stay under the vault root.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile

__all__ = ["LocalVault", "Vault"]


class Vault(ABC):
    """Byte store addressed by vault-relative POSIX keys.

    Keys look like `entities/service/api-gateway.md`: forward slashes,
    no leading slash, no `.` or `..` component. Values are `bytes`; text
    callers encode UTF-8 themselves so no backend has to guess.

    Writes are additive and single-writer by construction (one `collect`
    process at a time), so no method here takes a lock or promises
    atomicity across keys.
    """

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Write `data` at `key`, replacing any existing value.

        Postcondition: `exists(key)` is true and `get(key) == data`.
        Creates any intermediate structure the backend needs.

        Raises:
            ValueError: the key escapes the vault or is malformed.
        """

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Return the value at `key`.

        Raises:
            KeyError: no value at `key`.
            ValueError: the key escapes the vault or is malformed.
        """

    @abstractmethod
    def list(self, prefix: str = "") -> Iterator[str]:
        """Yield every key under `prefix`, in sorted order.

        Sorted so a rebuild is deterministic. Yields keys, not paths, so
        the result is portable across backends.
        """

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether a value is stored at `key`. Never raises on absence."""


class LocalVault(Vault):
    """A `Vault` backed by one directory on this machine."""

    def __init__(self, root: Path) -> None:
        """Bind the vault to `root`, creating it if missing.

        Postcondition: `self.root` is the RESOLVED root, so every later
        containment check compares resolved paths and a symlinked
        subdirectory cannot smuggle writes outside.
        """
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        """Resolve `key` to a path inside the vault, or raise.

        This is the single validation point for every method below: an
        absolute key, a `..` component, and a symlink pointing out of the
        vault are all rejected by the same resolved-containment check
        rather than by three different patterns.

        Raises:
            ValueError: empty key, or a key resolving outside the root.
        """
        key_path = Path(key)
        if not key or key_path.is_absolute():
            raise ValueError(f"invalid vault key: {key!r}")
        path = (self.root / key_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"vault key escapes root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        """See `Vault.put`. Writes via a temp file in the same directory
        then `os.replace`, so a reader never sees a half-written page."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as temp_file:
            temp_file.write(data)
            temp_path = Path(temp_file.name)
        temp_path.replace(path)

    def get(self, key: str) -> bytes:
        """See `Vault.get`."""
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as error:
            raise KeyError(key) from error

    def list(self, prefix: str = "") -> Iterator[str]:
        """See `Vault.list`."""
        start = self.root if prefix == "" else self._path(prefix)
        if not start.exists():
            return iter(())
        paths = sorted(path for path in start.rglob("*") if path.is_file())
        return (path.relative_to(self.root).as_posix() for path in paths)

    def exists(self, key: str) -> bool:
        """See `Vault.exists`."""
        return self._path(key).is_file()
