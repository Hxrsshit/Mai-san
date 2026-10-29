"""Stage 7B test scopes: trusted values, made the way the application makes them.

Every scope here comes from `KnowledgeScope.personal(principal)`, never from
the constructor, so tests use the same path as the request seam. The
Admin's scope matches the legacy Admin seeded by migration 0020 / the test
fixtures; the others are for isolation tests (a second member of the same
organisation, a member of another organisation). Their rows are synthetic.
"""

import uuid

from app.identity.constants import DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID, Role
from app.identity.principal import Principal
from app.identity.scope import KnowledgeScope

ADMIN_PRINCIPAL = Principal(
    member_id=LEGACY_ADMIN_MEMBER_ID, organisation_id=DEFAULT_ORGANISATION_ID, role=Role.ADMIN
)
#: The scope every ordinary test runs in: the legacy Admin's own.
ADMIN_SCOPE = KnowledgeScope.personal(ADMIN_PRINCIPAL)

#: A second member of the default organisation.
COLLEAGUE_PRINCIPAL = Principal(
    member_id=uuid.UUID("00000000-0000-0000-0000-0000000c0111"),
    organisation_id=DEFAULT_ORGANISATION_ID,
    role=Role.MEMBER,
)
COLLEAGUE_SCOPE = KnowledgeScope.personal(COLLEAGUE_PRINCIPAL)

#: A member of a second organisation.
OUTSIDER_PRINCIPAL = Principal(
    member_id=uuid.UUID("00000000-0000-0000-0000-0000000f0e01"),
    organisation_id=uuid.UUID("00000000-0000-0000-0000-0000000f0e00"),
    role=Role.ADMIN,
)
OUTSIDER_SCOPE = KnowledgeScope.personal(OUTSIDER_PRINCIPAL)

__all__ = [
    "ADMIN_PRINCIPAL",
    "ADMIN_SCOPE",
    "COLLEAGUE_PRINCIPAL",
    "COLLEAGUE_SCOPE",
    "OUTSIDER_PRINCIPAL",
    "OUTSIDER_SCOPE",
]
