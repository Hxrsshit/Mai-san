"""Stage 7A: who an authenticated caller is. Frozen interface, owned by the lead.

`Principal` is a frozen value of plain fields, never an ORM row, so it stays
valid after the transaction that read it has ended (the request seam ends its
read transaction at once; an expired ORM row would then reload lazily, which
under the async session is an error).

`load_active_principal` is the one membership read the request seam uses.
It answers only for an **active** member of an **active** organisation, and
it lets database errors propagate: a failure must never read as "inactive"
and never as a default principal. The caller decides what an error means.
"""

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.constants import MemberStatus, OrganisationStatus, Role
from app.identity.models import Organisation, OrganisationMember


@dataclass(frozen=True)
class Principal:
    member_id: uuid.UUID
    organisation_id: uuid.UUID
    role: Role


def _active_member(member_id: uuid.UUID):
    """The one membership query: an active member of an active organisation."""
    return (
        select(
            OrganisationMember.id,
            OrganisationMember.organisation_id,
            OrganisationMember.role,
        )
        .join(Organisation, Organisation.id == OrganisationMember.organisation_id)
        .where(
            OrganisationMember.id == member_id,
            OrganisationMember.status == MemberStatus.ACTIVE.value,
            Organisation.status == OrganisationStatus.ACTIVE.value,
        )
    )


def _principal(row) -> Optional[Principal]:
    if row is None:
        return None
    return Principal(member_id=row[0], organisation_id=row[1], role=Role(row[2]))


async def load_active_principal(
    session: AsyncSession, member_id: uuid.UUID
) -> Optional[Principal]:
    """The active member `member_id`, or None. One indexed read; no cache."""
    return _principal((await session.execute(_active_member(member_id))).first())


async def lock_active_principal(
    session: AsyncSession, member_id: uuid.UUID
) -> Optional[Principal]:
    """`load_active_principal`, holding a share lock on the membership row
    until the caller's transaction ends (Stage 7B unit 7).

    For background work done on a member's behalf. Revocation
    (`MembershipService.mark_revoked`) takes the row FOR UPDATE, so the two
    serialize: either the revocation committed first and this returns None,
    or the revocation waits until the work's transaction has committed. Work
    written under the lock is work the member was still entitled to. SQLite
    has no row locks and ignores the clause; there a single writer serializes
    the same way. Database errors propagate, exactly as for
    `load_active_principal`: an error is never "inactive"."""
    statement = _active_member(member_id).with_for_update(read=True, of=OrganisationMember)
    return _principal((await session.execute(statement)).first())


__all__ = ["Principal", "load_active_principal", "lock_active_principal"]
