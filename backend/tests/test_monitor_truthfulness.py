"""Stage 6P: the model cannot claim Gmail monitoring the application did not do.

Monitoring turns are answered by the application, so on any reply the model
writes there is no monitoring evidence -- and every claim that monitoring
started, is active, checked Gmail, matched or notified is refused by the
existing execution-truth validator, not by the prompt alone.
"""

import uuid

import pytest

from app.synthesis.execution_truth import (
    Channel,
    ExecutionRecord,
    ExecutionState,
    claims,
    record_for_turn,
    render_note,
    truthful_reply,
    validate,
)
from app.tasks.requests import MonitorOutcome, MonitorResult

pytestmark = pytest.mark.anyio


FALSE_CLAIMS = [
    # started / active / scheduled
    "Done. I'm monitoring your Gmail for emails from Apple.",
    "I'm now watching your inbox for a message from Stripe.",
    "I am keeping an eye on your inbox for that invoice.",
    "I've set up monitoring for emails from apple.com.",
    "I have scheduled a check every 15 minutes.",
    "I've created a Gmail monitor for you.",
    "Monitoring is now active.",
    "Your Gmail monitor is running.",
    "The email monitor has been set up.",
    "Monitoring has started.",
    # promises that presuppose a monitor
    "I'll let you know when an email from Apple arrives.",
    "I'll let you know when you get an email from Apple.",
    "I will notify you as soon as it comes in.",
    "I'll keep an eye on your inbox.",
    "I'm going to watch your Gmail for that.",
    "Sure! I'm monitoring your inbox and will let you know.",
    # checked / matched / notified
    "I've been checking your inbox all morning.",
    "I found a match in your inbox.",
    "The email you were waiting for has arrived.",
    "I've sent you a notification on Telegram.",
    "A notification has been sent.",
    "I've notified you.",
]

NOT_CLAIMS = [
    "I can watch your Gmail for an email from Apple if you'd like.",
    "Would you like me to monitor your inbox?",
    "I'm not monitoring your inbox.",
    "I haven't set up any monitoring.",
    "I can't monitor your inbox from here.",
    "I can’t monitor your inbox from here.",
    "I won't watch your inbox.",
    "I'll let you know when it arrives once you confirm the proposal.",
    "Once you confirm, I'll let you know when it arrives.",
    "To start, say 'watch my Gmail for an email from apple.com and tell me when it arrives'.",
    "Monitoring only starts when you confirm one of my monitoring proposals.",
    "Network monitoring is useful for servers.",
    "I'm watching the game tonight.",
    "I'll explain how email monitoring works.",
    "I'll tell you about Gmail filters.",
    "I've set up the reminder for 5pm.",
    "You asked me to watch your inbox.",
]


@pytest.mark.parametrize("text", FALSE_CLAIMS)
def test_a_monitoring_claim_is_detected(text) -> None:
    assert Channel.MONITOR in claims(text)


@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_offers_negations_and_discussion_are_not_claims(text) -> None:
    assert Channel.MONITOR not in claims(text)


@pytest.mark.parametrize("text", FALSE_CLAIMS)
def test_a_model_written_turn_can_never_license_one(text) -> None:
    """No layer result on a model-written turn is a scheduled monitor."""
    for monitor in (None, MonitorResult(), MonitorResult(outcome=MonitorOutcome.ABANDONED)):
        verdict = validate(text, record_for_turn(monitor=monitor))
        assert not verdict.ok and Channel.MONITOR in verdict.violations


def test_only_a_scheduled_monitor_with_its_task_is_an_execution() -> None:
    assert record_for_turn().monitor is ExecutionState.NOT_REQUESTED
    assert record_for_turn(monitor=MonitorResult(
        outcome=MonitorOutcome.SCHEDULED, task_id=uuid.uuid4(), reply="Done."
    )).monitor is ExecutionState.EXECUTED_SUCCESSFULLY
    # Said scheduled, carried no task: a contradiction, read as UNKNOWN.
    assert record_for_turn(monitor=MonitorResult(
        outcome=MonitorOutcome.SCHEDULED
    )).monitor is ExecutionState.UNKNOWN
    for outcome in MonitorOutcome:
        if outcome in (MonitorOutcome.NOT_MONITOR, MonitorOutcome.SCHEDULED, MonitorOutcome.FAILED):
            continue
        state = record_for_turn(monitor=MonitorResult(outcome=outcome)).monitor
        assert state is ExecutionState.PROPOSED_NOT_EXECUTED, outcome


def test_web_mail_and_calendar_are_judged_as_before() -> None:
    record = ExecutionRecord(web=ExecutionState.EXECUTED_SUCCESSFULLY)
    assert validate("I searched the web and found three articles.", record).ok
    verdict = validate("I checked your inbox and found nothing.", record)
    assert not verdict.ok and verdict.violations == (Channel.MAIL,)


def test_the_corrective_reply_is_true_whatever_monitors_exist() -> None:
    reply = truthful_reply(ExecutionRecord(), (Channel.MONITOR,))
    assert reply.startswith("I can't start Gmail monitoring or report on it from here")
    assert "nothing is watching" not in reply.lower()
    assert "Monitoring only starts when you confirm" in reply
    assert Channel.MONITOR not in claims(reply)
    combined = truthful_reply(ExecutionRecord(), (Channel.WEB, Channel.MONITOR))
    assert "web search" in combined and combined.endswith(reply)


def test_the_execution_note_states_monitoring_too() -> None:
    note = render_note(ExecutionRecord())
    assert "- Gmail monitoring setup: was not requested and did NOT happen" in note


async def test_a_turn_the_grammar_declines_cannot_claim_monitoring(
    db_session, settings, fake_provider
) -> None:
    """End to end: a phrasing the deterministic grammar does not handle reaches
    the model, the model promises to watch, and the application replaces the
    promise with the truth. No task exists."""
    from sqlalchemy import select

    from app.services.chat_service import ChatService
    from app.services.conversation_service import ConversationService
    from app.tasks.models import Task

    fake_provider.reply = "Sure! I'll let you know as soon as an email from Apple arrives."
    conversation = await ConversationService(db_session).create_conversation()
    chat = ChatService(session=db_session, provider=fake_provider, settings=settings)
    _user, assistant, *_, monitor = await chat.send_message(
        conversation.id, "can you keep tabs on apple emails for me?",
        owner_id=uuid.UUID(int=1),
    )
    assert monitor.outcome is MonitorOutcome.NOT_MONITOR
    assert assistant.content.startswith("I can't start Gmail monitoring or report on it from here")
    assert "I'll let you know" not in assistant.content
    assert (await db_session.execute(select(Task))).scalars().all() == []


async def test_a_turn_that_abandoned_a_proposal_tells_the_model_so(
    db_session, settings, fake_provider
) -> None:
    """The execution note carries this turn's monitoring state: a proposal was
    discussed and did not run -- not "nothing was requested"."""
    from app.services.chat_service import ChatService
    from app.services.conversation_service import ConversationService

    conversation = await ConversationService(db_session).create_conversation()
    chat = ChatService(session=db_session, provider=fake_provider, settings=settings)
    owner = uuid.UUID(int=1)
    await chat.send_message(
        conversation.id,
        "Watch my Gmail for an email from apple.com and tell me when it arrives.",
        owner_id=owner,
    )
    *_, monitor = await chat.send_message(conversation.id, "hello there", owner_id=owner)
    assert monitor.outcome is MonitorOutcome.ABANDONED
    prompt = "\n".join(m.content for m in fake_provider.last_call)
    assert "- Gmail monitoring setup: was discussed or proposed but did NOT happen" in prompt
