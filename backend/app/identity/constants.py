"""Stage 7A: shared identity constants. Frozen interface, owned by the lead.

The default organisation exists so that the single-owner deployment keeps
working unchanged: its Admin member *is* the configured owner
(`app.core.owner.CONFIGURED_OWNER_ID`), so every existing owner-scoped row
already belongs to that Admin and nothing is rewritten.

Migration `0020_organisation_identity` seeds the same three rows with these
values spelled literally (a migration never imports application code); a test
asserts the two agree.
"""

import enum
import uuid

from app.core.owner import CONFIGURED_OWNER_ID

#: The one organisation of a single-owner deployment.
DEFAULT_ORGANISATION_ID = uuid.UUID("00000000-0000-0000-0000-00000000b1b1")
DEFAULT_ORGANISATION_NAME = "Default organisation"

#: The person behind the legacy owner.
LEGACY_ADMIN_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000c1c1")
LEGACY_ADMIN_DISPLAY_NAME = "Admin"

#: The legacy owner's membership id. The same value, imported: the owner id
#: every existing owner-scoped row carries is this member's id.
LEGACY_ADMIN_MEMBER_ID = CONFIGURED_OWNER_ID

#: Link codes: 16 characters from a 32-symbol alphabet (80 bits), with no
#: I, O, 0 or 1 to misread. Single use, stored only as a SHA-256 digest.
LINK_CODE_TTL_SECONDS = 600
LINK_CODE_LENGTH = 16
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

TELEGRAM_PROVIDER = "telegram"

MAX_ORGANISATION_NAME_CHARS = 200
MAX_DISPLAY_NAME_CHARS = 100
#: A Telegram user id as decimal text: positive, at most 20 digits.
MAX_SUBJECT_CHARS = 20
MAX_REVOKED_REASON_CHARS = 64


class Role(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


class MemberStatus(str, enum.Enum):
    INVITED = "invited"
    ACTIVE = "active"
    REVOKED = "revoked"


class OrganisationStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


__all__ = [
    "DEFAULT_ORGANISATION_ID",
    "DEFAULT_ORGANISATION_NAME",
    "LEGACY_ADMIN_DISPLAY_NAME",
    "LEGACY_ADMIN_MEMBER_ID",
    "LEGACY_ADMIN_USER_ID",
    "LINK_CODE_ALPHABET",
    "LINK_CODE_LENGTH",
    "LINK_CODE_TTL_SECONDS",
    "MAX_DISPLAY_NAME_CHARS",
    "MAX_ORGANISATION_NAME_CHARS",
    "MAX_REVOKED_REASON_CHARS",
    "MAX_SUBJECT_CHARS",
    "MemberStatus",
    "OrganisationStatus",
    "Role",
    "TELEGRAM_PROVIDER",
]
