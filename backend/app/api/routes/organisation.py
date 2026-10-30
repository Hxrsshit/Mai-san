"""Stage 7A: the organisation's members, for its Admin.

Five routes: read the organisation, list its members, invite one, issue a
member a new link code, and revoke a member. Every one requires an Admin
(`AdminPrincipal`, which depends on the authentication seam), and the
organisation is always the Admin's own -- `principal.organisation_id`, never
a request value, so another organisation's member is the same 404 as a
missing one.

HTTP only. Membership changes are `MembershipService`'s; a revocation, with
the member's work and standing grants, is `MemberRevocation`'s, which owns
its transaction. Nothing here grants, schedules, executes or approves.

Responses carry the member's id, name, role, status and times. Never a user
id, an external account id, a code hash or a token. A link code's plaintext
appears exactly once, in the response that issued it, marked `no-store`.

The two routes that issue a code commit before they answer. The request
session's own commit runs only after the response has been sent, so a code
answered before its row is durable could be a code that never existed. A
database error while issuing or committing is 503 `database_error`, rolled
back and logged by class name only, and no code is returned.

This module imports nothing from `app.organisation`; `RevocationIncomplete`
comes through `app.api.deps`, the one place the API reaches that package.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AdminPrincipal,
    DbSession,
    MemberRevocationDep,
    MembershipServiceDep,
    RevocationIncomplete,
)
from app.core.errors import DatabaseError
from app.core.logging import get_logger
from app.identity.schemas import (
    AdminLinkNotSupported,
    InvalidDisplayName,
    IssuedCode,
    MemberNotFound,
    MemberRevoked,
    MemberView,
    RevokeOutcome,
)
from app.identity.service import MAX_LIST_LIMIT, MIN_LIST_LIMIT

logger = get_logger(__name__)

router = APIRouter(prefix="/api/organisation", tags=["organisation"])

#: A response holding a link code must not be kept by any cache.
NO_STORE = "no-store"

#: Longest `display_name` a request body may carry. Not the name rule (the
#: service's 1..100 after stripping); a cheap bound on what reaches it.
MAX_DISPLAY_NAME_BODY_CHARS = 1000


# --- Bodies ---------------------------------------------------------------------


class EmptyRequest(BaseModel):
    """An empty JSON object. Required, so that only a JSON request -- which a
    cross-site form cannot send without a preflight -- can write anything.
    The same rule as the task cancel route."""

    model_config = ConfigDict(extra="forbid", strict=True)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    #: Validated by `MembershipService` (non-empty after stripping, at most
    #: 100 characters, no control or format characters). The bound here only
    #: refuses a huge body before it reaches the service.
    display_name: str = Field(max_length=MAX_DISPLAY_NAME_BODY_CHARS)


# --- Responses -------------------------------------------------------------------


class OrganisationRead(BaseModel):
    id: uuid.UUID
    name: str


class MemberRead(BaseModel):
    id: uuid.UUID
    display_name: str
    role: str
    status: str
    linked_at: Optional[datetime]
    created_at: datetime
    revoked_at: Optional[datetime]


class MemberList(BaseModel):
    members: List[MemberRead]


class LinkCodeRead(BaseModel):
    code: str
    expires_at: datetime


class InviteRead(BaseModel):
    member: MemberRead
    link_code: LinkCodeRead


class RevokeRead(BaseModel):
    member: MemberRead


def _member(view: MemberView) -> MemberRead:
    return MemberRead(
        id=view.id,
        display_name=view.display_name,
        role=view.role.value,
        status=view.status.value,
        linked_at=view.linked_at,
        created_at=view.created_at,
        revoked_at=view.revoked_at,
    )


def _code(issued: IssuedCode) -> LinkCodeRead:
    return LinkCodeRead(code=issued.code, expires_at=issued.expires_at)


#: What a database call can raise. Drivers can let connection errors escape
#: unwrapped (`get_db_session` catches the same pair).
_DATABASE_FAILURES = (SQLAlchemyError, OSError)


async def _fail_closed(session: AsyncSession, message: str, category: str) -> DatabaseError:
    """Roll back quietly, log the category, and return the 503 to raise.

    Called **outside** the `except` block that caught the failure, so no
    exception is being handled while the rollback and the log run: nothing
    downstream (the global handler, a pool or driver log) can render the
    original error, whose text quotes bound parameters -- here a display
    name or a code's hash. Only the error's class name is logged.
    """
    try:
        await session.rollback()
    except _DATABASE_FAILURES:
        pass
    logger.error(message, extra={"category": category})
    return DatabaseError()


async def _commit_before_answering(session: AsyncSession) -> None:
    """Make the issued code durable before it is shown, or 503."""
    failed: Optional[str] = None
    try:
        await session.commit()
    except _DATABASE_FAILURES as exc:
        failed = type(exc).__name__
    if failed is not None:
        raise await _fail_closed(session, "Link code commit failed", failed) from None


# --- Routes ------------------------------------------------------------------------


@router.get("", response_model=OrganisationRead)
async def get_organisation(
    principal: AdminPrincipal, service: MembershipServiceDep
) -> OrganisationRead:
    """The Admin's organisation."""
    name = await service.organisation_name(principal.organisation_id)
    if name is None:
        # Unreachable while the seam requires an active organisation.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="organisation_not_found"
        )
    return OrganisationRead(id=principal.organisation_id, name=name)


@router.get("/members", response_model=MemberList)
async def list_members(
    principal: AdminPrincipal,
    service: MembershipServiceDep,
    limit: int = Query(default=100, ge=MIN_LIST_LIMIT, le=MAX_LIST_LIMIT),
) -> MemberList:
    """The organisation's members, oldest first. Bounded."""
    views = await service.list_members(principal.organisation_id, limit=limit)
    return MemberList(members=[_member(view) for view in views])


@router.post("/members", response_model=InviteRead, status_code=status.HTTP_201_CREATED)
async def invite_member(
    body: InviteRequest,
    response: Response,
    principal: AdminPrincipal,
    service: MembershipServiceDep,
    session: DbSession,
) -> InviteRead:
    """Invite a member and issue its first link code.

    Committed here, before the code is answered; rolled back with the
    request session on any error before that.
    """
    failed: Optional[str] = None
    try:
        view, issued = await service.invite(principal.organisation_id, body.display_name)
    except InvalidDisplayName:
        raise HTTPException(
            status_code=422, detail="invalid_display_name"
        ) from None
    except MemberNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="organisation_not_found"
        ) from None
    except _DATABASE_FAILURES as exc:
        failed = type(exc).__name__
    if failed is not None:
        # Outside the `except`: see `_fail_closed`.
        raise await _fail_closed(session, "Invitation failed", failed) from None
    await _commit_before_answering(session)
    response.headers["Cache-Control"] = NO_STORE
    return InviteRead(member=_member(view), link_code=_code(issued))


@router.post(
    "/members/{member_id}/link-codes",
    response_model=LinkCodeRead,
    status_code=status.HTTP_201_CREATED,
)
async def issue_link_code(
    member_id: uuid.UUID,
    body: EmptyRequest,
    response: Response,
    principal: AdminPrincipal,
    service: MembershipServiceDep,
    session: DbSession,
) -> LinkCodeRead:
    """A new link code for a member. Every earlier open code stops working.

    Committed here, before the code is answered."""
    failed: Optional[str] = None
    try:
        issued = await service.issue_link_code(principal.organisation_id, member_id)
    except MemberNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="member_not_found"
        ) from None
    except AdminLinkNotSupported:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="admin_link_not_supported"
        ) from None
    except MemberRevoked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="member_revoked"
        ) from None
    except _DATABASE_FAILURES as exc:
        failed = type(exc).__name__
    if failed is not None:
        # Outside the `except`: see `_fail_closed`.
        raise await _fail_closed(session, "Link code issue failed", failed) from None
    await _commit_before_answering(session)
    response.headers["Cache-Control"] = NO_STORE
    return _code(issued)


@router.post("/members/{member_id}/revoke", response_model=RevokeRead)
async def revoke_member(
    member_id: uuid.UUID,
    body: EmptyRequest,
    principal: AdminPrincipal,
    revocation: MemberRevocationDep,
    service: MembershipServiceDep,
) -> RevokeRead:
    """Revoke a member, its open work and its standing grants, atomically.

    Idempotent: a revoked member is 200 again (and anything left is still
    closed). 404 `member_not_found`, 409 `cannot_revoke_admin`, 500
    `revocation_failed` with nothing changed.
    """
    try:
        outcome = await revocation.revoke(principal.organisation_id, member_id)
    except RevocationIncomplete:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="revocation_failed"
        ) from None
    if outcome is RevokeOutcome.NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="member_not_found"
        )
    if outcome is RevokeOutcome.CANNOT_REVOKE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="cannot_revoke_admin"
        )
    view = await service.get_member(principal.organisation_id, member_id)
    if view is None:
        # Unreachable: `revoke` just accepted this member in this
        # organisation, and members are never deleted. Never answered as a
        # success it cannot show.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="revocation_failed"
        )
    return RevokeRead(member=_member(view))


__all__ = ["router"]
