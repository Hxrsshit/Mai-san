"""Stage 7A: `MembershipService` through a real (SQLite) database.

Every test database is seeded by the conftest with the default organisation
and its active Admin (the configured owner). Subjects and display names here
are synthetic.
"""

import ast
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.identity.service as service_module

from app.core.config import Settings
from app.core.owner import CONFIGURED_OWNER_ID
from app.database.metadata import Base
from app.identity.constants import (
    DEFAULT_ORGANISATION_ID,
    LEGACY_ADMIN_MEMBER_ID,
    LINK_CODE_TTL_SECONDS,
    TELEGRAM_PROVIDER,
    MemberStatus,
    OrganisationStatus,
    Role,
)
from app.identity.link_codes import code_sha256
from app.identity.models import (
    LinkCode,
    Organisation,
    OrganisationMember,
    User,
    UserIdentity,
)
from app.identity.schemas import (
    AdminLinkNotSupported,
    InvalidDisplayName,
    MemberNotFound,
    MemberRevoked,
    RedeemResult,
    RevokeOutcome,
)
from app.identity.service import MembershipService

SUBJECT = "918273645512"
OTHER_SUBJECT = "556677889900"
ADMIN_CHAT = "424242424242"
SERVICE_LOGGER = "app.identity.service"


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@pytest.fixture
def svc_settings(settings: Settings) -> Settings:
    """The suite's settings with no admin chat configured, deterministically."""
    return settings.model_copy(update={"TELEGRAM_ALLOWED_CHAT_ID": ""})


@pytest.fixture
def service(db_session, svc_settings) -> MembershipService:
    return MembershipService(db_session, svc_settings)


async def _code_row(session, code: str) -> LinkCode:
    return (
        await session.execute(
            select(LinkCode)
            .where(LinkCode.code_sha256 == code_sha256(code))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _member_row(session, member_id: uuid.UUID) -> OrganisationMember:
    return (
        await session.execute(
            select(OrganisationMember)
            .where(OrganisationMember.id == member_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _identities(session, user_id: uuid.UUID) -> List[UserIdentity]:
    return list(
        (
            await session.execute(
                select(UserIdentity)
                .where(UserIdentity.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _other_organisation(session, status: str = "active", member_status: str = "active"):
    """A second organisation with one member. Returns (organisation id, member id)."""
    now = datetime.now(timezone.utc)
    organisation = Organisation(name="Other organisation", status=status)
    user = User(display_name="Other person")
    session.add_all([organisation, user])
    await session.flush()
    member = OrganisationMember(
        organisation_id=organisation.id,
        user_id=user.id,
        role=Role.MEMBER.value,
        status=member_status,
        activated_at=now if member_status == "active" else None,
    )
    session.add(member)
    await session.flush()
    return organisation.id, member.id


def _reasons(caplog) -> List[str]:
    return [
        getattr(record, "reason")
        for record in caplog.records
        if record.name == SERVICE_LOGGER and hasattr(record, "reason")
    ]


# --- Seed and principals -----------------------------------------------------


async def test_the_seeded_admin_is_the_active_principal(service) -> None:
    principal = await service.active_principal(CONFIGURED_OWNER_ID)
    assert principal is not None
    assert principal.member_id == LEGACY_ADMIN_MEMBER_ID == CONFIGURED_OWNER_ID
    assert principal.organisation_id == DEFAULT_ORGANISATION_ID
    assert principal.role is Role.ADMIN


async def test_no_principal_for_an_invited_member(service) -> None:
    view, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Invited person")
    assert await service.active_principal(view.id) is None


async def test_no_principal_for_a_revoked_member(service) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Soon revoked")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    assert await service.active_principal(view.id) is not None
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id) is RevokeOutcome.REVOKED
    assert await service.active_principal(view.id) is None


async def test_no_principal_in_a_suspended_organisation(service, db_session) -> None:
    _, member_id = await _other_organisation(db_session, status=OrganisationStatus.SUSPENDED.value)
    assert await service.active_principal(member_id) is None


async def test_no_principal_for_an_unknown_member(service) -> None:
    assert await service.active_principal(uuid.uuid4()) is None


async def test_organisation_name(service) -> None:
    assert await service.organisation_name(DEFAULT_ORGANISATION_ID) == "Default organisation"
    assert await service.organisation_name(uuid.uuid4()) is None


# --- invite --------------------------------------------------------------------


async def test_invite_creates_an_invited_member_and_returns_a_code(service, db_session) -> None:
    now = datetime.now(timezone.utc)
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "  Asha  ", now=now)

    assert view.display_name == "Asha"
    assert view.role is Role.MEMBER
    assert view.status is MemberStatus.INVITED
    assert view.linked_at is None and view.revoked_at is None
    assert issued.expires_at == now + timedelta(seconds=LINK_CODE_TTL_SECONDS)
    assert len(issued.code) == 16

    member = await _member_row(db_session, view.id)
    assert member.organisation_id == DEFAULT_ORGANISATION_ID
    assert member.role == Role.MEMBER.value
    assert member.status == MemberStatus.INVITED.value
    assert member.activated_at is None
    user = (await db_session.execute(select(User).where(User.id == member.user_id))).scalar_one()
    assert user.display_name == "Asha"

    row = await _code_row(db_session, issued.code)
    assert row.member_id == view.id
    assert row.consumed_at is None and row.invalidated_at is None
    assert _aware(row.expires_at) == issued.expires_at

    members = await service.list_members(DEFAULT_ORGANISATION_ID)
    assert [m.id for m in members] == [LEGACY_ADMIN_MEMBER_ID, view.id]
    assert members[0].role is Role.ADMIN and members[0].status is MemberStatus.ACTIVE


async def test_invite_stores_only_the_hash(service, db_session) -> None:
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Hash only")
    await db_session.flush()
    digest = code_sha256(issued.code)
    seen_digest = False
    checked = 0
    for table in Base.metadata.sorted_tables:
        for row in (await db_session.execute(table.select())).all():
            for value in row:
                checked += 1
                if isinstance(value, str):
                    assert issued.code not in value
                    assert issued.code.lower() not in value.lower() or value == digest
                    seen_digest = seen_digest or value == digest
    assert checked > 0
    assert seen_digest


@pytest.mark.parametrize(
    "name",
    [
        "", "   ", "x" * 101, "a‮b", "bell\x07", "line sep", "para sep", "zw​j",
        "lone\ud800surrogate",  # Cs
        "privateuse",  # Co
        "unassigned͸",  # Cn
    ],
)
async def test_invite_rejects_bad_display_names(service, db_session, name: str) -> None:
    with pytest.raises(InvalidDisplayName):
        await service.invite(DEFAULT_ORGANISATION_ID, name)
    count = len((await db_session.execute(select(OrganisationMember.id))).all())
    assert count == 1  # only the seeded Admin


async def test_invite_accepts_a_name_of_exactly_100_characters(service) -> None:
    view, _ = await service.invite(DEFAULT_ORGANISATION_ID, "y" * 100)
    assert view.display_name == "y" * 100


async def test_invite_refuses_an_unknown_or_suspended_organisation(service, db_session) -> None:
    with pytest.raises(MemberNotFound):
        await service.invite(uuid.uuid4(), "Nobody")
    suspended, _ = await _other_organisation(db_session, status=OrganisationStatus.SUSPENDED.value)
    with pytest.raises(MemberNotFound):
        await service.invite(suspended, "Nobody")


# --- issue_link_code -----------------------------------------------------------


async def test_issuing_a_new_code_invalidates_the_earlier_open_code(service, db_session) -> None:
    view, first = await service.invite(DEFAULT_ORGANISATION_ID, "Reissued")
    now = datetime.now(timezone.utc)
    second = await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id, now=now)
    assert second.code != first.code

    first_row = await _code_row(db_session, first.code)
    second_row = await _code_row(db_session, second.code)
    assert _aware(first_row.invalidated_at) == now
    assert first_row.consumed_at is None
    assert second_row.invalidated_at is None and second_row.consumed_at is None


async def test_issue_refuses_the_admin(service, db_session) -> None:
    with pytest.raises(AdminLinkNotSupported):
        await service.issue_link_code(DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID)
    assert (await db_session.execute(select(LinkCode.id))).first() is None


async def test_issue_refuses_a_revoked_member(service, db_session) -> None:
    view, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Revoked")
    await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id)
    with pytest.raises(MemberRevoked):
        await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)
    open_codes = (
        await db_session.execute(
            select(LinkCode.id).where(
                LinkCode.member_id == view.id,
                LinkCode.consumed_at.is_(None),
                LinkCode.invalidated_at.is_(None),
            )
        )
    ).all()
    assert open_codes == []


async def test_issue_member_not_found_for_unknown_or_foreign_member(service, db_session) -> None:
    with pytest.raises(MemberNotFound):
        await service.issue_link_code(DEFAULT_ORGANISATION_ID, uuid.uuid4())
    other_org, other_member = await _other_organisation(db_session)
    with pytest.raises(MemberNotFound):
        await service.issue_link_code(DEFAULT_ORGANISATION_ID, other_member)
    # The member is real; only the organisation scope refused it.
    issued = await service.issue_link_code(other_org, other_member)
    assert issued.code


# --- redeem_link_code ------------------------------------------------------------


async def test_redeem_links_once(service, db_session) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Linker")
    now = datetime.now(timezone.utc)

    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False
    assert await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT) is None

    result = await service.redeem_link_code(issued.code.lower(), TELEGRAM_PROVIDER, SUBJECT, now=now)
    assert result is RedeemResult.LINKED

    member = await _member_row(db_session, view.id)
    assert member.status == MemberStatus.ACTIVE.value
    assert _aware(member.activated_at) == now
    identities = await _identities(db_session, member.user_id)
    assert len(identities) == 1
    assert identities[0].provider == TELEGRAM_PROVIDER
    assert identities[0].subject == SUBJECT
    assert identities[0].detached_at is None
    assert _aware(identities[0].linked_at) == now
    row = await _code_row(db_session, issued.code)
    assert _aware(row.consumed_at) == now
    assert row.invalidated_at is None

    # A second redemption of the same code, by anyone, is refused.
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, OTHER_SUBJECT) is RedeemResult.INVALID
    assert len(await _identities(db_session, member.user_id)) == 1
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, OTHER_SUBJECT) is False

    views = {m.id: m for m in await service.list_members(DEFAULT_ORGANISATION_ID)}
    assert views[view.id].status is MemberStatus.ACTIVE
    assert views[view.id].linked_at == now


async def test_principal_and_attached_identity_after_a_link(service) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Principal")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED

    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is True
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, OTHER_SUBJECT) is False
    principal = await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT)
    assert principal is not None
    assert principal.member_id == view.id
    assert principal.organisation_id == DEFAULT_ORGANISATION_ID
    assert principal.role is Role.MEMBER
    assert await service.principal_for_identity(TELEGRAM_PROVIDER, OTHER_SUBJECT) is None
    assert await service.principal_for_identity("other", SUBJECT) is None


async def test_a_detached_identity_names_no_principal_even_for_an_active_member(service, db_session) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Detached")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    await db_session.execute(
        update(UserIdentity)
        .where(UserIdentity.subject == SUBJECT)
        .values(detached_at=datetime.now(timezone.utc))
    )
    assert (await _member_row(db_session, view.id)).status == MemberStatus.ACTIVE.value
    assert await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT) is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False


async def test_principal_for_identity_requires_an_active_organisation(service, db_session) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Suspended later")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    await db_session.execute(
        update(Organisation)
        .where(Organisation.id == DEFAULT_ORGANISATION_ID)
        .values(status=OrganisationStatus.SUSPENDED.value)
    )
    assert await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT) is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is True


async def test_redeem_refuses_an_expired_code(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    issued_at = datetime.now(timezone.utc) - timedelta(hours=1)
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Late", now=issued_at)
    at_expiry = issued.expires_at
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT, now=at_expiry) is RedeemResult.INVALID
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-2:] == ["expired", "expired"]
    row = await _code_row(db_session, issued.code)
    assert row.consumed_at is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False
    assert (await _member_row(db_session, view.id)).status == MemberStatus.INVITED.value

    # One second before expiry still links: the boundary is `expires_at > now`.
    _, fresh = await service.invite(DEFAULT_ORGANISATION_ID, "On time", now=issued_at)
    just_before = fresh.expires_at - timedelta(seconds=1)
    assert await service.redeem_link_code(fresh.code, TELEGRAM_PROVIDER, SUBJECT, now=just_before) is RedeemResult.LINKED


async def test_redeem_refuses_an_invalidated_code(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    view, first = await service.invite(DEFAULT_ORGANISATION_ID, "Twice issued")
    second = await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)
    assert await service.redeem_link_code(first.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "invalidated"
    assert (await _code_row(db_session, first.code)).consumed_at is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False
    # The current code still works.
    assert await service.redeem_link_code(second.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED


async def test_redeem_refuses_unknown_and_malformed_codes(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Unknown")
    unknown = "ABCDEFGHJKMNPQRS" if issued.code != "ABCDEFGHJKMNPQRS" else "SRQPNMKJHGFEDCBA"
    assert await service.redeem_link_code(unknown, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "unknown"
    for malformed in ("", "ABC", issued.code + "A", issued.code[:-1] + "0", issued.code[:-1] + "I"):
        assert await service.redeem_link_code(malformed, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
        assert _reasons(caplog)[-1] == "malformed"
    assert (await _code_row(db_session, issued.code)).consumed_at is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False


async def test_redeem_refuses_a_subject_attached_to_another_member(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    first, first_code = await service.invite(DEFAULT_ORGANISATION_ID, "First")
    second, second_code = await service.invite(DEFAULT_ORGANISATION_ID, "Second")
    assert await service.redeem_link_code(first_code.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED

    assert await service.redeem_link_code(second_code.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "identity_in_use"
    row = await _code_row(db_session, second_code.code)
    assert row.consumed_at is None and row.invalidated_at is None
    second_member = await _member_row(db_session, second.id)
    assert second_member.status == MemberStatus.INVITED.value
    assert await _identities(db_session, second_member.user_id) == []
    # The first link survived the rolled-back savepoint.
    principal = await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT)
    assert principal is not None and principal.member_id == first.id

    # The session is still usable and the unconsumed code still links elsewhere.
    assert await service.redeem_link_code(second_code.code, TELEGRAM_PROVIDER, OTHER_SUBJECT) is RedeemResult.LINKED


async def test_redeem_refuses_a_second_identity_for_a_linked_member(service, db_session) -> None:
    view, first = await service.invite(DEFAULT_ORGANISATION_ID, "Already linked")
    assert await service.redeem_link_code(first.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    second = await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)
    assert await service.redeem_link_code(second.code, TELEGRAM_PROVIDER, OTHER_SUBJECT) is RedeemResult.INVALID
    assert (await _code_row(db_session, second.code)).consumed_at is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, OTHER_SUBJECT) is False


async def test_redeem_refuses_a_revoked_member(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Revoked behind the code's back")
    # Revoke the member *without* closing its code, so only the member check
    # can refuse the redemption.
    await db_session.execute(
        update(OrganisationMember)
        .where(OrganisationMember.id == view.id)
        .values(status=MemberStatus.REVOKED.value, revoked_at=datetime.now(timezone.utc))
    )
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "member_status"
    row = await _code_row(db_session, issued.code)
    assert row.consumed_at is None and row.invalidated_at is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False


async def test_redeem_refuses_a_code_of_a_suspended_organisation(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Suspended org")
    await db_session.execute(
        update(Organisation)
        .where(Organisation.id == DEFAULT_ORGANISATION_ID)
        .values(status=OrganisationStatus.SUSPENDED.value)
    )
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "organisation_unavailable"
    assert (await _code_row(db_session, issued.code)).consumed_at is None


async def test_redeem_refuses_a_code_of_an_admin_member(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Promoted")
    await db_session.execute(
        update(OrganisationMember)
        .where(OrganisationMember.id == view.id)
        .values(role=Role.ADMIN.value)
    )
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "not_member_role"
    assert (await _code_row(db_session, issued.code)).consumed_at is None


async def test_redeem_refuses_the_admin_subject(db_session, svc_settings, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    configured = svc_settings.model_copy(update={"TELEGRAM_ALLOWED_CHAT_ID": ADMIN_CHAT})
    service = MembershipService(db_session, configured)
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Not the admin")

    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, ADMIN_CHAT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "admin_subject"
    assert (await _code_row(db_session, issued.code)).consumed_at is None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, ADMIN_CHAT) is False

    # Only that subject is refused: the same code links another account.
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED


@pytest.mark.parametrize("chat_id", ["", "not-a-number", "0", "-100123"])
async def test_an_unusable_admin_chat_setting_refuses_nothing(db_session, svc_settings, chat_id) -> None:
    configured = svc_settings.model_copy(update={"TELEGRAM_ALLOWED_CHAT_ID": chat_id})
    service = MembershipService(db_session, configured)
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Plain")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED


@pytest.mark.parametrize("subject", ["0", "-5", "abc", "", "012345", "1" * 21, " 123", "123\n", "１２３"])
async def test_redeem_refuses_a_bad_subject(service, db_session, caplog, subject: str) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Bad subject")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, subject) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "subject"
    assert (await _code_row(db_session, issued.code)).consumed_at is None
    assert (await db_session.execute(select(UserIdentity.id))).first() is None


async def test_redeem_refuses_another_provider(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Wrong provider")
    assert await service.redeem_link_code(issued.code, "email", SUBJECT) is RedeemResult.INVALID
    assert _reasons(caplog)[-1] == "provider"
    assert (await _code_row(db_session, issued.code)).consumed_at is None


async def test_a_twenty_digit_subject_is_accepted(service) -> None:
    _, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Long id")
    subject = "9" * 20
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, subject) is RedeemResult.LINKED
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, subject) is True


# --- mark_revoked ------------------------------------------------------------------


async def test_mark_revoked_revokes_closes_codes_and_detaches(service, db_session) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "To revoke")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    open_code = await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)

    now = datetime.now(timezone.utc)
    outcome = await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id, reason="left the team", now=now)
    assert outcome is RevokeOutcome.REVOKED

    member = await _member_row(db_session, view.id)
    assert member.status == MemberStatus.REVOKED.value
    assert _aware(member.revoked_at) == now
    assert member.revoked_reason == "left the team"
    open_row = await _code_row(db_session, open_code.code)
    assert _aware(open_row.invalidated_at) == now
    assert open_row.consumed_at is None
    # The consumed code stays consumed, not invalidated.
    used_row = await _code_row(db_session, issued.code)
    assert used_row.consumed_at is not None and used_row.invalidated_at is None
    identities = await _identities(db_session, member.user_id)
    assert len(identities) == 1
    assert _aware(identities[0].detached_at) == now
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False
    assert await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT) is None
    assert await service.redeem_link_code(open_code.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID

    views = {m.id: m for m in await service.list_members(DEFAULT_ORGANISATION_ID)}
    assert views[view.id].status is MemberStatus.REVOKED
    assert views[view.id].revoked_at == now
    assert views[view.id].linked_at is None

    later = now + timedelta(minutes=5)
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id, now=later) is RevokeOutcome.ALREADY_REVOKED
    member = await _member_row(db_session, view.id)
    assert _aware(member.revoked_at) == now
    assert member.revoked_reason == "left the team"


async def test_mark_revoked_default_and_truncated_reason(service, db_session) -> None:
    first, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Default reason")
    second, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Long reason")
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, first.id) is RevokeOutcome.REVOKED
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, second.id, reason="r" * 100) is RevokeOutcome.REVOKED
    assert (await _member_row(db_session, first.id)).revoked_reason == "admin_revoked"
    assert (await _member_row(db_session, second.id)).revoked_reason == "r" * 64


async def test_already_revoked_still_closes_left_open_codes_and_identities(service, db_session) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Half revoked")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    open_code = await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)
    # A revocation that set the status but closed nothing else.
    await db_session.execute(
        update(OrganisationMember)
        .where(OrganisationMember.id == view.id)
        .values(status=MemberStatus.REVOKED.value, revoked_at=datetime.now(timezone.utc))
    )
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id) is RevokeOutcome.ALREADY_REVOKED
    assert (await _code_row(db_session, open_code.code)).invalidated_at is not None
    assert await service.has_attached_identity(TELEGRAM_PROVIDER, SUBJECT) is False


async def test_mark_revoked_cannot_revoke_the_admin(service, db_session) -> None:
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID) is RevokeOutcome.CANNOT_REVOKE_ADMIN
    admin = await _member_row(db_session, LEGACY_ADMIN_MEMBER_ID)
    assert admin.status == MemberStatus.ACTIVE.value
    assert admin.revoked_at is None
    assert await service.active_principal(LEGACY_ADMIN_MEMBER_ID) is not None


async def test_mark_revoked_cannot_revoke_any_admin_role(service, db_session) -> None:
    view, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Second admin")
    await db_session.execute(
        update(OrganisationMember).where(OrganisationMember.id == view.id).values(role=Role.ADMIN.value)
    )
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id) is RevokeOutcome.CANNOT_REVOKE_ADMIN
    assert (await _member_row(db_session, view.id)).status == MemberStatus.INVITED.value


async def test_mark_revoked_not_found_for_unknown_or_foreign_member(service, db_session) -> None:
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, uuid.uuid4()) is RevokeOutcome.NOT_FOUND
    _, other_member = await _other_organisation(db_session)
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, other_member) is RevokeOutcome.NOT_FOUND
    member = await _member_row(db_session, other_member)
    assert member.status == MemberStatus.ACTIVE.value
    assert member.revoked_at is None


async def test_after_revocation_the_subject_can_link_a_new_member(service, db_session) -> None:
    old, old_code = await service.invite(DEFAULT_ORGANISATION_ID, "Old membership")
    assert await service.redeem_link_code(old_code.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, old.id) is RevokeOutcome.REVOKED

    new, new_code = await service.invite(DEFAULT_ORGANISATION_ID, "New membership")
    assert await service.redeem_link_code(new_code.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    principal = await service.principal_for_identity(TELEGRAM_PROVIDER, SUBJECT)
    assert principal is not None and principal.member_id == new.id

    rows = (
        await db_session.execute(
            select(UserIdentity).where(
                UserIdentity.provider == TELEGRAM_PROVIDER, UserIdentity.subject == SUBJECT
            )
        )
    ).scalars().all()
    assert len(rows) == 2
    assert sum(1 for row in rows if row.detached_at is None) == 1


# --- list_members ----------------------------------------------------------------


async def test_list_members_is_scoped_ordered_and_clamped(service, db_session) -> None:
    base = datetime.now(timezone.utc)
    ids = []
    for index in range(3):
        view, _ = await service.invite(DEFAULT_ORGANISATION_ID, f"Member {index}", now=base + timedelta(seconds=index))
        ids.append(view.id)
    await _other_organisation(db_session)

    members = await service.list_members(DEFAULT_ORGANISATION_ID)
    assert [m.id for m in members] == [LEGACY_ADMIN_MEMBER_ID] + ids
    assert [m.display_name for m in members[1:]] == ["Member 0", "Member 1", "Member 2"]
    assert len(await service.list_members(DEFAULT_ORGANISATION_ID, limit=0)) == 1
    assert len(await service.list_members(DEFAULT_ORGANISATION_ID, limit=-5)) == 1
    assert len(await service.list_members(DEFAULT_ORGANISATION_ID, limit=2)) == 2
    assert len(await service.list_members(DEFAULT_ORGANISATION_ID, limit=10_000)) == 4
    assert await service.list_members(uuid.uuid4()) == []


# --- get_member ------------------------------------------------------------------


async def test_get_member_found_then_linked(service) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Fetched")
    # Another member first, so a missing id filter cannot pass by accident.
    await service.invite(DEFAULT_ORGANISATION_ID, "Neighbour")

    found = await service.get_member(DEFAULT_ORGANISATION_ID, view.id)
    assert found is not None
    assert found.id == view.id
    assert found.display_name == "Fetched"
    assert found.role is Role.MEMBER
    assert found.status is MemberStatus.INVITED
    assert found.linked_at is None and found.revoked_at is None
    assert found.created_at == view.created_at

    now = datetime.now(timezone.utc)
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT, now=now) is RedeemResult.LINKED
    linked = await service.get_member(DEFAULT_ORGANISATION_ID, view.id)
    assert linked is not None
    assert linked.status is MemberStatus.ACTIVE
    assert linked.linked_at == now


async def test_get_member_returns_the_seeded_admin(service) -> None:
    admin = await service.get_member(DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID)
    assert admin is not None
    assert admin.role is Role.ADMIN and admin.status is MemberStatus.ACTIVE


async def test_get_member_none_for_an_unknown_id(service) -> None:
    await service.invite(DEFAULT_ORGANISATION_ID, "Someone")
    assert await service.get_member(DEFAULT_ORGANISATION_ID, uuid.uuid4()) is None


async def test_get_member_none_for_another_organisations_member(service, db_session) -> None:
    other_org, other_member = await _other_organisation(db_session)
    assert await service.get_member(DEFAULT_ORGANISATION_ID, other_member) is None
    assert await service.get_member(other_org, LEGACY_ADMIN_MEMBER_ID) is None
    # The member is real: in its own organisation it is found.
    found = await service.get_member(other_org, other_member)
    assert found is not None and found.id == other_member


async def test_get_member_returns_a_revoked_member(service) -> None:
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Gone")
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    now = datetime.now(timezone.utc)
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id, now=now) is RevokeOutcome.REVOKED

    revoked = await service.get_member(DEFAULT_ORGANISATION_ID, view.id)
    assert revoked is not None
    assert revoked.status is MemberStatus.REVOKED
    assert revoked.revoked_at == now
    # Its identity is detached, so it reads as unlinked.
    assert revoked.linked_at is None


async def test_get_member_equals_the_list_members_entry(service) -> None:
    linked, linked_code = await service.invite(DEFAULT_ORGANISATION_ID, "Linked one")
    plain, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Plain one")
    gone, gone_code = await service.invite(DEFAULT_ORGANISATION_ID, "Gone one")
    assert await service.redeem_link_code(linked_code.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    assert await service.redeem_link_code(gone_code.code, TELEGRAM_PROVIDER, OTHER_SUBJECT) is RedeemResult.LINKED
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, gone.id) is RevokeOutcome.REVOKED

    listed = {m.id: m for m in await service.list_members(DEFAULT_ORGANISATION_ID)}
    for member_id in (LEGACY_ADMIN_MEMBER_ID, linked.id, plain.id, gone.id):
        assert await service.get_member(DEFAULT_ORGANISATION_ID, member_id) == listed[member_id]
    assert listed[linked.id].linked_at is not None


# --- Logs ------------------------------------------------------------------------


async def test_logs_never_carry_the_code_its_hash_or_the_subject(service, db_session, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="app")
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Logged person")
    reissued = await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)
    # A refusal of each kind that sees the code or the subject.
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert await service.redeem_link_code(reissued.code, TELEGRAM_PROVIDER, "-" + SUBJECT) is RedeemResult.INVALID
    assert await service.redeem_link_code(reissued.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED
    _, other = await service.invite(DEFAULT_ORGANISATION_ID, "Collider")
    assert await service.redeem_link_code(other.code, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
    assert await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id) is RevokeOutcome.REVOKED

    forbidden = {
        issued.code, issued.code.lower(), code_sha256(issued.code),
        reissued.code, reissued.code.lower(), code_sha256(reissued.code),
        other.code, code_sha256(other.code),
        SUBJECT, "Logged person", "Collider",
    }
    standard = set(vars(logging.makeLogRecord({})))
    records = [r for r in caplog.records if r.name == SERVICE_LOGGER]
    assert len(records) >= 8
    assert {"invalidated", "subject", "identity_in_use"} <= set(_reasons(caplog))
    for record in records:
        assert record.exc_info is None
        texts = [record.getMessage()]
        texts.extend(str(value) for key, value in vars(record).items() if key not in standard)
        for text in texts:
            for secret in forbidden:
                assert secret not in text, (record.getMessage(), secret)


# --- Display-name categories ----------------------------------------------------


def test_the_refused_name_samples_have_the_categories_they_claim() -> None:
    import unicodedata

    assert unicodedata.category("\ud800") == "Cs"
    assert unicodedata.category("") == "Co"
    assert unicodedata.category("͸") == "Cn"


# --- Non-ASCII codes never alias ----------------------------------------------------


async def test_a_non_ascii_lookalike_never_redeems_a_real_code(service, db_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger=SERVICE_LOGGER)
    view, _ = await service.invite(DEFAULT_ORGANISATION_ID, "Lookalike")
    known = "ABCDEFGHJKMNPQRS"
    now = datetime.now(timezone.utc)
    db_session.add(
        LinkCode(
            member_id=view.id,
            code_sha256=code_sha256(known),
            created_at=now,
            expires_at=now + timedelta(seconds=LINK_CODE_TTL_SECONDS),
        )
    )
    await db_session.flush()

    for lookalike in ("ABCDEFGHJKMNPQRſ", "abcdefghjkmnpqrſ", "ABCDEFGHJKMNPQß"):
        assert await service.redeem_link_code(lookalike, TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.INVALID
        assert _reasons(caplog)[-1] == "malformed"
    assert (await _code_row(db_session, known)).consumed_at is None
    assert await service.redeem_link_code(known.lower(), TELEGRAM_PROVIDER, SUBJECT) is RedeemResult.LINKED


# --- `now` is aware UTC ---------------------------------------------------------------


async def test_an_offset_now_gives_the_same_instant_as_utc(service, db_session) -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 10, 20, 18, 0, 0, tzinfo=india)
    as_utc = local.astimezone(timezone.utc)

    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Offset", now=local)
    assert issued.expires_at == as_utc + timedelta(seconds=LINK_CODE_TTL_SECONDS)
    assert issued.expires_at.utcoffset() == timedelta(0)
    assert view.created_at.utcoffset() == timedelta(0)
    row = await _code_row(db_session, issued.code)
    # SQLite keeps the wall-clock text: it must be the UTC wall clock.
    assert _aware(row.expires_at) == as_utc + timedelta(seconds=LINK_CODE_TTL_SECONDS)
    assert _aware(row.created_at) == as_utc

    just_before = (issued.expires_at - timedelta(seconds=1)).astimezone(india)
    assert await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT, now=just_before) is RedeemResult.LINKED
    member = await _member_row(db_session, view.id)
    assert _aware(member.activated_at) == just_before


async def test_a_naive_now_is_refused(service, db_session) -> None:
    naive = datetime(2026, 10, 20, 12, 0, 0)
    with pytest.raises(ValueError):
        await service.invite(DEFAULT_ORGANISATION_ID, "Naive", now=naive)
    view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Aware")
    with pytest.raises(ValueError):
        await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id, now=naive)
    with pytest.raises(ValueError):
        await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT, now=naive)
    with pytest.raises(ValueError):
        await service.mark_revoked(DEFAULT_ORGANISATION_ID, view.id, now=naive)
    assert (await _code_row(db_session, issued.code)).consumed_at is None
    assert (await _member_row(db_session, view.id)).status == MemberStatus.INVITED.value
    names = (await db_session.execute(select(User.display_name))).scalars().all()
    assert "Naive" not in names


# --- The service never commits --------------------------------------------------------


def test_the_service_never_commits() -> None:
    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    assert calls == []
    # The walk is not vacuous: the module does call methods on its session.
    flushes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "flush"
    ]
    assert flushes


async def _counts(session_factory):
    async with session_factory() as session:
        members = (await session.execute(select(func.count()).select_from(OrganisationMember))).scalar()
        users = (await session.execute(select(func.count()).select_from(User))).scalar()
        codes = (await session.execute(select(func.count()).select_from(LinkCode))).scalar()
        identities = (await session.execute(select(func.count()).select_from(UserIdentity))).scalar()
        await session.rollback()
    return members, users, codes, identities


async def test_rolling_back_after_each_call_leaves_nothing(session_factory, svc_settings) -> None:
    # Separate sessions, used one after another: they share one connection.
    assert await _counts(session_factory) == (1, 1, 0, 0)

    async with session_factory() as session:
        await MembershipService(session, svc_settings).invite(DEFAULT_ORGANISATION_ID, "Rolled back")
        await session.rollback()
    assert await _counts(session_factory) == (1, 1, 0, 0)

    async with session_factory() as session:
        view, issued = await MembershipService(session, svc_settings).invite(DEFAULT_ORGANISATION_ID, "Kept")
        await session.commit()
    assert await _counts(session_factory) == (2, 2, 1, 0)

    async with session_factory() as session:
        result = await MembershipService(session, svc_settings).redeem_link_code(
            issued.code, TELEGRAM_PROVIDER, SUBJECT
        )
        assert result is RedeemResult.LINKED
        await session.rollback()
    assert await _counts(session_factory) == (2, 2, 1, 0)

    async with session_factory() as session:
        outcome = await MembershipService(session, svc_settings).mark_revoked(DEFAULT_ORGANISATION_ID, view.id)
        assert outcome is RevokeOutcome.REVOKED
        await session.rollback()
    assert await _counts(session_factory) == (2, 2, 1, 0)

    async with session_factory() as session:
        member = await _member_row(session, view.id)
        assert member.status == MemberStatus.INVITED.value
        assert member.activated_at is None and member.revoked_at is None
        row = await _code_row(session, issued.code)
        assert row.consumed_at is None and row.invalidated_at is None
        await session.rollback()


# --- A revocation that loses a race is not reported as done ---------------------------------


class _ConcurrentRevoker:
    """The real session, except that just before the service's revoking
    UPDATE runs, another writer revokes the member first. Unreachable while
    the member lock holds; this forces the branch to prove its guard."""

    def __init__(self, session, member_id: uuid.UUID) -> None:
        self._session = session
        self._member_id = member_id
        self.raced = False

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def execute(self, statement, *args, **kwargs):
        table = getattr(statement, "table", None)
        if (
            not self.raced
            and getattr(statement, "is_update", False)
            and getattr(table, "name", None) == "organisation_members"
        ):
            self.raced = True
            await self._session.execute(
                update(OrganisationMember)
                .where(OrganisationMember.id == self._member_id)
                .values(
                    status=MemberStatus.REVOKED.value,
                    revoked_at=datetime.now(timezone.utc),
                    revoked_reason="concurrent",
                )
            )
        return await self._session.execute(statement, *args, **kwargs)


async def test_a_revocation_that_matches_nothing_raises(db_session, svc_settings) -> None:
    view, _ = await MembershipService(db_session, svc_settings).invite(DEFAULT_ORGANISATION_ID, "Raced")
    racing = _ConcurrentRevoker(db_session, view.id)
    with pytest.raises(service_module._RevocationRaced):
        await MembershipService(racing, svc_settings).mark_revoked(DEFAULT_ORGANISATION_ID, view.id)
    assert racing.raced is True
    assert (await _member_row(db_session, view.id)).revoked_reason == "concurrent"


# --- The caller's pending errors are not swallowed ---------------------------------------


async def test_a_callers_pending_violation_is_raised_not_reported_invalid(session_factory, svc_settings) -> None:
    async with session_factory() as session:
        _, issued = await MembershipService(session, svc_settings).invite(DEFAULT_ORGANISATION_ID, "Pending")
        await session.commit()

    engine = session_factory.kw["bind"]
    async with AsyncSession(bind=engine, autoflush=False, expire_on_commit=False) as session:
        assert session.autoflush is False
        # The caller's own pending work violates NOT NULL. Nothing has flushed
        # it yet: autoflush is off, and the service's reads do not flush.
        session.add(User(display_name=None))
        service = MembershipService(session, svc_settings)
        with pytest.raises(IntegrityError):
            await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT)
        await session.rollback()

    async with session_factory() as session:
        row = await _code_row(session, issued.code)
        assert row.consumed_at is None and row.invalidated_at is None
        assert (await session.execute(select(UserIdentity.id))).first() is None
        await session.rollback()
