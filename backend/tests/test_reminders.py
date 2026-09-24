"""Stage 5F.1: reminder parsing, persistence, and conversational management.

Time is injected everywhere. No test sleeps, and no expectation is derived
from `datetime.now()` -- a reminder test that computes its own answer from the
clock proves only that two calls to the clock agree.

Expected instants are literals wherever the value matters.
"""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.reminders import language
from app.reminders.language import ScheduleProblem, parse
from app.reminders.models import (
    NotificationState,
    Recurrence,
    Reminder,
    ReminderNotification,
    ReminderState,
)
from app.reminders.schemas import ReminderOutcome
from app.reminders.service import ReminderService, as_utc

pytestmark = pytest.mark.anyio

IST = ZoneInfo("Asia/Kolkata")
#: Wednesday 23 September 2026, 15:30 IST. Fixed, so every expectation below
#: is a literal rather than an arithmetic restatement of the implementation.
NOW = datetime(2026, 9, 23, 15, 30, tzinfo=IST)


# --- Parsing: the three supported schedule forms ------------------------------


def test_a_one_time_absolute_schedule() -> None:
    result = parse("Remind me tomorrow at 10 AM to call X.", NOW, "Asia/Kolkata")
    assert result.ok
    assert result.recurrence is Recurrence.ONCE
    assert result.run_at.isoformat() == "2026-09-24T10:00:00+05:30"
    assert result.text == "call X"


def test_a_relative_schedule() -> None:
    result = parse("Remind me in 3 hours to check the deployment.", NOW, "Asia/Kolkata")
    assert result.ok
    assert result.recurrence is Recurrence.ONCE
    assert result.run_at.isoformat() == "2026-09-23T18:30:00+05:30"
    assert result.text == "check the deployment"


def test_a_weekly_schedule() -> None:
    result = parse("Remind me every Sunday to plan my week.", NOW, "Asia/Kolkata")
    assert result.ok
    assert result.recurrence is Recurrence.WEEKLY
    assert result.run_at.isoformat() == "2026-09-27T09:00:00+05:30"
    assert result.text == "plan my week"


def test_a_daily_schedule_with_a_time() -> None:
    result = parse("remind me every day at 9am to stretch", NOW, "Asia/Kolkata")
    assert result.ok
    assert result.recurrence is Recurrence.DAILY
    assert result.run_at.isoformat() == "2026-09-24T09:00:00+05:30"


def test_a_weekday_schedule_with_a_time() -> None:
    result = parse(
        "remind me every monday at 9 AM to file the report", NOW, "Asia/Kolkata"
    )
    assert result.ok
    assert result.recurrence is Recurrence.WEEKLY
    assert result.run_at.isoformat() == "2026-09-28T09:00:00+05:30"


def test_a_bare_clock_time_rolls_to_tomorrow_when_it_has_passed() -> None:
    """At 15:30, "at 9am" is tomorrow morning."""
    result = parse("remind me at 9am to stretch", NOW, "Asia/Kolkata")
    assert result.run_at.isoformat() == "2026-09-24T09:00:00+05:30"


def test_a_bare_clock_time_stays_today_when_it_is_still_ahead() -> None:
    result = parse("remind me at 8pm to call mum", NOW, "Asia/Kolkata")
    assert result.run_at.isoformat() == "2026-09-23T20:00:00+05:30"


# --- Parsing: refusing rather than guessing -------------------------------------


@pytest.mark.parametrize(
    "message, problem",
    [
        ("remind me to buy milk", ScheduleProblem.NO_TIME),
        ("remind me tomorrow", ScheduleProblem.NO_TIME),
        ("remind me later to do the thing", ScheduleProblem.UNSUPPORTED_SCHEDULE),
        ("remind me sometime to call", ScheduleProblem.UNSUPPORTED_SCHEDULE),
        ("remind me next week to review", ScheduleProblem.UNSUPPORTED_SCHEDULE),
        ("remind me every other tuesday to water plants",
         ScheduleProblem.UNSUPPORTED_SCHEDULE),
        ("remind me every month to pay rent", ScheduleProblem.UNSUPPORTED_SCHEDULE),
        ("remind me on weekdays to stand up", ScheduleProblem.UNSUPPORTED_SCHEDULE),
    ],
)
def test_an_unreadable_schedule_is_refused(message, problem) -> None:
    result = parse(message, NOW, "Asia/Kolkata")
    assert not result.ok
    assert result.problem is problem
    assert result.run_at is None


def test_a_time_in_the_past_is_refused() -> None:
    result = parse("remind me in 0 minutes to go", NOW, "Asia/Kolkata")
    assert not result.ok


def test_a_time_too_far_ahead_is_refused() -> None:
    result = parse("remind me in 400 days to renew", NOW, "Asia/Kolkata")
    assert not result.ok
    assert result.problem is ScheduleProblem.TOO_FAR_AHEAD


def test_an_ordinary_message_is_not_a_reminder() -> None:
    for message in ("what is Python?", "tell me about Fable", "search the web"):
        assert parse(message, NOW, "Asia/Kolkata").problem is ScheduleProblem.NOT_A_REMINDER


def test_a_question_about_reminders_is_not_a_request_for_one() -> None:
    assert language.is_list_request("what reminders do I have?")
    assert not language.is_reminder_request("what reminders do I have?")


# --- Timezone -----------------------------------------------------------------


def test_the_schedule_is_read_in_the_users_zone_not_utc() -> None:
    """Asia/Kolkata is +05:30, so a UTC reading would be 5.5 hours out."""
    result = parse("remind me tomorrow at 10 AM to call X", NOW, "Asia/Kolkata")
    assert result.run_at.utcoffset() == timedelta(hours=5, minutes=30)
    # The same instant in UTC, written out as a literal.
    assert result.run_at.astimezone(timezone.utc).isoformat() == (
        "2026-09-24T04:30:00+00:00"
    )


def test_recurrence_advances_the_local_clock_not_the_utc_instant() -> None:
    """"Every day at 9am" means nine on the clock, across a DST change."""
    london = ZoneInfo("Europe/London")
    # 25 October 2026 is the day BST ends in the UK.
    before = datetime(2026, 10, 24, 9, 0, tzinfo=london)
    after = language.next_occurrence(before, Recurrence.DAILY)
    assert after.hour == 9
    assert after.date().isoformat() == "2026-10-25"


def test_a_one_time_reminder_has_no_next_occurrence() -> None:
    assert language.next_occurrence(NOW, Recurrence.ONCE) is None


# --- Persistence ------------------------------------------------------------------


async def test_a_parsed_reminder_persists(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    parsed = parse("remind me in 2 hours to call X", service._now_local(), "Asia/Kolkata")
    result = await service.create(parsed)

    assert result.outcome is ReminderOutcome.CREATED
    assert result.reminder_id is not None

    rows = (await db_session.execute(select(Reminder))).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "call X"
    assert rows[0].state is ReminderState.SCHEDULED
    assert rows[0].recurrence is Recurrence.ONCE


async def test_an_unparsed_request_persists_nothing(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    parsed = parse("remind me to buy milk", service._now_local(), "Asia/Kolkata")
    result = await service.create(parsed)

    assert result.outcome is ReminderOutcome.NEEDS_CLARIFICATION
    assert (await db_session.execute(select(Reminder))).scalars().all() == []
    # And it must not claim to have set anything.
    assert "I'll remind you" not in result.reply


# --- The scheduler ------------------------------------------------------------------


async def seed(session, settings, *, text="ping", when, recurrence=Recurrence.ONCE,
               state=ReminderState.SCHEDULED):
    reminder = Reminder(
        text=text, state=state, recurrence=recurrence,
        next_run_at=when, timezone_name="Asia/Kolkata",
    )
    session.add(reminder)
    await session.flush()
    return reminder


async def test_the_scheduler_finds_a_due_reminder(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, settings, when=due_at)

    # Just before due: nothing.
    assert await service.due(now=due_at - timedelta(seconds=1)) == []
    # Exactly due, and just after: found.
    assert len(await service.due(now=due_at)) == 1
    assert len(await service.due(now=due_at + timedelta(seconds=1))) == 1


async def test_a_one_time_reminder_fires_once_and_completes(
    db_session, settings
) -> None:
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder = await seed(db_session, settings, when=due_at)

    assert await service.fire(reminder) is True

    await db_session.refresh(reminder)
    assert reminder.state is ReminderState.COMPLETED
    assert reminder.fire_count == 1

    notifications = (
        await db_session.execute(select(ReminderNotification))
    ).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].text == "ping"
    assert notifications[0].state is NotificationState.PENDING


async def test_a_second_claim_of_the_same_occurrence_does_nothing(
    db_session, settings
) -> None:
    """The atomicity guarantee, exercised directly."""
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder = await seed(db_session, settings, when=due_at)

    assert await service.fire(reminder) is True
    # A second pass still holding the stale row must not deliver again.
    assert await service.fire(reminder) is False

    notifications = (
        await db_session.execute(select(ReminderNotification))
    ).scalars().all()
    assert len(notifications) == 1, "the occurrence was delivered twice"


async def test_a_recurring_reminder_reschedules_itself(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)  # 09:00 IST
    reminder = await seed(
        db_session, settings, when=due_at, recurrence=Recurrence.DAILY
    )

    assert await service.fire(reminder) is True
    await db_session.refresh(reminder)

    assert reminder.state is ReminderState.SCHEDULED, "a daily reminder must survive"
    assert reminder.fire_count == 1
    # Read the way the application reads it: SQLite hands back a naive value,
    # so asserting on the raw column would test the driver, not the schedule.
    advanced = as_utc(reminder.next_run_at).astimezone(IST)
    # Advanced by exactly one local day, so 09:00 IST stays 09:00 IST.
    assert advanced.isoformat() == "2026-09-24T09:00:00+05:30"


async def test_a_recurring_reminder_does_not_duplicate_its_next_occurrence(
    db_session, settings
) -> None:
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)
    reminder = await seed(
        db_session, settings, when=due_at, recurrence=Recurrence.DAILY
    )

    await service.fire(reminder)

    # A second scheduler pass that read the row *before* the advance still
    # holds the old occurrence. Detached, so mutating the copy cannot flush
    # the stale value back and quietly satisfy the guard under test.
    db_session.expunge(reminder)
    reminder.next_run_at = due_at
    assert await service.fire(reminder) is False

    current = (await db_session.execute(select(Reminder))).scalars().one()
    assert current.fire_count == 1, "the occurrence was claimed twice"
    assert len(
        (await db_session.execute(select(ReminderNotification))).scalars().all()
    ) == 1


async def test_a_cancelled_reminder_never_fires(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder = await seed(
        db_session, settings, when=due_at, state=ReminderState.SCHEDULED
    )
    await service._cancel(reminder)
    await db_session.refresh(reminder)

    assert reminder.state is ReminderState.CANCELLED
    assert await service.due(now=due_at + timedelta(hours=1)) == []
    assert await service.fire(reminder) is False
    assert (await db_session.execute(select(ReminderNotification))).scalars().all() == []


async def test_reminders_survive_a_restart(db_session, settings) -> None:
    """Nothing is held in memory: a new service sees the same row."""
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, settings, when=due_at, text="after restart")
    await db_session.commit()

    fresh = ReminderService(db_session, settings=settings)
    found = await fresh.due(now=due_at)
    assert len(found) == 1
    assert found[0].text == "after restart"


# --- Management -----------------------------------------------------------------------


async def test_listing_reminders(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    base = datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc)
    await seed(db_session, settings, when=base, text="call the dentist")
    await seed(db_session, settings, when=base + timedelta(hours=1), text="pay rent")

    result = await service.describe_active()
    assert result.outcome is ReminderOutcome.LISTED
    assert result.matched == 2
    assert "call the dentist" in result.reply
    assert "pay rent" in result.reply


async def test_listing_when_there_are_none(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    result = await service.describe_active()
    assert result.matched == 0
    assert "no reminders" in result.reply.lower()


async def test_cancelling_by_phrase(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    base = datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc)
    await seed(db_session, settings, when=base, text="check the deployment")
    await seed(db_session, settings, when=base, text="pay rent")

    result = await service.cancel_matching("deployment")
    assert result.outcome is ReminderOutcome.CANCELLED
    assert result.matched == 1

    remaining = await service.active()
    assert [r.text for r in remaining] == ["pay rent"]


async def test_an_ambiguous_cancellation_cancels_nothing(db_session, settings) -> None:
    """§: do not allow vague cancellation to delete multiple reminders."""
    service = ReminderService(db_session, settings=settings)
    base = datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc)
    await seed(db_session, settings, when=base, text="review the deployment plan")
    await seed(db_session, settings, when=base, text="check the deployment")

    result = await service.cancel_matching("deployment")
    assert result.outcome is ReminderOutcome.AMBIGUOUS_CANCEL
    assert result.matched == 2
    assert len(await service.active()) == 2, "an ambiguous phrase cancelled something"


async def test_a_bare_cancellation_with_several_reminders_asks(
    db_session, settings
) -> None:
    service = ReminderService(db_session, settings=settings)
    base = datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc)
    await seed(db_session, settings, when=base, text="one")
    await seed(db_session, settings, when=base, text="two")

    result = await service.cancel_matching("")
    assert result.outcome is ReminderOutcome.AMBIGUOUS_CANCEL
    assert len(await service.active()) == 2


async def test_a_bare_cancellation_with_one_reminder_is_unambiguous(
    db_session, settings
) -> None:
    service = ReminderService(db_session, settings=settings)
    await seed(
        db_session, settings,
        when=datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc), text="only one",
    )
    result = await service.cancel_matching("")
    assert result.outcome is ReminderOutcome.CANCELLED
    assert await service.active() == []


async def test_cancelling_something_that_does_not_exist(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    await seed(
        db_session, settings,
        when=datetime(2026, 9, 24, 4, 30, tzinfo=timezone.utc), text="pay rent",
    )
    result = await service.cancel_matching("dentist")
    assert result.outcome is ReminderOutcome.NOTHING_TO_CANCEL
    assert len(await service.active()) == 1


# --- Notifications -----------------------------------------------------------------------


async def test_a_fired_reminder_produces_a_pending_notification(
    db_session, settings
) -> None:
    service = ReminderService(db_session, settings=settings)
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder = await seed(db_session, settings, when=due_at, text="stand up")
    await service.fire(reminder)

    pending, total = await service.pending_notifications()
    assert total == 1
    assert pending[0].text == "stand up"


async def test_a_notification_can_be_marked_read(db_session, settings) -> None:
    service = ReminderService(db_session, settings=settings)
    reminder = await seed(
        db_session, settings, when=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    )
    await service.fire(reminder)
    pending, _ = await service.pending_notifications()

    assert await service.mark_read(pending[0].id) is True
    assert (await service.pending_notifications())[1] == 0
    # Marking twice is not an error the second time; it simply matches nothing.
    assert await service.mark_read(pending[0].id) is False


async def test_a_notification_outlives_its_reminder_text(db_session, settings) -> None:
    """The text is copied, not joined, so the record still reads correctly."""
    service = ReminderService(db_session, settings=settings)
    reminder = await seed(
        db_session, settings,
        when=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc), text="original text",
    )
    await service.fire(reminder)
    reminder.text = "changed afterwards"
    await db_session.flush()

    pending, _ = await service.pending_notifications()
    assert pending[0].text == "original text"


# --- Gaps found by mutation testing -------------------------------------------------
#
# Each test below exists because a mutation survived the suite above. The
# common thread is that the earlier tests exercised `parse` directly and the
# service only through a UTC configuration -- so the two places the timezone
# is actually applied were never observed, and neither was the persisted
# instant. A reminder subsystem whose timezone handling is untested is the one
# thing this stage could not ship.


@pytest.fixture
def ist_settings(settings):
    """Settings in a zone that is not UTC, and not a whole number of hours.

    Asia/Kolkata is +05:30. A half-hour offset catches the class of bug that
    a whole-hour zone hides, where a wrong-but-plausible conversion still
    lands on the correct minute.
    """
    return settings.model_copy(update={"MAI_TIMEZONE": "Asia/Kolkata"})


def test_the_configured_zone_is_the_one_used(ist_settings, db_session) -> None:
    """M5: the service read `MAI_TIMEZONE` and nothing checked which zone."""
    service = ReminderService(db_session, settings=ist_settings)
    assert str(service.timezone()) == "Asia/Kolkata"


def test_a_request_is_read_in_the_configured_zone(ist_settings, db_session) -> None:
    service = ReminderService(db_session, settings=ist_settings)
    parsed = service.read_request("remind me tomorrow at 10 AM to call X")

    assert parsed.ok
    assert parsed.timezone_name == "Asia/Kolkata"
    # 10:00 on the user's clock, which is 04:30 UTC. Read as UTC it would be
    # 10:00 UTC -- five and a half hours late, and invisible in a UTC test.
    assert parsed.run_at.strftime("%H:%M") == "10:00"
    assert parsed.run_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert parsed.run_at.astimezone(timezone.utc).strftime("%H:%M") == "04:30"


async def test_the_persisted_instant_is_the_parsed_one(
    ist_settings, db_session
) -> None:
    """M9: nothing asserted what `create` actually wrote to the row."""
    service = ReminderService(db_session, settings=ist_settings)
    parsed = parse(
        "remind me tomorrow at 10 AM to call X", NOW, "Asia/Kolkata"
    )
    result = await service.create(parsed)
    assert result.outcome is ReminderOutcome.CREATED

    stored = (await db_session.execute(select(Reminder))).scalars().one()
    # Literal, in UTC, because that is what the column holds.
    assert as_utc(stored.next_run_at).isoformat() == "2026-09-24T04:30:00+00:00"
    assert stored.timezone_name == "Asia/Kolkata"


async def test_a_recurring_reminder_keeps_its_clock_time_across_dst(
    db_session, settings
) -> None:
    """M6: `_zone_of` could return UTC and every test still passed.

    A reminder set for 09:00 London must stay 09:00 London when the clocks
    change. Advanced in UTC it becomes 10:00 -- an hour late, once a year,
    which is exactly the kind of fault that reaches production.
    """
    service = ReminderService(db_session, settings=settings)
    london = ZoneInfo("Europe/London")
    # 24 October 2026, 09:00 BST. The clocks go back on the 25th.
    due_at = datetime(2026, 10, 24, 9, 0, tzinfo=london).astimezone(timezone.utc)
    reminder = Reminder(
        text="stretch", state=ReminderState.SCHEDULED, recurrence=Recurrence.DAILY,
        next_run_at=due_at, timezone_name="Europe/London",
    )
    db_session.add(reminder)
    await db_session.flush()

    assert await service.fire(reminder) is True
    await db_session.refresh(reminder)

    advanced = as_utc(reminder.next_run_at).astimezone(london)
    assert advanced.isoformat() == "2026-10-25T09:00:00+00:00"


def test_weekly_recurrence_advances_by_seven_days() -> None:
    """M19: only the *first* weekly occurrence was ever asserted."""
    sunday = datetime(2026, 9, 27, 9, 0, tzinfo=IST)
    following = language.next_occurrence(sunday, Recurrence.WEEKLY)
    assert following.isoformat() == "2026-10-04T09:00:00+05:30"
    # Still a Sunday, which is the whole point of "every Sunday".
    assert following.weekday() == sunday.weekday()


def test_daily_recurrence_advances_by_one_day() -> None:
    monday = datetime(2026, 9, 28, 9, 0, tzinfo=IST)
    following = language.next_occurrence(monday, Recurrence.DAILY)
    assert following.isoformat() == "2026-09-29T09:00:00+05:30"


async def test_a_reminder_given_up_on_cannot_be_fired(db_session, settings) -> None:
    """M12: the claim's state predicate had no test of its own.

    A cancelled reminder is already refused by a CHECK constraint and a
    completed one by the unique index, so removing the state predicate from
    the claim changed nothing observable. A FAILED reminder is the case where
    neither of those applies -- and firing one would deliver a reminder the
    system had already given up on.
    """
    service = ReminderService(db_session, settings=settings)
    reminder = Reminder(
        text="given up on", state=ReminderState.FAILED, recurrence=Recurrence.ONCE,
        next_run_at=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
        timezone_name="Asia/Kolkata", failure_count=3,
    )
    db_session.add(reminder)
    await db_session.flush()

    assert await service.fire(reminder) is False
    assert (await db_session.execute(select(ReminderNotification))).scalars().all() == []
    await db_session.refresh(reminder)
    assert reminder.state is ReminderState.FAILED
    assert reminder.fire_count == 0


async def test_the_database_refuses_two_notifications_for_one_occurrence(
    db_session, settings
) -> None:
    """M16: the unique index was never exercised directly.

    The atomic claim means the application never tries this. The index is the
    second line of defence, for a bug in the first -- so it needs a test that
    does what no correct caller would.
    """
    from sqlalchemy.exc import IntegrityError

    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder = Reminder(
        text="once only", state=ReminderState.SCHEDULED, recurrence=Recurrence.ONCE,
        next_run_at=due_at, timezone_name="Asia/Kolkata",
    )
    db_session.add(reminder)
    await db_session.flush()

    db_session.add(
        ReminderNotification(reminder_id=reminder.id, text="once only", due_at=due_at)
    )
    await db_session.flush()

    db_session.add(
        ReminderNotification(reminder_id=reminder.id, text="once only", due_at=due_at)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_two_occurrences_of_one_reminder_are_allowed(
    db_session, settings
) -> None:
    """The index must forbid duplicates without forbidding recurrence."""
    first = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder = Reminder(
        text="daily", state=ReminderState.SCHEDULED, recurrence=Recurrence.DAILY,
        next_run_at=first, timezone_name="Asia/Kolkata",
    )
    db_session.add(reminder)
    await db_session.flush()

    db_session.add_all([
        ReminderNotification(reminder_id=reminder.id, text="daily", due_at=first),
        ReminderNotification(
            reminder_id=reminder.id, text="daily", due_at=first + timedelta(days=1)
        ),
    ])
    await db_session.flush()

    delivered = (
        await db_session.execute(select(ReminderNotification))
    ).scalars().all()
    assert len(delivered) == 2
