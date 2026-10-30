"""Stage 6M.4: the one authenticator.

Mai has one owner and one API token. The operator generates the token and
configures only its SHA-256, as `MAI_API_TOKEN_SHA256`; the token itself is
never stored. A caller is authenticated by presenting the token, which is
hashed and compared in constant time with `hmac.compare_digest`.

Pure: standard library and settings only. It knows nothing of HTTP, requests,
the database or any channel. The request seam (`app.api.deps.get_owner_id`)
turns its answer into an HTTP status; background work never calls it.

`authenticate` says only "this owner" or "no". Whether the server is
configured is a separate question (`is_configured`), asked by the seam so it
can fail closed with a distinct status -- never answered by accepting a token.
"""

import hashlib
import hmac
import re
import uuid
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.owner import CONFIGURED_OWNER_ID

#: A SHA-256 digest, as lowercase or uppercase hex.
_DIGEST = re.compile(r"[0-9a-fA-F]{64}")

#: The longest token considered. Far beyond any real token; a bound so an
#: oversized header is refused without hashing it.
MAX_TOKEN_CHARS = 1024


def _configured_digest(settings: Settings) -> Optional[str]:
    value = (getattr(settings, "MAI_API_TOKEN_SHA256", "") or "").strip()
    return value.lower() if _DIGEST.fullmatch(value) else None


def is_configured(settings: Optional[Settings] = None) -> bool:
    """True when a well-formed token digest is configured."""
    return _configured_digest(settings or get_settings()) is not None


def authenticate(token: Optional[str], settings: Optional[Settings] = None) -> Optional[uuid.UUID]:
    """The configured owner if `token` is the API token, otherwise None."""
    expected = _configured_digest(settings or get_settings())
    if expected is None or not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    supplied = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return CONFIGURED_OWNER_ID if hmac.compare_digest(supplied, expected) else None


__all__ = ["MAX_TOKEN_CHARS", "authenticate", "is_configured"]
