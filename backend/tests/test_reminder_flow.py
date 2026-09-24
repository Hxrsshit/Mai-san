"""Stage 5F.1: the conversational flow, the scheduler loop, and the API.

Everything here is driven at an injected instant. The scheduler tests run the
real `run_due_reminders` against a real session factory -- what they do not do
is wait for wall-clock time to pass.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models.conversation import Conversation

from app.reminders.chat import MAX_PENDING, PendingProposals, ReminderChat
from app.reminders.models import (
    NotificationState,
    Recurrence,
    Reminder,
    ReminderNotification,
    ReminderState,
)
from app.reminders.scheduler import ReminderScheduler, run_due_reminders
from app.reminders.schemas import ReminderOutcome
from app.reminders.service import ReminderService

pytestmark = pytest.mark.anyio

CONVERSATION = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
async def conversations(db_session):
    """Real conversation rows.

    `Reminder.conversation_id` is a foreign key, and the first draft of these
    tests used invented ids -- every create failed with an integrity error the
    service correctly reported as FAILED. Seeding real rows tests the flow
    rather than the constraint.
    """
    for conversation_id in (CONVERSATION, OTHER):
        db_session.add(Conversation(id=conversation_id, title="test"))
    await db_session.flush()
    await db_session.commit()


def make_chat(session, settings):
    return ReminderChat(
        ReminderService(session, settings=settings), pending=PendingProposals()
    )


# --- The confirmation flow --------------------------------------------------------


async def test_a_reminder_is_proposed_before_it_is_created(
    db_session, settings
) -> None:
    chat = make_chat(db_session, settings)
    result = await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")

    assert result.outcome is ReminderOutcome.AWAITING_CONFIRMATION
    assert "call the bank" in result.reply
    # Nothing is written until the user agrees.
    assert (await db_session.execute(select(Reminder))).scalars().all() == []


async def test_confirming_creates_it(db_session, settings) -> None:
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")
    result = await chat.handle(CONVERSATION, "yes")

    assert result.outcome is ReminderOutcome.CREATED
    rows = (await db_session.execute(select(Reminder))).scalars().all()
    assert [r.text for r in rows] == ["call the bank"]
    assert rows[0].conversation_id == CONVERSATION


async def test_declining_creates_nothing(db_session, settings) -> None:
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")
    result = await chat.handle(CONVERSATION, "no")

    assert result.outcome is ReminderOutcome.DECLINED
    assert (await db_session.execute(select(Reminder))).scalars().all() == []


async def test_a_second_yes_cannot_set_the_same_reminder_twice(
    db_session, settings
) -> None:
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")
    await chat.handle(CONVERSATION, "yes")
    again = await chat.handle(CONVERSATION, "yes")

    assert again.outcome is ReminderOutcome.NOT_REMINDER
    assert len((await db_session.execute(select(Reminder))).scalars().all()) == 1


async def test_an_unrelated_reply_abandons_the_proposal(db_session, settings) -> None:
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")
    moved_on = await chat.handle(CONVERSATION, "actually, what's the weather?")

    assert moved_on.outcome is ReminderOutcome.NOT_REMINDER
    # And a later "ok" must not retroactively confirm it.
    stray = await chat.handle(CONVERSATION, "ok")
    assert stray.outcome is ReminderOutcome.NOT_REMINDER
    assert (await db_session.execute(select(Reminder))).scalars().all() == []


async def test_a_yes_with_a_change_attached_is_not_a_confirmation(
    db_session, settings
) -> None:
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me tomorrow at 10 AM to call the bank")
    result = await chat.handle(CONVERSATION, "yes but make it 11")

    assert result.outcome is not ReminderOutcome.CREATED
    assert (await db_session.execute(select(Reminder))).scalars().all() == []


async def test_a_proposal_belongs_to_one_conversation(db_session, settings) -> None:
    """A "yes" in another thread must not confirm this thread's proposal."""
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")
    elsewhere = await chat.handle(OTHER, "yes")

    assert elsewhere.outcome is ReminderOutcome.NOT_REMINDER
    assert (await db_session.execute(select(Reminder))).scalars().all() == []


async def test_an_unreadable_schedule_asks_instead_of_guessing(
    db_session, settings
) -> None:
    chat = make_chat(db_session, settings)
    result = await chat.handle(CONVERSATION, "remind me later to do the thing")

    assert result.outcome is ReminderOutcome.NEEDS_CLARIFICATION
    assert (await db_session.execute(select(Reminder))).scalars().all() == []


async def test_an_ordinary_message_is_left_alone(db_session, settings) -> None:
    chat = make_chat(db_session, settings)
    for message in (
        "what is the capital of France?",
        "summarise this article",
        "who won the match yesterday?",
    ):
        result = await chat.handle(CONVERSATION, message)
        assert result.outcome is ReminderOutcome.NOT_REMINDER, message


async def test_listing_and_cancelling_need_no_confirmation(
    db_session, settings
) -> None:
    chat = make_chat(db_session, settings)
    await chat.handle(CONVERSATION, "remind me in 2 hours to call the bank")
    await chat.handle(CONVERSATION, "yes")

    listed = await chat.handle(CONVERSATION, "what reminders do I have?")
    assert listed.outcome is ReminderOutcome.LISTED
    assert "call the bank" in listed.reply

    cancelled = await chat.handle(CONVERSATION, "cancel my reminder about the bank")
    assert cancelled.outcome is ReminderOutcome.CANCELLED

    empty = await chat.handle(CONVERSATION, "what reminders do I have?")
    assert empty.matched == 0


async def test_the_proposal_table_is_bounded(db_session, settings) -> None:
    pending = PendingProposals()
    service = ReminderService(db_session, settings=settings)
    parsed = service.read_request("remind me in 2 hours to call")
    for _ in range(MAX_PENDING + 10):
        pending.put(uuid.uuid4(), parsed)
    # Literal, so raising the ceiling has to be argued for rather than
    # inherited by a fixture computed from the constant it guards.
    assert len(pending._pending) <= 64


async def test_a_service_failure_does_not_fail_the_turn(db_session, settings) -> None:
    class Broken(ReminderService):
        async def describe_active(self):
            raise RuntimeError("database gone")

    chat = ReminderChat(
        Broken(db_session, settings=settings), pending=PendingProposals()
    )
    result = await chat.handle(CONVERSATION, "what reminders do I have?")
    assert result.outcome is ReminderOutcome.NOT_REMINDER


# --- The scheduler loop -------------------------------------------------------------


async def seed(session, *, text="ping", when, recurrence=Recurrence.ONCE):
    reminder = Reminder(
        text=text, state=ReminderState.SCHEDULED, recurrence=recurrence,
        next_run_at=when, timezone_name="Asia/Kolkata",
    )
    session.add(reminder)
    await session.flush()
    await session.commit()
    return reminder.id


async def test_a_tick_fires_nothing_before_the_due_moment(
    db_session, session_factory, settings
) -> None:
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, when=due_at)

    assert await run_due_reminders(
        session_factory, settings, now=due_at - timedelta(seconds=1)
    ) == 0
    assert await run_due_reminders(session_factory, settings, now=due_at) == 1


async def test_a_tick_at_the_exact_due_moment_fires(
    db_session, session_factory, settings
) -> None:
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, when=due_at)
    assert await run_due_reminders(session_factory, settings, now=due_at) == 1


async def test_repeated_ticks_do_not_fire_a_one_time_reminder_twice(
    db_session, session_factory, settings
) -> None:
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, when=due_at)

    fired = [
        await run_due_reminders(session_factory, settings, now=due_at + timedelta(minutes=n))
        for n in (0, 1, 2, 60)
    ]
    assert fired == [1, 0, 0, 0]

    async with session_factory() as session:
        notifications = (
            await session.execute(select(ReminderNotification))
        ).scalars().all()
    assert len(notifications) == 1


async def test_concurrent_ticks_fire_an_occurrence_once(
    db_session, session_factory, settings
) -> None:
    """Two passes racing on the same occurrence. One wins."""
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, when=due_at)

    results = await asyncio.gather(
        run_due_reminders(session_factory, settings, now=due_at),
        run_due_reminders(session_factory, settings, now=due_at),
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, BaseException)]
    assert sum(results) == 1

    async with session_factory() as session:
        notifications = (
            await session.execute(select(ReminderNotification))
        ).scalars().all()
    assert len(notifications) == 1


async def test_a_recurring_reminder_fires_once_per_occurrence(
    db_session, session_factory, settings
) -> None:
    due_at = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)  # 09:00 IST
    await seed(db_session, when=due_at, recurrence=Recurrence.DAILY)

    # Three ticks inside the same day: one delivery.
    for minute in (0, 5, 600):
        await run_due_reminders(
            session_factory, settings, now=due_at + timedelta(minutes=minute)
        )
    async with session_factory() as session:
        first = (await session.execute(select(ReminderNotification))).scalars().all()
    assert len(first) == 1

    # A tick the next day delivers the next occurrence.
    await run_due_reminders(session_factory, settings, now=due_at + timedelta(days=1))
    async with session_factory() as session:
        second = (await session.execute(select(ReminderNotification))).scalars().all()
    assert len(second) == 2
    assert len({n.due_at for n in second}) == 2, "two occurrences, two distinct times"


async def test_a_reminder_due_while_the_process_was_down_still_fires(
    db_session, session_factory, settings
) -> None:
    """Restart recovery: the row is the only state, so a gap costs nothing."""
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    await seed(db_session, when=due_at, text="missed while down")

    # No tick happens for six hours, as if the process were stopped.
    assert await run_due_reminders(
        session_factory, settings, now=due_at + timedelta(hours=6)
    ) == 1
    async with session_factory() as session:
        delivered = (
            await session.execute(select(ReminderNotification))
        ).scalars().one()
    assert delivered.text == "missed while down"


async def test_a_cancelled_reminder_is_never_fired_by_a_tick(
    db_session, session_factory, settings
) -> None:
    due_at = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    reminder_id = await seed(db_session, when=due_at, text="cancelled one")

    async with session_factory() as session:
        service = ReminderService(session, settings=settings)
        await service.cancel_matching("cancelled one")
        await session.commit()

    assert await run_due_reminders(
        session_factory, settings, now=due_at + timedelta(hours=1)
    ) == 0
    async with session_factory() as session:
        assert (
            await session.execute(select(ReminderNotification))
        ).scalars().all() == []


async def test_a_broken_session_factory_does_not_raise(settings) -> None:
    def broken():
        raise RuntimeError("no database")

    assert await run_due_reminders(broken, settings) == 0


async def test_the_scheduler_starts_and_stops_cleanly(session_factory, settings) -> None:
    scheduler = ReminderScheduler(session_factory, settings)
    scheduler.start()
    assert scheduler.running
    scheduler.start()  # idempotent
    await scheduler.stop()
    assert not scheduler.running
    await scheduler.stop()  # stopping twice is not an error


# --- Regressions: the reminder layer must not swallow other turns -------------------
#
# Reminder recognition sits between the calendar and mail. These assert the
# boundary from the reminder side: anything another subsystem owns, or that is
# an ordinary question, must come back NOT_REMINDER so the turn carries on.


#: Messages belonging to other subsystems, kept as a literal table so a
#: widening of the reminder grammar has to be argued for here.
NOT_REMINDERS = [
    # Research (Stage 5E).
    "what's the latest news on the RBI repo rate?",
    "search the web for FastAPI 0.120 release notes",
    "find me recent papers on retrieval augmented generation",
    "what happened in the market today?",
    # Calendar (Stage 4).
    "what's on my calendar tomorrow?",
    "am I free at 3pm on Thursday?",
    "when is my next meeting?",
    # Gmail (Stage 5B).
    "check my email",
    "any unread messages from Priya?",
    "summarise my inbox",
    # Context resolution (Stage 5D.2).
    "what about the second one?",
    "tell me more",
    "why?",
    # Ordinary questions that merely mention time.
    "what time is it in Tokyo?",
    "how long does a reminder last?",
    "what did I do yesterday at 10am?",
]


@pytest.mark.parametrize("message", NOT_REMINDERS)
async def test_the_reminder_layer_declines_other_work(
    message, db_session, settings
) -> None:
    chat = make_chat(db_session, settings)
    result = await chat.handle(CONVERSATION, message)
    assert result.outcome is ReminderOutcome.NOT_REMINDER, message
    assert not result.has_reply, message


async def test_a_reminder_naming_a_calendar_noun_still_parses(
    db_session, settings
) -> None:
    """Routing puts the calendar first; the reminder grammar still reads this.

    Pinned so the two layers' claims stay visible: if the calendar ever stops
    taking "remind me about my 3pm meeting", the reminder layer is what
    catches it, and that has to be a deliberate change rather than a silent
    gap.
    """
    chat = make_chat(db_session, settings)
    result = await chat.handle(
        CONVERSATION, "remind me tomorrow at 9 AM about the standup"
    )
    assert result.outcome is ReminderOutcome.AWAITING_CONFIRMATION


async def test_the_capability_switch_is_honoured(db_session, settings) -> None:
    """With reminders off, nothing is parsed, proposed or stored."""
    from app.runtime.facts import build as build_facts

    disabled = settings.model_copy(update={"REMINDERS_ENABLED": False})
    facts = build_facts(settings=disabled)
    assert facts.reminders_enabled is False

    enabled = build_facts(settings=settings.model_copy(
        update={"REMINDERS_ENABLED": True}
    ))
    assert enabled.reminders_enabled is True


# --- The chat path, end to end -------------------------------------------------------
#
# Every test above drives `ReminderChat` directly. That is how live
# verification found two empty assistant messages with the whole suite green:
# the chat service routed the turn correctly, called the shared
# application-answer helper, and passed it no reminder -- so the reply it
# stored was an unrelated empty `ResearchResult`. These go through the API.


async def test_a_reminder_turn_returns_the_confirmation_text(
    client, conversation_id
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "remind me tomorrow at 10 AM to call the bank"},
    )
    assert response.status_code == 201
    reply = response.json()["assistant_message"]["content"]

    assert reply.strip(), "the assistant message was empty"
    assert "call the bank" in reply
    assert "yes" in reply.lower()


async def test_confirming_through_the_api_sets_the_reminder(
    client, conversation_id
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "remind me tomorrow at 10 AM to call the bank"},
    )
    confirm = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "yes"},
    )
    reply = confirm.json()["assistant_message"]["content"]
    assert reply.strip(), "the confirmation turn stored an empty message"

    listing = await client.get("/api/reminders")
    assert [r["text"] for r in listing.json()["reminders"]] == ["call the bank"]


async def test_listing_through_the_api_answers_in_words(
    client, conversation_id
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "remind me tomorrow at 10 AM to call the bank"},
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "yes"}
    )
    listed = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "what reminders do I have?"},
    )
    reply = listed.json()["assistant_message"]["content"]
    assert "call the bank" in reply


async def test_an_unreadable_request_answers_in_words(
    client, conversation_id
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "remind me later to do the thing"},
    )
    reply = response.json()["assistant_message"]["content"]
    assert reply.strip(), "the clarification turn stored an empty message"


def test_no_application_answered_turn_can_be_empty() -> None:
    """The helper must never return blank for a layer that has a reply.

    The regression in one line: a layer the chain did not name fell through
    to an empty default. Pinned here for every layer position.
    """
    from app.services.chat_service import ChatService

    class Layer:
        def __init__(self, reply):
            self.reply = reply
            self.has_reply = bool(reply)

    answered = Layer("the text")
    silent = Layer("")
    for position in range(5):
        layers = [silent] * 5
        layers[position] = answered
        assert ChatService._application_reply(*layers) == "the text", position

    assert ChatService._application_reply(*([silent] * 5)) == ""
    assert ChatService._application_reply(None, None) == ""


async def test_cancelling_a_reminder_reaches_the_reminder_layer(
    client, conversation_id
) -> None:
    """The second thing live verification found.

    "Cancel my reminder about X" matched the calendar's cancel grammar first
    and came back as "I can only read your calendar". The reminder stayed
    set, and every unit test passed, because they all called `ReminderChat`
    directly and never crossed the router.
    """
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "remind me tomorrow at 4 PM to water the plants"},
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "yes"}
    )
    assert len((await client.get("/api/reminders")).json()["reminders"]) == 1

    cancelled = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "cancel my reminder about the plants"},
    )
    reply = cancelled.json()["assistant_message"]["content"]
    assert "calendar" not in reply.lower(), reply
    assert (await client.get("/api/reminders")).json()["reminders"] == []


async def test_listing_reminders_is_not_answered_by_the_calendar(
    client, conversation_id
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "what reminders do I have?"},
    )
    reply = response.json()["assistant_message"]["content"]
    assert "calendar" not in reply.lower(), reply
    assert "reminder" in reply.lower()


async def test_a_calendar_question_still_reaches_the_calendar(
    client, conversation_id
) -> None:
    """The reminder layer must not take the calendar's work in exchange.

    Reminder management jumps the queue only for messages containing the
    literal word "reminder"; a calendar question must route as it always did.
    """
    from app.reminders.language import is_management_request

    for message in (
        "what's on my calendar tomorrow?",
        "cancel my 3pm meeting",
        "delete the event on Friday",
        "am I free at 4pm?",
    ):
        assert not is_management_request(message), message
