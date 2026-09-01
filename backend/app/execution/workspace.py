"""The workspace sandbox.

Every filesystem path an executable tool touches passes through `resolve_in`.
It is the only place a user-supplied string becomes a path, and it fails
closed: anything it cannot prove is inside the workspace is refused.

The check is done on the **resolved** path, not the written one. Resolution
follows symlinks and normalises `..`, so a link inside the workspace pointing
at `/etc` resolves to `/etc/...` and fails containment -- which a purely
textual check on the input would have missed entirely.

What this does not defend against
---------------------------------

There is a window between resolving a path and writing to it in which the
filesystem could change beneath us. For a single-user local workspace that is
not a realistic threat, and closing it properly needs `openat2`-style
primitives that are not portable. Creation additionally uses `O_EXCL`, so the
most consequential operation cannot silently follow a link planted in that
window. The residual risk is documented rather than hidden.
"""

import os
import unicodedata
from pathlib import Path, PurePosixPath
from typing import List, Optional

from app.core.logging import get_logger
from app.execution.errors import WorkspaceViolation

logger = get_logger(__name__)

#: Path components that can never appear in a workspace-relative path.
#:
#: `..` is the obvious one. The empty string and `.` are rejected too, because
#: a path that normalises to nothing is not a file the user meant.
_FORBIDDEN_COMPONENTS = frozenset({"..", "", "."})

#: Characters that must not appear in any component. A null byte truncates a
#: path in the C layer beneath Python; the separators would smuggle structure
#: through a single component.
_FORBIDDEN_CHARACTERS = ("\x00", "\n", "\r")

#: The longest relative path accepted, and the deepest.
MAX_RELATIVE_PATH_CHARS = 400
MAX_PATH_DEPTH = 12


def workspace_root(configured: str) -> Path:
    """The absolute, resolved workspace root.

    Resolved once so every containment check compares against a real path
    rather than one that might itself traverse a symlink.
    """
    return Path(configured).expanduser().resolve()


def ensure_workspace(root: Path) -> Path:
    """Create the workspace if it does not exist. Never creates anything else."""
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_in(root: Path, relative: str) -> Path:
    """Resolve a user-supplied relative path inside `root`, or refuse.

    Raises `WorkspaceViolation` for anything that is not provably contained.
    The checks run before resolution where they can, so an obviously hostile
    input never reaches the filesystem at all.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise WorkspaceViolation(detail="empty path")

    # Normalise unicode first. Without this, a decomposed form could carry a
    # component past a textual comparison and recompose on the filesystem.
    candidate = unicodedata.normalize("NFC", relative).strip()

    if len(candidate) > MAX_RELATIVE_PATH_CHARS:
        raise WorkspaceViolation(detail="path too long")

    for character in _FORBIDDEN_CHARACTERS:
        if character in candidate:
            raise WorkspaceViolation(detail="illegal character in path")

    # Backslashes are a separator on some platforms and a literal on others.
    # Rather than guess, refuse: every allowed path uses forward slashes.
    if "\\" in candidate:
        raise WorkspaceViolation(detail="backslash in path")

    if candidate.startswith("/") or candidate.startswith("~"):
        raise WorkspaceViolation(detail="absolute path")

    # A drive letter or UNC prefix is absolute on Windows even without a
    # leading slash.
    if len(candidate) >= 2 and candidate[1] == ":":
        raise WorkspaceViolation(detail="drive-qualified path")

    parts = PurePosixPath(candidate).parts
    if not parts:
        raise WorkspaceViolation(detail="empty path")
    if len(parts) > MAX_PATH_DEPTH:
        raise WorkspaceViolation(detail="path too deep")

    # `..` is the member that fires here. `PurePosixPath` already drops `.`
    # and empty segments while parsing, so those two are belt and braces
    # against a future change of parser rather than live checks -- a test pins
    # that, so the set cannot quietly start doing more than this says.
    #
    # `..` is refused even where it stays inside the root. The containment
    # check below would allow `nested/../inside.txt`, but the approved string
    # and the file touched would then differ, and many strings would map to
    # one file -- which would make a fingerprint a poor identity for an action
    # and leave the journal naming a path that was never written.
    for part in parts:
        if part in _FORBIDDEN_COMPONENTS:
            raise WorkspaceViolation(detail="traversal component")

    resolved = (root / PurePosixPath(candidate)).resolve()

    # The containment check. Done after resolution, so symlinks and any
    # normalisation the OS applied are already accounted for.
    if not _is_inside(resolved, root):
        logger.warning(
            "Refused a path that resolved outside the workspace",
            # Never the path itself: it is user input and may be anything.
            extra={"depth": len(parts)},
        )
        raise WorkspaceViolation(detail="resolved outside workspace")

    return resolved


def _is_inside(candidate: Path, root: Path) -> bool:
    """True when `candidate` is `root` or below it.

    `Path.is_relative_to` is a pure lexical comparison on already-resolved
    paths, which is what is wanted here: both sides have been through
    `resolve()`, so no symlink remains to be followed.
    """
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (ValueError, OSError):  # pragma: no cover - defensive
        return False


def relative_to_root(path: Path, root: Path) -> str:
    """A workspace-relative display string. Never leaks the absolute root."""
    try:
        return str(path.relative_to(root))
    except ValueError:  # pragma: no cover - only reachable if containment broke
        return "<outside workspace>"


def list_files(
    root: Path,
    max_results: int,
    max_depth: int,
    subdirectory: Optional[str] = None,
) -> List[str]:
    """Workspace-relative file paths, bounded in count and depth.

    Symlinked directories are not followed. A link pointing outside would
    otherwise let a listing walk the whole filesystem, and one pointing inside
    would produce duplicates -- neither is wanted, and refusing to descend is
    simpler than detecting which it is.
    """
    base = resolve_in(root, subdirectory) if subdirectory else root
    if not base.is_dir():
        return []

    found: List[str] = []
    for current, directories, filenames in os.walk(base, followlinks=False):
        current_path = Path(current)

        # os.walk hands back the root itself first; guard anyway, cheaply.
        if not _is_inside(current_path.resolve(), root):
            directories[:] = []
            continue

        depth = len(current_path.relative_to(base).parts)
        if depth >= max_depth:
            directories[:] = []

        # Prune symlinked directories before descending into them.
        directories[:] = [
            name for name in sorted(directories)
            if not (current_path / name).is_symlink()
        ]

        for filename in sorted(filenames):
            entry = current_path / filename
            if entry.is_symlink():
                # A symlinked file could point anywhere; it is not workspace
                # content even though its name sits inside the workspace.
                continue
            found.append(relative_to_root(entry, root))
            if len(found) >= max_results:
                return found

    return found


__all__ = [
    "MAX_PATH_DEPTH",
    "MAX_RELATIVE_PATH_CHARS",
    "ensure_workspace",
    "list_files",
    "relative_to_root",
    "resolve_in",
    "workspace_root",
]
