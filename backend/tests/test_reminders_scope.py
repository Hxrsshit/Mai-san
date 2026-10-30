"""Stage 7B unit 8: reminders belong to the member who set them.

A reminder carries its creator's knowledge scope. Another member, a member of
another organisation, and an unattributed (legacy) reminder are invisible to
a scope: listing, reading, cancelling, firing, notifications and marking
them read. The scheduler fires only reminders of active members, re-checking
each member under the unit 7 share lock, and a legacy or revoked member's
reminder never occupies its bounded pass. Rows are synthetic.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from app.identity.constants import DEFAULT_ORGANISATION_ID, MemberStatus, Role
from app.identity.models import OrganisationMember, User
from app.identity.scope import InvalidScope
from app.reminders.models import (
    NotificationState,
    Recurrence,
    Reminder,
    ReminderNotification,
    ReminderState,
)
from app.reminders.scheduler import due_reminders, run_due_reminders
from app.reminders.service import MAX_PER_TICK, ReminderService
from app.reminders.schemas import ReminderOutcome
from app.services.conversation_service import ConversationService
from tests.scopes import ADMIN_SCOPE, COLLEAGUE_PRINCIPAL, COLLEAGUE_SCOPE, OUTSIDER_SCOPE

pytestmark = pytest.mark.anyio

OTHERS = [pytest.param(COLLEAGUE_SCOPE, id="colleague"), pytest.param(OUTSIDER_SCOPE, id="outsider")]
PAST = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _reminder(scope=None, text="call the bank", at=PAST) -> Reminder:
    columns = scope.write_columns() if scope is not None else {}
    return Reminder(text=text, state=ReminderState.SCHEDULED, recurrence=Recurrence.ONCE,
                    next_run_at=at, timezone_name="UTC", **columns)


async def _member(session_factory, scope_principal, status=MemberStatus.ACTIVE) -> None:
    async with session_factory() as session:
        user = User(display_name="Synthetic member")
        session.add(user)
        await session.flush()
        session.add(OrganisationMember(
            id=scope_principal.member_id, organisation_id=scope_principal.organisation_id,
            user_id=user.id, role=Role.MEMBER.value, status=status.value,
            activated_at=datetime.now(timezone.utc),
            revoked_at=datetime.now(timezone.utc) if status is MemberStatus.REVOKED else None))
        await session.commit()


async def _admin_reminder_with_notification(session_factory):
    async with session_factory() as session:
        reminder = _reminder(ADMIN_SCOPE)
        session.add(reminder)
        await session.flush()
        notification = ReminderNotification(reminder_id=reminder.id, text=reminder.text,
                                            due_at=PAST, state=NotificationState.PENDING)
        session.add(notification)
        await session.commit()
        return reminder.id, notification.id


def test_the_service_refuses_anything_but_a_scope() -> None:
    for bogus in (None, "personal", ADMIN_SCOPE.write_columns(), uuid.uuid4()):
        with pytest.raises(InvalidScope):
            ReminderService(object(), scope=bogus)  # type: ignore[arg-type]


async def test_a_new_reminder_carries_its_creators_scope(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings, scope=ADMIN_SCOPE)
    result = await service.create(service.read_request("remind me in 2 hours to call the bank"))
    assert result.outcome is ReminderOutcome.CREATED
    reminder = await service.get(result.reminder_id)
    assert ADMIN_SCOPE.owns(reminder)


async def test_another_scopes_conversation_is_never_linked(db_session, settings) -> None:
    theirs = await ConversationService(db_session, COLLEAGUE_SCOPE).create_conversation()
    mine = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    service = ReminderService(db_session, settings=settings, scope=ADMIN_SCOPE)
    parsed = service.read_request("remind me in 2 hours to call the bank")
    linked = await service.get((await service.create(parsed, conversation_id=mine.id)).reminder_id)
    assert linked.conversation_id == mine.id
    unlinked = await service.get((await service.create(parsed, conversation_id=theirs.id)).reminder_id)
    assert unlinked.conversation_id is None and ADMIN_SCOPE.owns(unlinked)


@pytest.mark.parametrize("other", OTHERS)
async def test_another_scope_cannot_see_cancel_fire_or_read_the_reminder(
    session_factory, settings, other
) -> None:
    reminder_id, notification_id = await _admin_reminder_with_notification(session_factory)
    async with session_factory() as session:
        service = ReminderService(session, settings=settings, scope=other)
        assert await service.active() == []
        assert await service.get(reminder_id) is None
        assert (await service.describe_active()).matched == 0
        assert (await service.cancel_matching("bank")).outcome is ReminderOutcome.NOTHING_TO_CANCEL
        assert (await service.cancel_by_id(reminder_id)).reason == "reminder_not_found"
        assert await service.due(now=NOW) == []
        loaded = (await session.execute(select(Reminder).where(Reminder.id == reminder_id))).scalar_one()
        assert await service.fire(loaded) is False
        assert await service.pending_notifications() == ([], 0)
        assert await service.mark_read(notification_id) is False
        await session.commit()
    async with session_factory() as session:          # nothing changed
        row = (await session.execute(select(Reminder).where(Reminder.id == reminder_id))).scalar_one()
        assert row.state is ReminderState.SCHEDULED and row.fire_count == 0
        state = (await session.execute(select(ReminderNotification.state)
                                       .where(ReminderNotification.id == notification_id))).scalar_one()
        assert state is NotificationState.PENDING


async def test_a_legacy_reminder_is_quarantined(db_session, settings) -> None:
    db_session.add(_reminder(None, "legacy"))
    await db_session.flush()
    service = ReminderService(db_session, settings=settings, scope=ADMIN_SCOPE)
    assert await service.active() == [] and await service.due(now=NOW) == []
    assert await due_reminders(db_session, now=NOW) == []


async def test_the_scheduler_fires_only_active_members_reminders(session_factory, settings) -> None:
    await _member(session_factory, COLLEAGUE_PRINCIPAL, MemberStatus.REVOKED)
    async with session_factory() as session:
        admin, colleague, legacy = _reminder(ADMIN_SCOPE, "admin's"), \
            _reminder(COLLEAGUE_SCOPE, "colleague's"), _reminder(None, "legacy")
        session.add_all([admin, colleague, legacy])
        await session.commit()
        ids = {"admin": admin.id, "colleague": colleague.id, "legacy": legacy.id}
    assert await run_due_reminders(session_factory, settings, now=NOW) == 1
    async with session_factory() as session:
        states = dict((await session.execute(select(Reminder.id, Reminder.state))).all())
        assert states[ids["admin"]] is ReminderState.COMPLETED
        assert states[ids["colleague"]] is ReminderState.SCHEDULED      # revoked: not fired
        assert states[ids["legacy"]] is ReminderState.SCHEDULED         # quarantined
        notified = (await session.execute(select(ReminderNotification.reminder_id))).scalars().all()
        assert notified == [ids["admin"]]


async def test_an_active_colleagues_reminder_fires_in_their_own_scope(session_factory, settings) -> None:
    await _member(session_factory, COLLEAGUE_PRINCIPAL)
    async with session_factory() as session:
        reminder = _reminder(COLLEAGUE_SCOPE)
        session.add(reminder)
        await session.commit()
    assert await run_due_reminders(session_factory, settings, now=NOW) == 1
    async with session_factory() as session:
        theirs = ReminderService(session, settings=settings, scope=COLLEAGUE_SCOPE)
        mine = ReminderService(session, settings=settings, scope=ADMIN_SCOPE)
        assert (await theirs.pending_notifications())[1] == 1
        assert (await mine.pending_notifications())[1] == 0


async def test_legacy_and_revoked_reminders_cannot_starve_the_pass(session_factory, settings) -> None:
    """More than a pass's worth of due rows that must never fire, older than
    the one that should: the bounded pass still reaches it."""
    await _member(session_factory, COLLEAGUE_PRINCIPAL, MemberStatus.REVOKED)
    async with session_factory() as session:
        for index in range(MAX_PER_TICK + 2):
            at = PAST - timedelta(minutes=index + 1)
            session.add(_reminder(None, f"legacy {index}", at=at))
            session.add(_reminder(COLLEAGUE_SCOPE, f"revoked {index}", at=at))
        session.add(_reminder(ADMIN_SCOPE, "the admin's", at=PAST))
        await session.commit()
    assert await run_due_reminders(session_factory, settings, now=NOW) == 1


async def test_a_member_revoked_after_discovery_is_not_fired(session_factory, settings, monkeypatch) -> None:
    """The pass found the reminder while the member was active; the member is
    revoked before the fire. The share-lock re-check refuses it."""
    from app.reminders import scheduler

    await _member(session_factory, COLLEAGUE_PRINCIPAL)
    async with session_factory() as session:
        session.add(_reminder(COLLEAGUE_SCOPE, at=PAST - timedelta(minutes=5)))   # first in the pass
        session.add(_reminder(ADMIN_SCOPE, "the admin's", at=PAST))
        await session.commit()

    real = scheduler.lock_active_principal

    async def revoke_then_check(session, member_id):
        if member_id != COLLEAGUE_PRINCIPAL.member_id:
            return await real(session, member_id)
        # The revocation lands between discovery and the re-check. (Written
        # in the pass's own session: the SQLite test harness shares one
        # connection, so a second writer cannot open a transaction here; the
        # cross-connection race is the PostgreSQL test of unit 7.)
        await session.execute(update(OrganisationMember)
                              .where(OrganisationMember.id == member_id)
                              .values(status=MemberStatus.REVOKED.value,
                                      revoked_at=datetime.now(timezone.utc)))
        return await real(session, member_id)

    monkeypatch.setattr(scheduler, "lock_active_principal", revoke_then_check)
    # The revoked member's reminder is skipped -- and only it: the rest of
    # the pass still runs (a refusal must not abort the tick).
    assert await run_due_reminders(session_factory, settings, now=NOW) == 1
    async with session_factory() as session:
        notified = (await session.execute(select(ReminderNotification.text))).scalars().all()
        assert notified == ["the admin's"]


async def test_the_claim_itself_is_scope_bound(db_session, settings) -> None:
    """Below the service's own ownership check: the conditional UPDATE that
    claims an occurrence names the scope, so even a direct claim of a
    foreign reminder changes nothing."""
    reminder = _reminder(COLLEAGUE_SCOPE)
    db_session.add(reminder)
    await db_session.flush()
    service = ReminderService(db_session, settings=settings, scope=ADMIN_SCOPE)
    from app.identity.scope import KnowledgeScope

    original = KnowledgeScope.owns
    KnowledgeScope.owns = lambda self, row: True   # bypass the first check only
    try:
        assert await service.fire(reminder) is False
    finally:
        KnowledgeScope.owns = original
    state = (await db_session.execute(select(Reminder.state, Reminder.fire_count)
                                      .where(Reminder.id == reminder.id))).one()
    assert tuple(state) == (ReminderState.SCHEDULED, 0)


async def test_the_api_never_reaches_a_colleagues_reminder(client: AsyncClient, session_factory) -> None:
    async with session_factory() as session:
        reminder = _reminder(COLLEAGUE_SCOPE, at=NOW + timedelta(days=1))
        session.add(reminder)
        await session.flush()
        notification = ReminderNotification(reminder_id=reminder.id, text="x", due_at=PAST,
                                            state=NotificationState.PENDING)
        session.add(notification)
        await session.commit()
        reminder_id, notification_id = reminder.id, notification.id
    assert (await client.get("/api/reminders")).json()["total"] == 0
    assert (await client.post(f"/api/reminders/{reminder_id}/cancel")).status_code == 404
    assert (await client.get("/api/reminders/notifications")).json()["total"] == 0
    assert (await client.post(f"/api/reminders/notifications/{notification_id}/read")).status_code == 404
    async with session_factory() as session:
        row = (await session.execute(select(Reminder).where(Reminder.id == reminder_id))).scalar_one()
        assert row.state is ReminderState.SCHEDULED


async def test_one_failing_reminder_never_aborts_the_pass(session_factory, settings, monkeypatch) -> None:
    """A membership read that raises for one reminder skips it; the rest of
    the pass still fires (and the failure is a skip, never a fire)."""
    from app.reminders import scheduler

    await _member(session_factory, COLLEAGUE_PRINCIPAL)
    async with session_factory() as session:
        session.add(_reminder(COLLEAGUE_SCOPE, at=PAST - timedelta(minutes=5)))
        session.add(_reminder(ADMIN_SCOPE, "the admin's", at=PAST))
        await session.commit()
    real = scheduler.lock_active_principal

    async def flaky(session, member_id):
        if member_id == COLLEAGUE_PRINCIPAL.member_id:
            raise RuntimeError("membership read failed")
        return await real(session, member_id)

    monkeypatch.setattr(scheduler, "lock_active_principal", flaky)
    assert await run_due_reminders(session_factory, settings, now=NOW) == 1
    async with session_factory() as session:
        assert (await session.execute(select(ReminderNotification.text))).scalars().all() == ["the admin's"]
