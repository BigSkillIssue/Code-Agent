"""The read ledger: which files the model has seen, and in which version."""

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LedgerEntry:
    """The version of a file the model last saw."""

    sha256: str
    mtime_ns: int
    full: bool  # True when every line was returned to the model


def sha256_of(data: bytes) -> str:
    """Hex SHA-256 of some bytes."""
    return hashlib.sha256(data).hexdigest()


class ReadLedger:
    """Maps resolved paths to the content hash the model last read or wrote."""

    def __init__(self) -> None:
        self._entries: dict[Path, LedgerEntry] = {}

    def record(self, path: Path, data: bytes, mtime_ns: int, full: bool) -> None:
        """Remember that the model saw `data` for `path` (a full read is not downgraded)."""
        digest = sha256_of(data)
        old = self._entries.get(path)
        if old is not None and old.sha256 == digest and old.full:
            full = True
        self._entries[path] = LedgerEntry(digest, mtime_ns, full)

    def get(self, path: Path) -> LedgerEntry | None:
        """The entry for `path`, if the model has seen it."""
        return self._entries.get(path)

    def forget(self, path: Path) -> None:
        """Drop `path`, e.g. after it was deleted."""
        self._entries.pop(path, None)
