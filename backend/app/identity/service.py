"""Stage 7A: memberships, link codes and linked identities.

`MembershipService` is the one writer of the identity tables after the
migration's seed. It never commits: the caller owns the transaction, so a
route or adapter decides when a link, an invitation or a revocation becomes
durable, and a failure rolls all of it back together. Database errors
propagate unchanged, with one deliberate exception: an `IntegrityError` while
attaching an identity inside redemption's savepoint means the account is
already linked, which is an INVALID redemption, not a failure.

What it never does: touch tasks, grants, executions or the background
runtime (revoking a member's work is the caller's job), commit, or log a code,
a code hash, a Telegram id or a display name. Log lines carry reason codes and
member ids only.

Lock order, everywhere: member, then code.
"""

import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.identity.constants import (
    LEGACY_ADMIN_MEMBER_ID,
    LINK_CODE_TTL_SECONDS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_REVOKED_REASON_CHARS,
    TELEGRAM_PROVIDER,
    MemberStatus,
    OrganisationStatus,
    Role,
)
from app.identity.link_codes import code_sha256, generate_code, is_well_formed
from app.identity.models import (
    LinkCode,
    Organisation,
    OrganisationMember,
    User,
    UserIdentity,
)
from app.identity.principal import Principal, load_active_principal
from app.identity.schemas import (
    AdminLinkNotSupported,
    InvalidDisplayName,
    IssuedCode,
    MemberNotFound,
    MemberRevoked,
    MemberView,
    RedeemResult,
    RevokeOutcome,
)

logger = get_logger(__name__)

#: A Telegram user id as decimal text: positive, no leading zero, 1-20 digits.
_SUBJECT = re.compile(r"[1-9][0-9]{0,19}")

#: Unicode categories a display name may not contain: control, format
#: (bidirectional overrides, zero-width characters), line and paragraph
#: separators, surrogates, private use and unassigned code points. Each can
#: disguise a name in a list or a log viewer, or fail to encode at all.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})

MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 200

#: Recorded when a revocation gives no reason.
DEFAULT_REVOKED_REASON = "admin_revoked"

#: Statuses a member may link from.
_LINKABLE_STATUSES = frozenset({MemberStatus.INVITED.value, MemberStatus.ACTIVE.value})


class _CodeConsumedConcurrently(Exception):
    """The locked, re-checked code was consumed or invalidated anyway.

    Private: it can only mean the lock did not hold, so the caller must roll
    the whole transaction back (the identity row included) rather than commit
    a link that consumed nothing.
    """


class _RevocationRaced(Exception):
    """The locked, not-yet-revoked member did not match the revoking UPDATE.

    Private and unreachable while the member lock holds; if it happens, the
    caller must roll back rather than commit a half-revocation.
    """


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """An aware UTC datetime. SQLite returns stored times naive."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    """The moment to act at, as aware UTC. A naive `now` is refused: its
    instant is ambiguous, and every stored time here is UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _refuse(reason: str) -> RedeemResult:
    # A reason code only: never the code, its hash or the subject.
    logger.info("Link code refused", extra={"reason": reason})
    return RedeemResult.INVALID


def _member_view_query(organisation_id: uuid.UUID) -> Select:
    """The one query behind every `MemberView`: an organisation's members,
    each with its attached Telegram identity's `linked_at` (or NULL)."""
    return (
        select(
            OrganisationMember.id,
            User.display_name,
            OrganisationMember.role,
            OrganisationMember.status,
            UserIdentity.linked_at,
            OrganisationMember.created_at,
            OrganisationMember.revoked_at,
        )
        .join(User, User.id == OrganisationMember.user_id)
        .outerjoin(
            UserIdentity,
            (UserIdentity.user_id == OrganisationMember.user_id)
            & (UserIdentity.provider == TELEGRAM_PROVIDER)
            & UserIdentity.detached_at.is_(None),
        )
        .where(OrganisationMember.organisation_id == organisation_id)
    )


def _member_view(row) -> MemberView:
    """One row of `_member_view_query` as the value a response may show."""
    return MemberView(
        id=row[0],
        display_name=row[1],
        role=Role(row[2]),
        status=MemberStatus(row[3]),
        linked_at=_utc(row[4]),
        created_at=_utc(row[5]),
        revoked_at=_utc(row[6]),
    )


def _clean_display_name(display_name: str) -> str:
    if not isinstance(display_name, str):
        raise InvalidDisplayName("invalid_display_name")
    name = display_name.strip()
    if not name or len(name) > MAX_DISPLAY_NAME_CHARS:
        raise InvalidDisplayName("invalid_display_name")
    if any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in name):
        raise InvalidDisplayName("invalid_display_name")
    return name


class MembershipService:
    """Organisation members, their link codes and their linked identities."""

    def __init__(self, session: AsyncSession, settings: Optional[Settings] = None) -> None:
        self._session = session
        self._settings = settings

    # --- Reads ----------------------------------------------------------------

    async def active_principal(self, member_id: uuid.UUID) -> Optional[Principal]:
        return await load_active_principal(self._session, member_id)

    async def principal_for_identity(
        self, provider: str, subject: str
    ) -> Optional[Principal]:
        """The principal behind an attached identity, or None.

        Only active memberships of active organisations count, and only an
        unambiguous answer is returned: two would mean the caller cannot know
        which organisation is meant, so neither is chosen.
        """
        rows = (
            await self._session.execute(
                select(
                    OrganisationMember.id,
                    OrganisationMember.organisation_id,
                    OrganisationMember.role,
                )
                .join(UserIdentity, UserIdentity.user_id == OrganisationMember.user_id)
                .join(Organisation, Organisation.id == OrganisationMember.organisation_id)
                .where(
                    UserIdentity.provider == provider,
                    UserIdentity.subject == subject,
                    UserIdentity.detached_at.is_(None),
                    OrganisationMember.status == MemberStatus.ACTIVE.value,
                    Organisation.status == OrganisationStatus.ACTIVE.value,
                )
                .limit(2)
            )
        ).all()
        if len(rows) != 1:
            return None
        row = rows[0]
        return Principal(member_id=row[0], organisation_id=row[1], role=Role(row[2]))

    async def has_attached_identity(self, provider: str, subject: str) -> bool:
        found = (
            await self._session.execute(
                select(UserIdentity.id)
                .where(
                    UserIdentity.provider == provider,
                    UserIdentity.subject == subject,
                    UserIdentity.detached_at.is_(None),
                )
                .limit(1)
            )
        ).first()
        return found is not None

    async def organisation_name(self, organisation_id: uuid.UUID) -> Optional[str]:
        return (
            await self._session.execute(
                select(Organisation.name).where(Organisation.id == organisation_id)
            )
        ).scalar_one_or_none()

    async def list_members(
        self, organisation_id: uuid.UUID, limit: int = 100
    ) -> List[MemberView]:
        """This organisation's members, oldest first."""
        bounded = max(MIN_LIST_LIMIT, min(int(limit), MAX_LIST_LIMIT))
        rows = (
            await self._session.execute(
                _member_view_query(organisation_id)
                .order_by(OrganisationMember.created_at.asc(), OrganisationMember.id.asc())
                .limit(bounded)
            )
        ).all()
        return [_member_view(row) for row in rows]

    async def get_member(
        self, organisation_id: uuid.UUID, member_id: uuid.UUID
    ) -> Optional[MemberView]:
        """One member of this organisation, or None (another organisation's
        member is the same None). Built exactly as `list_members` builds it.
        No lock: a read for a response."""
        row = (
            await self._session.execute(
                _member_view_query(organisation_id).where(OrganisationMember.id == member_id)
            )
        ).first()
        return _member_view(row) if row is not None else None

    # --- Invitation and codes -------------------------------------------------

    async def invite(
        self,
        organisation_id: uuid.UUID,
        display_name: str,
        now: Optional[datetime] = None,
    ) -> Tuple[MemberView, IssuedCode]:
        """A new invited member of an active organisation, and its first code."""
        moment = _now(now)
        name = _clean_display_name(display_name)

        status = (
            await self._session.execute(
                select(Organisation.status).where(Organisation.id == organisation_id)
            )
        ).scalar_one_or_none()
        if status != OrganisationStatus.ACTIVE.value:
            logger.info("Invitation refused", extra={"reason": "organisation_unavailable"})
            raise MemberNotFound("member_not_found")

        user = User(display_name=name, created_at=moment, updated_at=moment)
        self._session.add(user)
        await self._session.flush()
        member = OrganisationMember(
            organisation_id=organisation_id,
            user_id=user.id,
            role=Role.MEMBER.value,
            status=MemberStatus.INVITED.value,
            created_at=moment,
        )
        self._session.add(member)
        await self._session.flush()
        member_id = member.id

        issued = await self.issue_link_code(organisation_id, member_id, now=moment)
        logger.info("Member invited", extra={"member_id": str(member_id)})
        view = MemberView(
            id=member_id,
            display_name=name,
            role=Role.MEMBER,
            status=MemberStatus.INVITED,
            linked_at=None,
            created_at=moment,
            revoked_at=None,
        )
        return view, issued

    async def issue_link_code(
        self,
        organisation_id: uuid.UUID,
        member_id: uuid.UUID,
        now: Optional[datetime] = None,
    ) -> IssuedCode:
        """A new code for a non-admin, unrevoked member. Every earlier open code
        of that member stops working in the same transaction."""
        moment = _now(now)
        member = (
            await self._session.execute(
                select(OrganisationMember)
                .where(
                    OrganisationMember.id == member_id,
                    OrganisationMember.organisation_id == organisation_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if member is None:
            logger.info("Link code not issued", extra={"reason": "member_not_found"})
            raise MemberNotFound("member_not_found")
        if member.role == Role.ADMIN.value:
            logger.info("Link code not issued", extra={"reason": "admin"})
            raise AdminLinkNotSupported("admin_link_not_supported")
        if member.status == MemberStatus.REVOKED.value:
            logger.info("Link code not issued", extra={"reason": "member_revoked"})
            raise MemberRevoked("member_revoked")

        await self._invalidate_open_codes(member.id, moment)

        code = generate_code()
        expires_at = moment + timedelta(seconds=LINK_CODE_TTL_SECONDS)
        self._session.add(
            LinkCode(
                member_id=member.id,
                code_sha256=code_sha256(code),
                created_at=moment,
                expires_at=expires_at,
            )
        )
        await self._session.flush()
        logger.info("Link code issued", extra={"member_id": str(member.id)})
        return IssuedCode(code=code, expires_at=expires_at)

    async def redeem_link_code(
        self,
        code: str,
        provider: str,
        subject: str,
        now: Optional[datetime] = None,
    ) -> RedeemResult:
        """Attach `subject` to the code's member, or INVALID.

        Every refusal is the same INVALID; the reason is logged as a code.
        """
        moment = _now(now)

        # 1. Shape.
        if provider != TELEGRAM_PROVIDER:
            return _refuse("provider")
        if not isinstance(subject, str) or _SUBJECT.fullmatch(subject) is None:
            return _refuse("subject")
        if not isinstance(code, str) or not is_well_formed(code):
            return _refuse("malformed")

        # 2. The Admin's own chat is never linked to a member.
        admin_subject = self._admin_subject()
        if admin_subject is not None and subject == admin_subject:
            return _refuse("admin_subject")

        digest = code_sha256(code)

        # 3. Which member, read without a lock (the lock order is member first).
        member_id = (
            await self._session.execute(
                select(LinkCode.member_id).where(LinkCode.code_sha256 == digest)
            )
        ).scalar_one_or_none()
        if member_id is None:
            return _refuse("unknown")

        # 4. Lock the member, then the code.
        member = (
            await self._session.execute(
                select(OrganisationMember)
                .where(OrganisationMember.id == member_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if member is None:
            return _refuse("unknown")
        link_code = (
            await self._session.execute(
                select(LinkCode)
                .where(LinkCode.code_sha256 == digest, LinkCode.member_id == member_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if link_code is None:
            return _refuse("unknown")

        # 5. Re-check under the locks.
        if link_code.consumed_at is not None:
            return _refuse("consumed")
        if link_code.invalidated_at is not None:
            return _refuse("invalidated")
        if _utc(link_code.expires_at) <= moment:
            return _refuse("expired")
        if member.role != Role.MEMBER.value:
            return _refuse("not_member_role")
        if member.status not in _LINKABLE_STATUSES:
            return _refuse("member_status")
        organisation_status = (
            await self._session.execute(
                select(Organisation.status).where(Organisation.id == member.organisation_id)
            )
        ).scalar_one_or_none()
        if organisation_status != OrganisationStatus.ACTIVE.value:
            return _refuse("organisation_unavailable")

        code_id = link_code.id
        user_id = member.user_id
        was_invited = member.status == MemberStatus.INVITED.value

        # 6. Attach the identity in a savepoint. A unique-index violation
        #    means the account or the person is already linked; the savepoint
        #    rolls back and the code stays unconsumed.
        #    Flush the caller's pending work first, outside the `try`:
        #    `begin_nested()` would flush it inside, and with autoflush off a
        #    pending violation of the caller's would read as identity_in_use.
        await self._session.flush()
        try:
            async with self._session.begin_nested():
                self._session.add(
                    UserIdentity(
                        user_id=user_id,
                        provider=provider,
                        subject=subject,
                        linked_at=moment,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            # Deliberately no exception text and no exc_info: the driver's
            # message would quote the subject.
            return _refuse("identity_in_use")

        # 7. Consume the code, conditionally.
        consumed = await self._session.execute(
            update(LinkCode)
            .where(
                LinkCode.id == code_id,
                LinkCode.consumed_at.is_(None),
                LinkCode.invalidated_at.is_(None),
            )
            .values(consumed_at=moment)
        )
        if consumed.rowcount != 1:
            logger.warning("Link code consumed concurrently", extra={"reason": "code_race"})
            raise _CodeConsumedConcurrently("code_race")

        # 8. An invited member becomes active.
        if was_invited:
            await self._session.execute(
                update(OrganisationMember)
                .where(
                    OrganisationMember.id == member_id,
                    OrganisationMember.status == MemberStatus.INVITED.value,
                )
                .values(status=MemberStatus.ACTIVE.value, activated_at=moment)
            )

        logger.info("Identity linked", extra={"member_id": str(member_id)})
        return RedeemResult.LINKED

    # --- Revocation -----------------------------------------------------------

    async def mark_revoked(
        self,
        organisation_id: uuid.UUID,
        member_id: uuid.UUID,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> RevokeOutcome:
        """Revoke a member: status, open codes and attached identities.

        Idempotent: a second call reports ALREADY_REVOKED and still closes any
        code or identity left open. Tasks and grants are the caller's.
        """
        moment = _now(now)
        member = (
            await self._session.execute(
                select(OrganisationMember)
                .where(
                    OrganisationMember.id == member_id,
                    OrganisationMember.organisation_id == organisation_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if member is None:
            logger.info("Revocation refused", extra={"reason": "member_not_found"})
            return RevokeOutcome.NOT_FOUND
        if member.id == LEGACY_ADMIN_MEMBER_ID or member.role == Role.ADMIN.value:
            logger.info("Revocation refused", extra={"reason": "admin"})
            return RevokeOutcome.CANNOT_REVOKE_ADMIN

        user_id = member.user_id
        if member.status != MemberStatus.REVOKED.value:
            revoked = await self._session.execute(
                update(OrganisationMember)
                .where(
                    OrganisationMember.id == member_id,
                    OrganisationMember.status.in_(
                        (MemberStatus.INVITED.value, MemberStatus.ACTIVE.value)
                    ),
                )
                .values(
                    status=MemberStatus.REVOKED.value,
                    revoked_at=moment,
                    revoked_reason=(reason or DEFAULT_REVOKED_REASON)[:MAX_REVOKED_REASON_CHARS],
                )
            )
            if revoked.rowcount != 1:
                logger.warning("Revocation raced", extra={"reason": "revocation_race"})
                raise _RevocationRaced("revocation_race")
            outcome = RevokeOutcome.REVOKED
        else:
            outcome = RevokeOutcome.ALREADY_REVOKED

        await self._invalidate_open_codes(member_id, moment)
        await self._session.execute(
            update(UserIdentity)
            .where(UserIdentity.user_id == user_id, UserIdentity.detached_at.is_(None))
            .values(detached_at=moment)
        )
        logger.info(
            "Member revoked",
            extra={"member_id": str(member_id), "outcome": outcome.value},
        )
        return outcome

    # --- Internals ------------------------------------------------------------

    async def _invalidate_open_codes(self, member_id: uuid.UUID, moment: datetime) -> None:
        await self._session.execute(
            update(LinkCode)
            .where(
                LinkCode.member_id == member_id,
                LinkCode.consumed_at.is_(None),
                LinkCode.invalidated_at.is_(None),
            )
            .values(invalidated_at=moment)
        )

    def _admin_subject(self) -> Optional[str]:
        """The Admin's configured Telegram chat as decimal text, or None."""
        settings = self._settings if self._settings is not None else get_settings()
        raw = (settings.TELEGRAM_ALLOWED_CHAT_ID or "").strip()
        try:
            value = int(raw)
        except ValueError:
            return None
        return str(value) if value > 0 else None


__all__ = ["MembershipService"]
