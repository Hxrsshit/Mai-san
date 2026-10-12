"""Stage 6N: confirmed Gmail monitoring -- recognition, proposal, confirmation.

The service tests drive `MonitorRequestService` against the real
`TaskService`, `GrantService`, runtime and runner on in-memory SQLite. The
grammars are pure and tested directly. Nothing calls a model.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.authorization.grants import GrantRefused, GrantService
from app.authorization.models import ApprovalGrant, MAX_GRANT_TTL_SECONDS
from app.background.runtime import run_due_tasks
from app.core.owner import CONFIGURED_OWNER_ID
from app.execution.models import Execution
from app.integrations.gmail_schemas import NO_MESSAGES_MATCHED
from app.services.conversation_service import ConversationService
from app.tasks import requests as monitor_requests
from app.tasks.models import Task, TaskEvent, TaskNotification, TaskNotificationKind
from app.tasks.notifications import record_outcome
from app.tasks.requests import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_LIFETIME_SECONDS,
    MAX_MONITOR_CHECKS,
    MONITOR_CAPABILITY,
    PROPOSAL_TTL_SECONDS,
    MonitorOutcome,
    MonitorRequestService,
    RecognitionKind,
    ReplyClass,
    classify_reply,
    recognise,
)
from app.tasks.runner import TaskRunner
from app.tasks.schemas import RunnerOutcome, TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from app.tools.schemas import RiskLevel

pytestmark = pytest.mark.anyio

OWNER = CONFIGURED_OWNER_ID
OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")

REQUEST = (
    "Watch my Gmail for an email from apple.com about invoice and tell me "
    "when it arrives."
)


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


# ============================================================================
# A. The request grammar
# ============================================================================


@pytest.mark.parametrize("message, sender, terms, interval, lifetime", [
    (REQUEST, "apple.com", ("invoice",), 900, 604_800),
    ("Watch for an email from Apple about invoices and tell me when it arrives.",
     "apple", ("invoices",), 900, 604_800),
    ("monitor my inbox for an email from netflix about the payment receipt and "
     "notify me every 30 minutes for 3 days", "netflix", ("payment", "receipt"),
     1800, 259_200),
    ("Keep an eye on my email for a message from bob@acme.com and let me know "
     "when it comes in", "bob@acme.com", (), 900, 604_800),
    ("Let me know when I get an email from Stripe", "stripe", (), 900, 604_800),
    ("tell me when an email from apple arrives", "apple", (), 900, 604_800),
    ("notify me if there's a new message from bob", "bob", (), 900, 604_800),
    ("Please can you watch my gmail for an email from acme.com and alert me hourly "
     "for a week", "acme.com", (), 3600, 604_800),
    ('watch my gmail for an email with the subject "quarterly report" from '
     "acme.com and tell me", "acme.com", ("quarterly", "report"), 900, 604_800),
    ("watch my gmail for an email from apple and tell me every day for 2 days",
     "apple", (), 86_400, 172_800),
    ("watch my gmail for an email from apple and tell me every 5 minutes for 1 hour",
     "apple", (), 300, 3600),
    ("ｗａｔｃｈ my Gmail for an email from APPLE and tell me", "apple", (), 900, 604_800),
])
def test_every_supported_form_is_recognised(message, sender, terms, interval, lifetime) -> None:
    recognition = recognise(message)
    assert recognition.kind is RecognitionKind.REQUEST, recognition
    assert recognition.request.sender == sender
    assert recognition.request.subject_terms == terms
    assert recognition.request.interval_seconds == interval
    assert recognition.request.lifetime_seconds == lifetime


@pytest.mark.parametrize("message, reason", [
    ("watch my gmail and tell me when an email arrives", "missing_sender"),
    ("watch my gmail for an email from my boss and tell me", "missing_sender"),
    ("watch my gmail for emails from apple or google and tell me", "multiple_senders"),
    ("watch my gmail for an email from apple and google and tell me", "multiple_senders"),
    ("watch my gmail for an email from apple, google and tell me", "multiple_senders"),
    ("watch my gmail for an email from apple and from google and tell me",
     "multiple_senders"),
    ("watch my gmail for an email from café and tell me", "invalid_sender"),
    ("watch my gmail for an email from apple every 2 minutes and tell me",
     "invalid_interval"),
    ("watch my gmail for an email from apple every 2 days and tell me",
     "invalid_interval"),
    ("watch my gmail for an email from apple every 30 seconds and tell me",
     "invalid_interval"),
    ("watch my gmail for an email from apple every so often and tell me",
     "invalid_interval"),
    ("watch my gmail for an email from apple hourly and every day and tell me",
     "invalid_interval"),
    ("watch my gmail for an email from apple for 2 weeks and tell me",
     "invalid_lifetime"),
    ("watch my gmail for an email from apple for 30 minutes and tell me",
     "invalid_lifetime"),
    ("watch my gmail for an email from apple for 2 months and tell me",
     "invalid_lifetime"),
    ("watch my gmail for an email from apple until friday and tell me",
     "absolute_date"),
    ("watch my gmail for an email from apple and tell me by 10/12", "absolute_date"),
    ("watch my gmail for an email from apple about tax return refund status and "
     "tell me", "too_many_terms"),
    ('watch my gmail for an email from apple about "tax/refund" and tell me',
     "invalid_subject"),
])
def test_each_problem_asks_for_clarification(message, reason) -> None:
    recognition = recognise(message)
    assert recognition.kind is RecognitionKind.CLARIFY, recognition
    assert recognition.reason == reason


@pytest.mark.parametrize("message", [
    "watch my gmail for an email from apple and forward it to me",
    "watch my gmail for an email from apple, reply to it and tell me",
    "tell me when I get an email from apple and delete it",
    "monitor my inbox for an email from apple, archive it and let me know",
    "watch my gmail for an email from apple, label it and notify me",
])
def test_a_write_verb_is_refused_outright(message) -> None:
    recognition = recognise(message)
    assert recognition.kind is RecognitionKind.REFUSED
    assert recognition.reason == "write_verb"


@pytest.mark.parametrize("message", [
    "check my email from apple",
    "show me my latest emails",
    "what's the weather",
    "watch my gmail for an email from apple",            # no notify intent
    "watch the news and tell me",                         # not mail
    "tell me what emails I got from apple",               # a read, not an arrival
    "remind me to call apple tomorrow",
    "",
    "watch my gmail for an email from apple and tell me " + "x" * 300,
])
def test_anything_else_is_not_a_monitoring_request(message) -> None:
    assert recognise(message).kind is RecognitionKind.NOT_MONITOR


def test_the_no_match_sentinel_never_changes() -> None:
    """Persisted monitors carry this exact text as their expected value. If
    the rendering ever changed, every stored monitor would see "not the
    sentinel" on an empty mailbox and report a match that never happened."""
    from app.integrations.gmail_schemas import MailWindow

    assert NO_MESSAGES_MATCHED == "(no messages matched)"
    assert MailWindow(messages=()).as_external_data().content == NO_MESSAGES_MATCHED


def test_the_grammar_is_pure() -> None:
    assert recognise(REQUEST) == recognise(REQUEST)


@pytest.mark.parametrize("offset, accepted", [
    (0, True), (-60, True), (60, True), (61, False), (3600, False),
    (-(30 * 86_400), True), (-(30 * 86_400) - 1, False), (-(365 * 86_400), False),
])
def test_the_baseline_is_bounded_by_the_clock_at_both_layers(offset, accepted) -> None:
    """Never more than a minute ahead, never more than 30 days old -- in the
    tool's arguments and again in the integration's query."""
    import time

    from app.integrations.gmail_schemas import MailQuery, check_received_after
    from app.tools.catalog import GmailListMessagesArguments

    now = int(time.time())
    assert (check_received_after(now + offset, now=now) == now + offset) if accepted else True
    for model in (GmailListMessagesArguments, MailQuery):
        if offset == 61:
            continue    # one second from the bound: the models read the live clock
        if accepted and abs(offset) < 30 * 86_400 - 120:
            assert model(received_after=now + offset).received_after == now + offset
        elif not accepted:
            with pytest.raises(Exception):
                model(received_after=now + offset)
    if not accepted:
        with pytest.raises(ValueError):
            check_received_after(now + offset, now=now)


@pytest.mark.parametrize("value", [True, False, 1.5, "1700000000", [1], None])
def test_only_an_integer_is_a_baseline(value) -> None:
    from app.integrations.gmail_schemas import MailQuery, check_received_after

    with pytest.raises(ValueError):
        check_received_after(value, now=1_800_000_000)
    if value is not None:
        with pytest.raises(Exception):
            MailQuery(received_after=value)


def test_the_baseline_renders_as_an_after_clause_and_nothing_else() -> None:
    import time

    from app.integrations.gmail_schemas import MailQuery

    now = int(time.time())
    assert MailQuery(received_after=now).to_gmail_query() == f"after:{now}"
    assert MailQuery(sender="apple.com", subject_terms=("invoice",), received_after=now
                     ).to_gmail_query() == f'from:("apple.com") subject:("invoice") after:{now}'
    assert "after:" not in MailQuery(sender="apple.com").to_gmail_query()


# ============================================================================
# B. The reply grammar
# ============================================================================


@pytest.mark.parametrize("message", [
    "yes", "Yes.", "YES!", "yes, do it", "Yes, do it.", "yes please",
    "go ahead", "do it", "confirm", "okay, start it", "ok", "sure", "yeah",
    "yep", "confirmed", "start monitoring", "set it up", "please do it",
    "go ahead please", "yes thanks", "yes thank you", "ｙｅｓ",
])
def test_plain_confirmations_confirm(message) -> None:
    assert classify_reply(message) is ReplyClass.CONFIRM


@pytest.mark.parametrize("message", [
    "yes, every 30 minutes", "yes but change it", "actually make it every hour",
    "yes yes", "sure thing", "yes 2", "okay but only for a day", "yes from google",
    "go ahead and make it hourly", "remind me at 5",
])
def test_anything_that_changes_or_extends_it_invalidates(message) -> None:
    assert classify_reply(message) is ReplyClass.INVALIDATED


@pytest.mark.parametrize("message", [
    "no", "No.", "nope", "cancel", "stop", "don't", "do not", "never mind",
    "nevermind", "no thanks", "no thank you", "cancel please",
])
def test_a_decline_declines(message) -> None:
    assert classify_reply(message) is ReplyClass.DECLINE


def test_a_full_new_request_replaces() -> None:
    assert classify_reply(
        "watch my gmail for an email from google and tell me"
    ) is ReplyClass.REPLACE


@pytest.mark.parametrize("message, expected", [
    ("no, but every hour", ReplyClass.INVALIDATED),
    ("no thanks, make it daily", ReplyClass.INVALIDATED),
    ("cancel the meeting", ReplyClass.OTHER),
    ("stop the music", ReplyClass.OTHER),
    ("no idea", ReplyClass.OTHER),
])
def test_a_decline_with_more_words_is_not_a_decline(message, expected) -> None:
    assert classify_reply(message) is expected


@pytest.mark.parametrize("message", ["what's the weather", "thanks for that", "hello"])
def test_an_unrelated_message_is_other(message) -> None:
    assert classify_reply(message) is ReplyClass.OTHER


def test_a_long_or_symbolic_reply_never_confirms() -> None:
    assert classify_reply("yes " * 11) is not ReplyClass.CONFIRM
    assert classify_reply("yes 👍") is not ReplyClass.CONFIRM
    assert classify_reply("y-e-s") is not ReplyClass.CONFIRM


# ============================================================================
# C. The proposal
# ============================================================================


async def conversation(session):
    return (await ConversationService(session).create_conversation()).id


def service(session, settings, owner=OWNER):
    return MonitorRequestService(session, settings=settings, owner_id=owner)


async def tasks_of(session):
    return (await session.execute(select(Task))).scalars().all()


async def grants_of(session):
    return (await session.execute(select(ApprovalGrant))).scalars().all()


async def events_of(session, task_id):
    rows = (await session.execute(
        select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.sequence)
    )).scalars().all()
    return rows


async def test_a_request_writes_one_planned_inert_proposal(db_session, settings) -> None:
    conv = await conversation(db_session)
    before = int(datetime.now(timezone.utc).timestamp())
    result = await service(db_session, settings).handle(conv, REQUEST)
    after = int(datetime.now(timezone.utc).timestamp())

    assert result.outcome is MonitorOutcome.PROPOSED
    [task] = await tasks_of(db_session)
    assert task.id == result.task_id
    assert task.state is TaskState.PLANNED
    assert task.owner_id == OWNER and task.conversation_id == conv
    assert task.authorized_at is None and task.next_run_at is None
    assert task.monitor == {
        "condition": {
            "kind": "value", "path": "external.content",
            "operator": "ne", "expected": NO_MESSAGES_MATCHED,
        },
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    }
    [step] = task.plan["tasks"]
    assert step["capability"] == MONITOR_CAPABILITY
    arguments = step["arguments"]
    assert arguments == {
        "sender": "apple.com", "subject_terms": ["invoice"], "text_terms": [],
        "unread_only": False, "newer_than_days": None, "max_results": 1,
        "body_count": 0, "received_after": arguments["received_after"],
    }
    assert before <= arguments["received_after"] <= after
    assert await grants_of(db_session) == []
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_the_deadline_fits_the_check_budget(db_session, settings) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    [task] = await tasks_of(db_session)
    baseline = task.plan["tasks"][0]["arguments"]["received_after"]
    deadline = task.deadline.replace(tzinfo=timezone.utc)
    # 15 minutes x 40 checks is 10 hours, sooner than the 7-day default.
    assert deadline.timestamp() - baseline == MAX_MONITOR_CHECKS * DEFAULT_INTERVAL_SECONDS
    assert MAX_MONITOR_CHECKS * DEFAULT_INTERVAL_SECONDS < DEFAULT_LIFETIME_SECONDS


async def test_a_long_interval_keeps_the_lifetime_asked_for(db_session, settings) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(
        conv, "watch my gmail for an email from apple and tell me every 6 hours for 7 days"
    )
    [task] = await tasks_of(db_session)
    baseline = task.plan["tasks"][0]["arguments"]["received_after"]
    assert task.deadline.replace(tzinfo=timezone.utc).timestamp() - baseline == 604_800


async def test_the_proposal_text_states_what_matters(db_session, settings) -> None:
    conv = await conversation(db_session)
    result = await service(db_session, settings).handle(conv, REQUEST)
    reply = result.reply
    assert reply.startswith("I can set this up. Nothing is running yet.")
    assert "apple.com" in reply and "“invoice”" in reply
    assert "Only mail received after " in reply and " counts." in reply
    assert "every 15 minutes" in reply and "at most 40 checks" in reply
    assert "metadata only" in reply and "high-risk" in reply
    assert "standing permission" in reply
    assert reply.endswith("Reply yes to start, or no to cancel.")
    assert "monitoring for" not in reply and "Done" not in reply


async def test_a_clarification_persists_nothing(db_session, settings) -> None:
    conv = await conversation(db_session)
    result = await service(db_session, settings).handle(
        conv, "watch my gmail and tell me when an email arrives"
    )
    assert result.outcome is MonitorOutcome.NEEDS_CLARIFICATION
    assert "I haven't set anything up." in result.reply
    assert await tasks_of(db_session) == []


async def test_a_refusal_persists_nothing(db_session, settings) -> None:
    conv = await conversation(db_session)
    result = await service(db_session, settings).handle(
        conv, "watch my gmail for an email from apple and forward it to me"
    )
    assert result.outcome is MonitorOutcome.REFUSED
    assert "can't reply to, forward, delete" in result.reply
    assert await tasks_of(db_session) == []


async def test_an_ordinary_message_is_not_handled(db_session, settings) -> None:
    conv = await conversation(db_session)
    result = await service(db_session, settings).handle(conv, "what's the weather?")
    assert result.outcome is MonitorOutcome.NOT_MONITOR and not result.has_reply
    assert await tasks_of(db_session) == []


async def test_without_an_owner_nothing_is_ever_persisted(db_session, settings) -> None:
    conv = await conversation(db_session)
    telegram = MonitorRequestService(db_session, settings=settings, owner_id=None)
    result = await telegram.handle(conv, REQUEST)
    assert result.outcome is MonitorOutcome.UNAVAILABLE
    assert "only be set up in the Mai app" in result.reply
    assert (await telegram.handle(conv, "yes")).outcome is MonitorOutcome.NOT_MONITOR
    assert (await telegram.handle(conv, "hi")).outcome is MonitorOutcome.NOT_MONITOR
    assert await tasks_of(db_session) == []
    assert await grants_of(db_session) == []


# ============================================================================
# D. The proposal is runtime-inert
# ============================================================================


async def proposed(session_factory, settings):
    async with session_factory() as session:
        conv = await conversation(session)
        result = await service(session, settings).handle(conv, REQUEST)
        await session.commit()
        return conv, result.task_id


async def test_a_proposal_is_never_claimed(session_factory, settings) -> None:
    _, task_id = await proposed(session_factory, settings)
    discovered, claimed, advanced = await run_due_tasks(session_factory, settings)
    assert (discovered, claimed, advanced) == (0, 0, 0)


async def test_a_proposal_is_not_claimed_even_with_a_forged_schedule(
    session_factory, settings
) -> None:
    _, task_id = await proposed(session_factory, settings)
    async with session_factory() as session:
        await session.execute(
            update(Task).where(Task.id == task_id).values(
                next_run_at=datetime.now(timezone.utc) - timedelta(hours=1)
            )
        )
        await session.commit()
    assert await run_due_tasks(session_factory, settings) == (0, 0, 0)


async def test_a_proposal_cannot_be_scheduled(session_factory, settings) -> None:
    _, task_id = await proposed(session_factory, settings)
    async with session_factory() as session:
        result = await TaskService(session, settings=settings, owner_id=OWNER).schedule_background(task_id)
        assert result.outcome is TaskOutcome.REFUSED and result.reason == "plan_not_authorized"
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        assert task.next_run_at is None


async def test_a_proposal_cannot_execute(session_factory, settings) -> None:
    _, task_id = await proposed(session_factory, settings)
    async with session_factory() as session:
        runner = TaskRunner(TaskService(session, settings=settings, owner_id=OWNER))
        for attempt in (runner.check, runner.advance):
            result = await attempt(task_id)
            assert result.outcome is RunnerOutcome.REFUSED
            assert result.reason == "plan_not_authorized"
        assert (await session.execute(select(Execution))).scalars().all() == []


async def test_a_proposal_creates_no_grant_and_no_notification(
    session_factory, settings
) -> None:
    _, task_id = await proposed(session_factory, settings)
    await run_due_tasks(session_factory, settings)
    async with session_factory() as session:
        assert await grants_of(session) == []
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        for kind in TaskNotificationKind:
            assert await record_outcome(session, task, kind) is None
        assert (await session.execute(select(TaskNotification))).scalars().all() == []


async def test_a_proposal_never_reaches_gmail(session_factory, settings, monkeypatch) -> None:
    """Every path a proposal can take, with the integration rigged to record."""
    calls = []

    async def recording(self, arguments):  # pragma: no cover - must not run
        calls.append(arguments)

    monkeypatch.setattr(
        "app.integrations.google_gmail.GoogleGmailIntegration._list_messages", recording
    )
    for answer in ("no", "yes, every hour", "what's the weather", None):
        conv, task_id = await proposed(session_factory, settings)
        await run_due_tasks(session_factory, settings)
        if answer is not None:
            async with session_factory() as session:
                await service(session, settings).handle(conv, answer)
                await session.commit()
        await run_due_tasks(session_factory, settings)
    assert calls == []


# ============================================================================
# E. Confirmation
# ============================================================================


async def test_confirmation_authorizes_grants_and_schedules_the_one_row(
    db_session, settings
) -> None:
    conv = await conversation(db_session)
    proposal = await service(db_session, settings).handle(conv, REQUEST)
    before = datetime.now(timezone.utc)
    result = await service(db_session, settings).handle(conv, "Yes, do it.")

    assert result.outcome is MonitorOutcome.SCHEDULED
    assert result.task_id == proposal.task_id
    assert result.reply.startswith("Done. I'm monitoring for a Gmail message from apple.com")
    [task] = await tasks_of(db_session)
    assert task.state is TaskState.QUEUED
    assert task.authorized_at is not None and task.next_run_at is not None
    [grant] = await grants_of(db_session)
    assert grant.capability == MONITOR_CAPABILITY
    assert grant.risk_level is RiskLevel.HIGH
    assert grant.owner_id == OWNER and grant.revoked_at is None
    deadline = task.deadline.replace(tzinfo=timezone.utc)
    expires = grant.expires_at.replace(tzinfo=timezone.utc)
    # ceil(deadline - now): ends at the deadline, within the second.
    assert deadline <= expires <= deadline + timedelta(seconds=1)
    assert (expires - before) <= timedelta(seconds=MAX_GRANT_TTL_SECONDS)
    kinds = [e.event_type.value for e in await events_of(db_session, task.id)]
    for expected in ("approval_requested", "background_scheduled"):
        assert expected in kinds
    approvals = [
        e for e in await events_of(db_session, task.id)
        if e.event_type.value == "state_changed"
        and (e.event_metadata or {}).get("reason") == "monitor_confirmed"
    ]
    assert len(approvals) == 1 and approvals[0].actor == "user"


async def test_a_second_yes_falls_through_and_changes_nothing(db_session, settings) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    await service(db_session, settings).handle(conv, "yes")
    again = await service(db_session, settings).handle(conv, "yes")
    assert again.outcome is MonitorOutcome.NOT_MONITOR and not again.has_reply
    assert len(await grants_of(db_session)) == 1
    [task] = await tasks_of(db_session)
    assert task.state is TaskState.QUEUED


async def test_confirmation_is_bound_to_this_conversation(db_session, settings) -> None:
    conv = await conversation(db_session)
    elsewhere = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    result = await service(db_session, settings).handle(elsewhere, "yes")
    assert result.outcome is MonitorOutcome.NOT_MONITOR
    [task] = await tasks_of(db_session)
    assert task.state is TaskState.PLANNED
    assert await grants_of(db_session) == []


async def test_confirmation_is_bound_to_the_owner(db_session, settings) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    result = await service(db_session, settings, owner=OTHER).handle(conv, "yes")
    assert result.outcome is MonitorOutcome.NOT_MONITOR
    [task] = await tasks_of(db_session)
    assert task.state is TaskState.PLANNED and task.authorized_at is None


async def test_a_row_already_answered_is_not_confirmed_twice(db_session, settings) -> None:
    """The conditional claim: a confirmation that loses the race does nothing,
    and in particular does not cancel what the winner confirmed."""
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    [task] = await tasks_of(db_session)
    monitors = service(db_session, settings)
    await monitors.handle(conv, "yes")
    result = await monitors._confirm(task)
    # Changed deliberately in 6P: the loser says what actually happened --
    # the monitor is already active -- instead of falling through to the
    # model with a bare "yes".
    assert result.outcome is MonitorOutcome.ALREADY_MONITORING
    assert result.reply.startswith("That proposal had already been confirmed")
    await db_session.refresh(task)
    assert task.state is TaskState.QUEUED
    assert len(await grants_of(db_session)) == 1


@pytest.mark.parametrize("failure", ["authorize", "approve", "grant", "schedule", "ttl"])
async def test_any_failed_step_rolls_back_everything(
    db_session, settings, monkeypatch, failure
) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)

    from app.tasks.schemas import TaskResult

    refused = TaskResult(outcome=TaskOutcome.REFUSED, reason="injected")
    if failure == "authorize":
        async def authorize(self, task_id):
            return refused
        monkeypatch.setattr(TaskService, "authorize_plan", authorize)
    elif failure == "approve":
        original = TaskService.transition

        async def transition(self, task_id, target, actor="user", reason=None):
            if target is TaskState.QUEUED:
                return refused
            return await original(self, task_id, target, actor=actor, reason=reason)
        monkeypatch.setattr(TaskService, "transition", transition)
    elif failure == "grant":
        async def create(self, *args, **kwargs):
            raise GrantRefused("injected")
        monkeypatch.setattr(GrantService, "create", create)
    elif failure == "schedule":
        async def schedule(self, task_id, now=None):
            return refused
        monkeypatch.setattr(TaskService, "schedule_background", schedule)
    else:
        monkeypatch.setattr(
            "app.authorization.models.MAX_GRANT_TTL_SECONDS", 1, raising=True
        )

    attempts = []
    if failure != "grant":
        original_create = GrantService.create

        async def counted(self, *args, **kwargs):
            attempts.append(args[0] if args else kwargs.get("capability"))
            return await original_create(self, *args, **kwargs)
        monkeypatch.setattr(GrantService, "create", counted)

    result = await service(db_session, settings).handle(conv, "yes")
    assert result.outcome is MonitorOutcome.FAILED
    assert result.reply == "Monitoring was not set up. I haven't set anything up."
    # A refusal stops the sequence where it happens: no grant is even
    # attempted after a refused authorization, approval or lifetime check.
    if failure in ("authorize", "approve", "ttl"):
        assert attempts == []
    [task] = await tasks_of(db_session)
    await db_session.refresh(task)
    assert task.state is TaskState.CANCELLED
    assert task.authorized_at is None and task.next_run_at is None
    assert await grants_of(db_session) == []
    reasons = [
        (e.event_metadata or {}).get("reason") for e in await events_of(db_session, task.id)
        if e.event_type.value == "task_cancelled"
    ]
    assert reasons == ["proposal_failed"]


# ============================================================================
# F. Declining, invalidating, replacing, abandoning, expiring
# ============================================================================


async def cancelled_reason(session, task_id):
    for event in await events_of(session, task_id):
        if event.event_type.value == "task_cancelled":
            return (event.event_metadata or {}).get("reason")
    return None


async def test_a_decline_cancels_the_proposal(db_session, settings) -> None:
    conv = await conversation(db_session)
    proposal = await service(db_session, settings).handle(conv, REQUEST)
    result = await service(db_session, settings).handle(conv, "no thanks")
    assert result.outcome is MonitorOutcome.DECLINED
    assert result.reply == "Okay, I won't monitor that. Nothing was set up."
    assert await cancelled_reason(db_session, proposal.task_id) == "proposal_declined"
    assert await grants_of(db_session) == []


@pytest.mark.parametrize("answer", [
    "yes, every 30 minutes", "yes but change it", "actually make it every hour",
])
async def test_a_changed_specification_invalidates_and_starts_nothing(
    db_session, settings, answer
) -> None:
    conv = await conversation(db_session)
    proposal = await service(db_session, settings).handle(conv, REQUEST)
    result = await service(db_session, settings).handle(conv, answer)
    assert result.outcome is MonitorOutcome.INVALIDATED
    assert result.reply.startswith("I didn't start monitoring.")
    assert await cancelled_reason(db_session, proposal.task_id) == "proposal_invalidated"
    # The old row is never adjusted, and a later "yes" has nothing to confirm.
    [task] = await tasks_of(db_session)
    assert task.monitor["interval_seconds"] == DEFAULT_INTERVAL_SECONDS
    assert (await service(db_session, settings).handle(conv, "yes")).outcome is (
        MonitorOutcome.NOT_MONITOR
    )
    assert await grants_of(db_session) == []


async def test_a_new_full_request_replaces_the_proposal(db_session, settings) -> None:
    conv = await conversation(db_session)
    first = await service(db_session, settings).handle(conv, REQUEST)
    second = await service(db_session, settings).handle(
        conv, "watch my gmail for an email from google and tell me every hour"
    )
    assert second.outcome is MonitorOutcome.PROPOSED and second.task_id != first.task_id
    assert second.reply.startswith("I've cancelled the earlier proposal. I can set this up.")
    assert await cancelled_reason(db_session, first.task_id) == "proposal_replaced"
    confirmed = await service(db_session, settings).handle(conv, "yes")
    assert confirmed.task_id == second.task_id


async def test_an_unrelated_message_abandons_and_falls_through(db_session, settings) -> None:
    conv = await conversation(db_session)
    proposal = await service(db_session, settings).handle(conv, REQUEST)
    result = await service(db_session, settings).handle(conv, "what's the weather?")
    assert result.outcome is MonitorOutcome.ABANDONED and not result.has_reply
    assert await cancelled_reason(db_session, proposal.task_id) == "proposal_abandoned"


async def test_an_expired_proposal_is_never_confirmed(db_session, settings) -> None:
    conv = await conversation(db_session)
    proposal = await service(db_session, settings).handle(conv, REQUEST)
    await db_session.execute(
        update(Task).where(Task.id == proposal.task_id).values(
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=PROPOSAL_TTL_SECONDS + 1)
        )
    )
    result = await service(db_session, settings).handle(conv, "yes")
    assert result.outcome is MonitorOutcome.EXPIRED
    assert "expired" in result.reply and "nothing was started" in result.reply
    assert await cancelled_reason(db_session, proposal.task_id) == "proposal_expired"
    assert await grants_of(db_session) == []


async def test_a_proposal_just_inside_its_window_still_confirms(db_session, settings) -> None:
    conv = await conversation(db_session)
    proposal = await service(db_session, settings).handle(conv, REQUEST)
    await db_session.execute(
        update(Task).where(Task.id == proposal.task_id).values(
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=PROPOSAL_TTL_SECONDS - 30)
        )
    )
    assert (await service(db_session, settings).handle(conv, "yes")).outcome is (
        MonitorOutcome.SCHEDULED
    )


async def test_an_identical_active_monitor_is_not_duplicated(db_session, settings) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    await service(db_session, settings).handle(conv, "yes")
    result = await service(db_session, settings).handle(conv, REQUEST)
    assert result.outcome is MonitorOutcome.ALREADY_MONITORING
    assert "already monitoring" in result.reply
    assert len(await tasks_of(db_session)) == 1


async def test_a_different_subject_is_not_a_duplicate(db_session, settings) -> None:
    conv = await conversation(db_session)
    await service(db_session, settings).handle(conv, REQUEST)
    await service(db_session, settings).handle(conv, "yes")
    result = await service(db_session, settings).handle(
        conv, REQUEST.replace("invoice", "receipt")
    )
    assert result.outcome is MonitorOutcome.PROPOSED


async def test_an_older_live_proposal_is_superseded(db_session, settings) -> None:
    conv = await conversation(db_session)
    first = await service(db_session, settings).handle(conv, REQUEST)
    # A second row written behind the service's back -- two concurrent
    # requests -- leaves the older one to be withdrawn on the next turn.
    second = await service(db_session, settings)._propose(
        conv, recognise(REQUEST.replace("invoice", "receipt")).request
    )
    await db_session.execute(
        update(Task).where(Task.id == first.task_id).values(
            created_at=datetime.now(timezone.utc) - timedelta(seconds=60)
        )
    )
    result = await service(db_session, settings).handle(conv, "yes")
    assert result.task_id == second.task_id
    assert await cancelled_reason(db_session, first.task_id) == "proposal_replaced"


async def test_a_handling_error_is_contained_and_truthful(db_session, settings, monkeypatch) -> None:
    async def broken(self, conversation_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(MonitorRequestService, "_pending_for", broken)
    conv = await conversation(db_session)
    result = await service(db_session, settings).handle(conv, REQUEST)
    assert result.outcome is MonitorOutcome.FAILED
    assert result.reply.startswith("Monitoring was not set up.")


# ============================================================================
# G. Cancellation
# ============================================================================


async def confirmed(session, settings, message=REQUEST):
    conv = await conversation(session)
    await service(session, settings).handle(conv, message)
    return (await service(session, settings).handle(conv, "yes")).task_id


async def test_cancelling_a_monitor_releases_its_grant(db_session, settings) -> None:
    task_id = await confirmed(db_session, settings)
    result = await service(db_session, settings).cancel(task_id)
    assert result.outcome is TaskOutcome.CANCELLED
    [grant] = await grants_of(db_session)
    assert grant.revoked_at is not None and grant.revoked_reason == "monitor_ended"
    assert await cancelled_reason(db_session, task_id) == "cancelled_by_owner"


async def test_a_grant_another_monitor_needs_is_kept(db_session, settings) -> None:
    first = await confirmed(db_session, settings)
    await confirmed(db_session, settings, REQUEST.replace("invoice", "receipt"))
    await service(db_session, settings).cancel(first)
    assert [g.revoked_at for g in await grants_of(db_session)] == [None, None]


async def test_cancel_is_idempotent_terminal_aware_and_owner_scoped(db_session, settings) -> None:
    task_id = await confirmed(db_session, settings)
    monitors = service(db_session, settings)
    assert (await monitors.cancel(task_id)).outcome is TaskOutcome.CANCELLED
    again = await monitors.cancel(task_id)
    assert again.outcome is TaskOutcome.CANCELLED and again.state is TaskState.CANCELLED

    other = await service(db_session, settings, owner=OTHER).cancel(
        await confirmed(db_session, settings, REQUEST.replace("invoice", "x1"))
    )
    assert other.outcome is TaskOutcome.NOT_FOUND
    assert (await service(db_session, settings).cancel(uuid.uuid4())).outcome is (
        TaskOutcome.NOT_FOUND
    )
    assert (await MonitorRequestService(db_session, settings=settings).cancel(task_id)).outcome is (
        TaskOutcome.NOT_FOUND
    )

    done = await confirmed(db_session, settings, REQUEST.replace("invoice", "x2"))
    await db_session.execute(
        update(Task).where(Task.id == done).values(
            state=TaskState.COMPLETED, completed_at=datetime.now(timezone.utc)
        )
    )
    db_session.expire_all()
    assert (await monitors.cancel(done)).outcome is TaskOutcome.TERMINAL
