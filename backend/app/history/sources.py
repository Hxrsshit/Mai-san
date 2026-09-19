"""Locating export files inside the configured import directory.

The caller names a file; the directory is server configuration. That split is
the whole security model of this module, and it has one failure mode worth
naming: if a caller-supplied name can escape the directory, the import feature
becomes an arbitrary-file-read primitive that reports its findings through the
archive tables.

So resolution is by *identity*, not by string inspection. The candidate is
resolved to a real path and its parent is compared to the resolved import
directory. Blocklisting `..` would be the obvious approach and is weaker:
it misses symlinks, which resolve to somewhere else entirely while containing
no suspicious characters at all.
"""

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.core.config import Settings

#: Extensions an export can plausibly have. Detection still inspects the file;
#: this only keeps the listing free of unrelated clutter.
LISTED_SUFFIXES = frozenset({".zip", ".json"})

#: Bytes per read when hashing. Exports are large; hashing must not load one
#: into memory to do it.
_HASH_CHUNK = 1024 * 1024


class ImportSourceError(Exception):
    """The named source cannot be used. Carries a reason code, not a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImportSource:
    """One candidate file. Metadata only -- never content."""

    filename: str
    size_bytes: int
    modified_at: datetime


def import_directory(settings: Settings) -> Path:
    """The configured directory, expanded and resolved."""
    return Path(os.path.expanduser(settings.MAI_IMPORT_DIR)).resolve()


def list_sources(settings: Settings) -> List[ImportSource]:
    """Candidate export files, newest first.

    Returns an empty list when the directory does not exist. A missing import
    directory is an unconfigured deployment, not an error worth raising at a
    user who just opened a panel.
    """
    directory = import_directory(settings)
    if not directory.is_dir():
        return []

    sources: List[ImportSource] = []
    for entry in directory.iterdir():
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in LISTED_SUFFIXES:
                continue
            # A symlink inside the directory that points outside it is not a
            # source. Listing it would advertise a file this module would then
            # refuse to open, which is a confusing way to be safe.
            if entry.resolve().parent != directory:
                continue
            stat = entry.stat()
        except OSError:
            continue
        sources.append(
            ImportSource(
                filename=entry.name,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )

    sources.sort(key=lambda source: source.modified_at, reverse=True)
    return sources


def resolve(filename: str, settings: Settings) -> Path:
    """Resolve a caller-supplied filename inside the import directory.

    Raises rather than returning None: every failure here is a refusal the
    caller must surface, and an `Optional` return invites a caller that
    forgets to check.
    """
    if not filename or len(filename) > 255:
        raise ImportSourceError("invalid_filename")

    # A name, not a path. `Path(filename).name` would silently *rewrite*
    # "../../etc/passwd" into "passwd" and import a different file than the
    # caller asked for; refusing is honest, and it keeps the audit trail
    # matching what was requested.
    if filename != Path(filename).name:
        raise ImportSourceError("invalid_filename")
    if filename in {".", ".."} or filename.startswith("."):
        raise ImportSourceError("invalid_filename")

    directory = import_directory(settings)
    candidate = (directory / filename).resolve()

    # Identity, not prefix matching: `str.startswith` on paths treats
    # `/imports-evil` as inside `/imports`.
    if candidate.parent != directory:
        raise ImportSourceError("outside_import_directory")
    if not candidate.is_file():
        raise ImportSourceError("source_not_found")

    return candidate


def sha256_of(path: Path, settings: Settings) -> str:
    """Content hash, streamed. The idempotency key for an import.

    Content-addressed rather than name-addressed, so a renamed copy of an
    export is recognised as the same export.
    """
    digest = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                if read > settings.IMPORT_MAX_FILE_BYTES:
                    raise ImportSourceError("source_too_large")
                digest.update(chunk)
    except OSError:
        raise ImportSourceError("source_unreadable")
    return digest.hexdigest()


__all__ = [
    "LISTED_SUFFIXES",
    "ImportSource",
    "ImportSourceError",
    "import_directory",
    "list_sources",
    "resolve",
    "sha256_of",
]
