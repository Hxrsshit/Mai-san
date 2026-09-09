"""Where OAuth tokens live.

Stage 4F-A designed the credential *abstraction* -- `CredentialType.OAUTH2`,
`required_scopes`, `granted_scopes`, `expires_at` -- and left the storage
unimplemented, because there was nothing to store. This is the storage.

Why a file with 0600 and not encryption
---------------------------------------

The obvious next thought is to encrypt the file. For this deployment that
would be theatre, and saying so plainly is better than shipping it:

Mai runs as one user on one machine. The threats that actually apply are
another local account reading the file, and the file escaping into a git
repository or a container image. Mode 0600 answers the first. `.gitignore`,
`.dockerignore` and a default path outside the source tree answer the second.

Encrypting with a key from the same `.env` answers neither: an attacker who
can read the token file can read the key beside it. It would add a dependency,
a failure mode, and a comforting word in a report, and would move no threat.

**What this is not:** encryption at rest, protection against a compromised
account, or protection against root. A deployment that needs those wants an OS
keychain or a secret manager, and the `CredentialResolver` abstraction is
where that plugs in -- this class implements one strategy behind it, not the
only possible one.

Nothing here is written to PostgreSQL. Tokens are not application data.
"""

import json
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Permissions the store and its files are created with, and required to have.
#:
#: Checked on read as well as set on write: a file that has been widened since
#: it was written is refused rather than used, because a token readable by
#: other accounts is one that should be re-issued rather than trusted.
_FILE_MODE = 0o600
_DIR_MODE = 0o700

#: Seconds before expiry at which a token is treated as already expired.
#:
#: A token that expires during the request it was fetched for is a race the
#: caller cannot handle, so it is refreshed early instead.
EXPIRY_SKEW_SECONDS = 120


class TokenStoreError(Exception):
    """The store could not be read or written. Carries no token material."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StoredToken:
    """One account's tokens. Never rendered, never serialised outward.

    `__repr__` is overridden because the default would print the values, and a
    debugger, a log line or an exception context would then carry them. There
    is no code path that formats this object for display.
    """

    __slots__ = ("access_token", "refresh_token", "expires_at", "scopes", "account")

    def __init__(
        self,
        access_token: str,
        refresh_token: str = "",
        expires_at: Optional[datetime] = None,
        scopes: Tuple[str, ...] = (),
        account: str = "default",
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.scopes = tuple(scopes)
        self.account = account

    def __repr__(self) -> str:
        return (
            f"<StoredToken account={self.account!r} "
            f"scopes={len(self.scopes)} expires_at={self.expires_at!r}>"
        )

    __str__ = __repr__

    @property
    def expired(self) -> bool:
        """True when the access token is spent, or nearly.

        No expiry recorded is treated as expired: a token whose lifetime the
        application cannot see is one it should refresh rather than gamble on.
        """
        if self.expires_at is None:
            return True
        expires = self.expires_at
        if expires.tzinfo is None:
            # SQLite-style naive datetimes never reach here, but a
            # hand-edited file could.
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires - timedelta(
            seconds=EXPIRY_SKEW_SECONDS
        )

    def covers(self, required) -> bool:
        """Whether the granted scopes include everything asked for."""
        return set(required or ()) <= set(self.scopes)


class FileTokenStore:
    """Tokens on disk, one JSON file per provider, mode 0600."""

    def __init__(self, directory: str) -> None:
        self._directory = Path(directory).expanduser()

    # --- Reading ------------------------------------------------------------

    def load(self, provider: str, account: str = "default") -> Optional[StoredToken]:
        """The stored token, or None. Never raises on a missing file."""
        path = self._path(provider, account)
        if not path.exists():
            return None

        if not self._permissions_are_tight(path):
            # Refused rather than used. A token file that has become readable
            # by other accounts should be re-issued, not trusted.
            logger.warning(
                "Refusing a token file with permissive mode",
                extra={"provider": provider, "account": account},
            )
            raise TokenStoreError("token_file_permissions")

        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise TokenStoreError("token_file_unreadable") from exc

        if not isinstance(payload, dict):
            raise TokenStoreError("token_file_malformed")

        expires_raw = payload.get("expires_at")
        expires_at = None
        if isinstance(expires_raw, str):
            try:
                expires_at = datetime.fromisoformat(expires_raw)
            except ValueError:
                expires_at = None

        return StoredToken(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=expires_at,
            scopes=tuple(
                str(scope) for scope in payload.get("scopes", []) if scope
            ),
            account=account,
        )

    # --- Writing ------------------------------------------------------------

    def save(self, provider: str, token: StoredToken) -> None:
        """Write atomically, with tight permissions from the moment it exists.

        Written to a temporary file in the same directory and renamed. A
        partial write must never be readable as a whole token, and the
        permissions are set on the descriptor rather than after the fact --
        `open` then `chmod` leaves a window in which the file exists with the
        default mode.
        """
        self._ensure_directory()
        path = self._path(provider, token.account)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

        payload = {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "scopes": list(token.scopes),
        }

        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
            )
        except OSError as exc:
            # Almost always a directory the process cannot write -- a
            # container volume owned by root, say. Raised as this module's own
            # error so the caller can say what happened: the failure lands at
            # the *end* of the consent flow, after the user has already
            # granted access at Google, and an unhandled OSError there is a
            # 500 with no explanation of what to fix.
            raise TokenStoreError("token_directory_not_writable") from exc
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        os.replace(temporary, path)
        os.chmod(path, _FILE_MODE)

    def delete(self, provider: str, account: str = "default") -> bool:
        """Remove a stored token. True when one was there."""
        path = self._path(provider, account)
        if not path.exists():
            return False
        path.unlink()
        return True

    # --- Internals ----------------------------------------------------------

    def _path(self, provider: str, account: str) -> Path:
        """One file per provider and account, with names that cannot escape.

        Both parts are reduced to a narrow alphabet, so neither can contain a
        separator, a `..`, or a null byte -- the filename cannot leave the
        directory however it is called.
        """
        safe_provider = self._safe(provider)
        safe_account = self._safe(account)
        return self._directory / f"{safe_provider}.{safe_account}.json"

    @staticmethod
    def _safe(value: str) -> str:
        cleaned = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in (value or "")
        ).strip("-")
        return cleaned[:64] or "unknown"

    def _ensure_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        try:
            os.chmod(self._directory, _DIR_MODE)
        except OSError:
            # A directory Mai does not own. The file mode check on read is
            # the guard that matters.
            pass

    @staticmethod
    def _permissions_are_tight(path: Path) -> bool:
        mode = stat.S_IMODE(path.stat().st_mode)
        # No group or other bits at all.
        return not (mode & 0o077)


__all__ = [
    "EXPIRY_SKEW_SECONDS",
    "FileTokenStore",
    "StoredToken",
    "TokenStoreError",
]
