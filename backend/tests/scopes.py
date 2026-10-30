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

async def add_active_member(session, member_id: uuid.UUID,
                            organisation_id: uuid.UUID = DEFAULT_ORGANISATION_ID) -> None:
    """`make_active_member` inside an existing session (flushed, not
    committed): for tests that work in one `db_session`."""
    from datetime import datetime, timezone

    from app.identity.constants import MemberStatus
    from app.identity.models import OrganisationMember, User

    user = User(display_name="Synthetic member")
    session.add(user)
    await session.flush()
    session.add(OrganisationMember(
        id=member_id, organisation_id=organisation_id, user_id=user.id,
        role=Role.MEMBER.value, status=MemberStatus.ACTIVE.value,
        activated_at=datetime.now(timezone.utc)))
    await session.flush()


async def make_active_member(session_factory, member_id: uuid.UUID,
                             organisation_id: uuid.UUID = DEFAULT_ORGANISATION_ID) -> None:
    """A real, active membership row for `member_id` (synthetic user).

    Since Stage 7B unit 8b a task's executions are bound to its owner's scope
    as the owner is *now*: an owner id with no active membership is refused.
    Tests that run work for a second owner therefore make that owner a
    member, rather than relying on an id nobody owns."""
    from datetime import datetime, timezone

    from app.identity.constants import MemberStatus
    from app.identity.models import OrganisationMember, User

    async with session_factory() as session:
        user = User(display_name="Synthetic member")
        session.add(user)
        await session.flush()
        session.add(OrganisationMember(
            id=member_id, organisation_id=organisation_id, user_id=user.id,
            role=Role.MEMBER.value, status=MemberStatus.ACTIVE.value,
            activated_at=datetime.now(timezone.utc)))
        await session.commit()


__all__ = [
    "add_active_member",
    "make_active_member",
    "ADMIN_PRINCIPAL",
    "ADMIN_SCOPE",
    "COLLEAGUE_PRINCIPAL",
    "COLLEAGUE_SCOPE",
    "OUTSIDER_PRINCIPAL",
    "OUTSIDER_SCOPE",
]
