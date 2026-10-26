"""Stage 7A: the identity service's result types. Frozen interface, owned by the lead.

Implementer A's `MembershipService` returns these; the organisation routes
(implementer B) and the Telegram adapter (implementer C) consume them. None
carries a Telegram id, a code hash or a user id: what crosses this boundary
is what a response may show.
"""

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.identity.constants import MemberStatus, Role


@dataclass(frozen=True)
class MemberView:
    id: uuid.UUID
    display_name: str
    role: Role
    status: MemberStatus
    #: When the member's attached identity was linked; None while unlinked.
    linked_at: Optional[datetime]
    created_at: datetime
    revoked_at: Optional[datetime]


@dataclass(frozen=True)
class IssuedCode:
    """A link code's plaintext. Exists only to be returned once, never stored.

    `repr=False` on the code: a stray log line, f-string or exception repr
    of this object must not print it.
    """

    code: str = field(repr=False)
    expires_at: datetime = field()


class RedeemResult(str, enum.Enum):
    """The only two answers a redemption gives. Why it failed is logged as a
    reason code, never returned: a caller cannot tell an unknown code from an
    expired, used or refused one."""

    LINKED = "linked"
    INVALID = "invalid"


class RevokeOutcome(str, enum.Enum):
    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    NOT_FOUND = "not_found"
    CANNOT_REVOKE_ADMIN = "cannot_revoke_admin"


class IdentityError(Exception):
    """Base of the refusals `invite` and `issue_link_code` raise."""


class MemberNotFound(IdentityError):
    """No such member in this organisation (another organisation's member is
    the same answer)."""


class AdminLinkNotSupported(IdentityError):
    """Stage 7A: an Admin's Telegram account is never linked by code."""


class MemberRevoked(IdentityError):
    """A revoked member gets no new link code."""


class InvalidDisplayName(IdentityError):
    """Empty after stripping, too long, or containing control or format
    characters."""


__all__ = [
    "AdminLinkNotSupported",
    "IdentityError",
    "InvalidDisplayName",
    "IssuedCode",
    "MemberNotFound",
    "MemberRevoked",
    "MemberView",
    "RedeemResult",
    "RevokeOutcome",
]
