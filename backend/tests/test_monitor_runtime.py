"""Stage 6N: a confirmed monitor through the real runtime, and how it ends.

Through the real `BackgroundRuntime`, `TaskRunner.check`, authorization with
the standing grant the confirmation created, `ExecutionService`, the
dispatcher, `GmailListMessagesTool` and the `google_gmail` integration with
its `SecureHttpClient` -- only the socket is replaced. Then 6H's notification
and, where enabled, 6M.3's automatic delivery to a stubbed Telegram. Every
token is a synthetic sentinel. No test sleeps: time is moved by editing rows.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select, update

from app.authorization.models import ApprovalGrant
from app.background.runtime import BackgroundRuntime, run_due_tasks
from app.composition import notification_delivery as composition
from app.composition.notification_delivery import get_delivery_registry
from app.core.owner import CONFIGURED_OWNER_ID
from app.delivery.models import DeliveryRecordStatus, NotificationDelivery
from app.execution.models import Execution
from app.services.conversation_service import ConversationService
from app.tasks.models import Task, TaskEvent, TaskNotification, TaskNotificationKind
from app.tasks.monitoring import MAX_MONITOR_LIFETIME_SECONDS, clamp_to_deadline, deadline_passed
from app.tasks.requests import MonitorOutcome, MonitorRequestService
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from tests.support.stub_transport import GmailTransport, gmail_list_payload, gmail_message_payload
from tests.scopes import ADMIN_SCOPE

pytestmark = pytest.mark.anyio

OWNER = CONFIGURED_OWNER_ID
REQUEST = (
    "Watch my Gmail for an email from apple.com about invoice and tell me "
    "when it arrives."
)
EMPTY = {"resultSizeEstimate": 0}
TOKEN = "999000111:TOKEN-SENTINEL-6n_Mon1"
CHAT_ID = 646464


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


@pytest.fixture
def gmail(calendar_settings, gmail_tokens, monkeypatch):
    """The process-wide execution path, pointed at a stubbed Gmail socket."""
    import app.execution.dispatcher as dispatcher
    import app.execution.service as execution_service
    import app.execution.tools as execution_tools
    from app.execution.gmail_tools import GmailGetMessageTool, GmailListMessagesTool
    from app.execution.tools import ExecutableRegistry
    from app.integrations.google_gmail import GoogleGmailIntegration
    from app.integrations.registry import IntegrationRegistry

    transport = GmailTransport(listing=EMPTY)

    def resolve(host, port):
        return [(2, 1, 6, "", ("142.250.72.1", port))]

    integrations = IntegrationRegistry()
    integrations.register(GoogleGmailIntegration(
        settings=calendar_settings, store=gmail_tokens,
        api_transport=transport, token_transport=transport, resolve=resolve,
    ))
    integrations.seal()
    executable = ExecutableRegistry()
    executable.register(GmailListMessagesTool())
    executable.register(GmailGetMessageTool())

    monkeypatch.setattr(execution_tools, "_registry", executable, raising=False)
    monkeypatch.setattr(execution_tools, "get_executable_registry", lambda: executable)
    monkeypatch.setattr(execution_service, "get_executable_registry", lambda: executable)
    monkeypatch.setattr(dispatcher, "get_executable_registry", lambda: executable)
    monkeypatch.setattr(dispatcher, "get_integration_registry", lambda: integrations)
    return transport


async def monitor(session_factory, settings, message=REQUEST):
    """Propose and confirm, as two committed chat turns would."""
    async with session_factory() as session:
        conv = (await ConversationService(session, ADMIN_SCOPE).create_conversation()).id
        proposal = await MonitorRequestService(session, settings, OWNER).handle(conv, message)
        assert proposal.outcome is MonitorOutcome.PROPOSED
        await session.commit()
    async with session_factory() as session:
        confirmed = await MonitorRequestService(session, settings, OWNER).handle(conv, "yes")
        assert confirmed.outcome is MonitorOutcome.SCHEDULED, confirmed
        await session.commit()
    return confirmed.task_id


async def load(session_factory, task_id):
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.sequence)
        )).scalars().all()
        return task, events


async def rows(session_factory, model):
    async with session_factory() as session:
        return (await session.execute(select(model))).scalars().all()


def utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def listings(transport):
    return [u for u in transport.urls if urlparse(u).path.endswith("/messages")]


async def make_due(session_factory, task_id, at=None):
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            next_run_at=at or datetime.now(timezone.utc) - timedelta(seconds=1)
        ))
        await session.commit()


# ============================================================================
# A. A check reads Gmail with the confirmed query, and only reads
# ============================================================================


async def test_a_check_lists_gmail_with_sender_subject_and_baseline(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    task, _ = await load(session_factory, task_id)
    baseline = task.plan["tasks"][0]["arguments"]["received_after"]

    assert await run_due_tasks(session_factory, calendar_settings) == (1, 1, 1)

    [url] = listings(gmail)
    query = parse_qs(urlparse(url).query)
    assert query["q"] == [f'from:("apple.com") subject:("invoice") after:{baseline}']
    assert query["maxResults"] == ["1"] and query["includeSpamTrash"] == ["false"]
    assert gmail.methods == ["GET"]                 # read only, nothing else
    [grant] = await rows(session_factory, ApprovalGrant)
    _, events = await load(session_factory, task_id)
    used = [e for e in events if e.event_type.value == "standing_grant_used"]
    assert len(used) == 1 and used[0].event_metadata["grant_id"] == str(grant.id)


async def test_no_match_reschedules_one_interval_later(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    now = datetime.now(timezone.utc)
    await run_due_tasks(session_factory, calendar_settings, now=now)
    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.RUNNING
    assert utc(task.next_run_at) == now + timedelta(seconds=900)
    assert task.failure_count == 0 and task.check_count == 1
    assert "observation_recorded" in [e.event_type.value for e in events]
    assert await rows(session_factory, TaskNotification) == []


async def test_a_match_completes_and_notifies_once(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    await run_due_tasks(session_factory, calendar_settings)          # nothing yet
    gmail.listing = gmail_list_payload(1)
    gmail.messages = {"msg-1": gmail_message_payload(
        "msg-1", sender="Apple <no_reply@apple.com>", subject="Your invoice"
    )}
    await make_due(session_factory, task_id)
    await run_due_tasks(session_factory, calendar_settings)

    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    [note] = await rows(session_factory, TaskNotification)
    assert note.kind is TaskNotificationKind.CONDITION_MET and note.owner_id == OWNER
    # Only metadata was asked for, never a body.
    fetches = [u for u in gmail.urls if "/messages/" in u]
    assert fetches and all("format=metadata" in u for u in fetches)
    # Nothing it observed reached the journal.
    for event in events:
        assert "Your invoice" not in json.dumps(event.event_metadata or {})
    assert await run_due_tasks(session_factory, calendar_settings) == (0, 0, 0)
    assert len(await rows(session_factory, TaskNotification)) == 1


async def test_the_full_loop_delivers_through_the_existing_automatic_path(
    session_factory, calendar_settings, gmail, monkeypatch
) -> None:
    """Request, confirmation, a tick that matches, 6H, and 6M.3 to a stubbed
    Telegram -- in that same tick."""
    from tests.test_notification_composition import stub_network

    calendar_settings.TELEGRAM_BOT_TOKEN = TOKEN
    calendar_settings.TELEGRAM_ALLOWED_CHAT_ID = str(CHAT_ID)
    calendar_settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS = ["telegram"]
    monkeypatch.setattr(composition, "_registry", None)
    monkeypatch.setattr(composition, "get_settings", lambda: calendar_settings)
    runtime = BackgroundRuntime(session_factory, calendar_settings)
    telegram = stub_network(get_delivery_registry())

    gmail.listing = gmail_list_payload(1)
    task_id = await monitor(session_factory, calendar_settings)
    report = await runtime.tick()

    [note] = await rows(session_factory, TaskNotification)
    assert note.task_id == task_id
    assert report.deliveries_attempted == 1
    assert json.loads(telegram.bodies[0])["chat_id"] == CHAT_ID
    [record] = await rows(session_factory, NotificationDelivery)
    assert record.status is DeliveryRecordStatus.DELIVERED


# ============================================================================
# B. The end: expiry is terminal, silent, and keeps no grant
# ============================================================================


async def test_at_the_deadline_the_monitor_is_cancelled_not_failed(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    await run_due_tasks(session_factory, calendar_settings)
    reads_before = len(gmail.urls)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1)
        ))
        await session.commit()
    await make_due(session_factory, task_id)

    await run_due_tasks(session_factory, calendar_settings)

    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED
    assert task.next_run_at is None
    assert task.failure_count == 0                  # not a failure
    assert len(gmail.urls) == reads_before          # nothing read
    assert await rows(session_factory, TaskNotification) == []
    tail = [(e.event_type.value, (e.event_metadata or {}).get("reason")) for e in events][-3:]
    assert tail == [
        ("runner_refused", "monitor_expired"),
        ("task_cancelled", "expired"),
        ("background_unscheduled", "monitor_expired"),
    ]
    [grant] = await rows(session_factory, ApprovalGrant)
    assert grant.revoked_at is not None and grant.revoked_reason == "monitor_ended"
    executions = await rows(session_factory, Execution)
    assert len(executions) == 1                     # only the earlier check


async def test_an_expired_monitor_is_never_blocked_or_retried(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            deadline=datetime.now(timezone.utc) - timedelta(seconds=1)
        ))
        await session.commit()
    for _ in range(4):
        await make_due(session_factory, task_id)
        await run_due_tasks(session_factory, calendar_settings)
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED and task.failure_count == 0
    assert gmail.urls == []
    assert await rows(session_factory, TaskNotification) == []


async def test_a_lapsed_monitoring_authorization_ends_the_same_way(
    session_factory, calendar_settings, gmail
) -> None:
    """The deadline is still ahead, but the decision is older than seven
    days: the same terminal end, never a stranded task."""
    task_id = await monitor(session_factory, calendar_settings)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            authorized_at=datetime.now(timezone.utc)
            - timedelta(seconds=MAX_MONITOR_LIFETIME_SECONDS + 1),
            deadline=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        await session.commit()
    await run_due_tasks(session_factory, calendar_settings)
    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED and task.failure_count == 0
    assert ("task_cancelled", "expired") in [
        (e.event_type.value, (e.event_metadata or {}).get("reason")) for e in events
    ]
    assert gmail.urls == []


async def test_the_next_check_is_never_scheduled_past_the_deadline(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    deadline = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=120)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(deadline=deadline))
        await session.commit()
    await run_due_tasks(session_factory, calendar_settings)
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.RUNNING
    assert utc(task.next_run_at) == deadline        # not now + 15 minutes


async def test_a_failed_check_backs_off_but_not_past_the_deadline(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    deadline = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=90)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(deadline=deadline))
        await session.commit()
    gmail._status = 500
    await run_due_tasks(session_factory, calendar_settings)
    task, _ = await load(session_factory, task_id)
    assert task.failure_count == 1
    assert utc(task.next_run_at) == deadline


async def test_a_cancelled_monitor_stops_and_releases_its_grant(
    session_factory, calendar_settings, gmail
) -> None:
    task_id = await monitor(session_factory, calendar_settings)
    async with session_factory() as session:
        result = await MonitorRequestService(session, calendar_settings, OWNER).cancel(task_id)
        assert result.ok
        await session.commit()
    await make_due(session_factory, task_id)
    assert await run_due_tasks(session_factory, calendar_settings) == (0, 0, 0)
    assert gmail.urls == []
    [grant] = await rows(session_factory, ApprovalGrant)
    assert grant.revoked_at is not None


async def test_a_match_after_cancellation_cannot_complete_the_task(
    session_factory, calendar_settings, gmail
) -> None:
    """Cancelled while the check is reading Gmail: the cancellation commits
    first, so the in-flight check finds the task no longer active, completes
    nothing and notifies nobody -- although its own copy says `running`."""
    from app.tasks.runner import TaskRunner

    gmail.listing = gmail_list_payload(1)
    task_id = await monitor(session_factory, calendar_settings)
    async with session_factory() as session:
        runner = TaskRunner(TaskService(session, settings=calendar_settings, owner_id=OWNER))
        await runner.mark_task_running(task_id)
        answer = gmail.handle_async_request

        async def cancelled_meanwhile(request):
            # Another transaction's committed cancellation, behind this
            # session's back: the ORM copy is left stale on purpose.
            if urlparse(str(request.url)).path.endswith("/messages"):
                await session.execute(
                    update(Task).where(Task.id == task_id).values(
                        state=TaskState.CANCELLED, cancelled_at=datetime.now(timezone.utc),
                    ).execution_options(synchronize_session=False)
                )
            return await answer(request)

        gmail.handle_async_request = cancelled_meanwhile
        result = await runner.check(task_id)
        await session.commit()

    assert result.reason == "task_not_active"
    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED and task.completed_at is None
    assert await rows(session_factory, TaskNotification) == []
    assert ("runner_refused", "task_not_active") in [
        (e.event_type.value, (e.event_metadata or {}).get("reason")) for e in events
    ]


async def test_a_cancellation_after_completion_is_refused_as_terminal(
    session_factory, calendar_settings, gmail
) -> None:
    gmail.listing = gmail_list_payload(1)
    task_id = await monitor(session_factory, calendar_settings)
    await run_due_tasks(session_factory, calendar_settings)
    async with session_factory() as session:
        result = await MonitorRequestService(session, calendar_settings, OWNER).cancel(task_id)
    assert result.outcome.value == "terminal"
    [note] = await rows(session_factory, TaskNotification)
    assert note.task_id == task_id


# ============================================================================
# C. The lifetime rules, directly
# ============================================================================


class _Row:
    def __init__(self, monitor=None, deadline=None, authorized_at=None):
        self.monitor = monitor
        self.deadline = deadline
        self.authorized_at = authorized_at


def test_a_monitor_is_authorized_until_its_deadline_and_no_longer(settings) -> None:
    service = TaskService(None, settings=settings, owner_id=OWNER)
    now = datetime.now(timezone.utc)
    task = _Row({"x": 1}, deadline=now + timedelta(days=3), authorized_at=now - timedelta(hours=5))
    assert service.authorization_valid(task, now)                       # past 900 s
    assert service.authorization_valid(task, now + timedelta(days=3) - timedelta(seconds=1))
    assert not service.authorization_valid(task, now + timedelta(days=3))


def test_a_monitors_authorization_never_outlives_seven_days(settings) -> None:
    service = TaskService(None, settings=settings, owner_id=OWNER)
    now = datetime.now(timezone.utc)
    task = _Row({"x": 1}, deadline=now + timedelta(days=30), authorized_at=now)
    limit = now + timedelta(seconds=MAX_MONITOR_LIFETIME_SECONDS)
    assert service.authorization_valid(task, limit - timedelta(seconds=1))
    assert not service.authorization_valid(task, limit)


def test_every_other_task_keeps_the_900_second_window(settings) -> None:
    service = TaskService(None, settings=settings, owner_id=OWNER)
    now = datetime.now(timezone.utc)
    for task in (
        _Row(None, deadline=now + timedelta(days=3), authorized_at=now),   # not a monitor
        _Row({"x": 1}, deadline=None, authorized_at=now),                  # no deadline
    ):
        assert service.authorization_valid(task, now + timedelta(seconds=900))
        assert not service.authorization_valid(task, now + timedelta(seconds=901))
    assert settings.TASK_AUTHORIZATION_TTL_SECONDS == 900


def test_deadline_helpers_ignore_ordinary_tasks() -> None:
    now = datetime.now(timezone.utc)
    past = now - timedelta(seconds=1)
    assert deadline_passed(_Row({"x": 1}, deadline=past), now)
    assert deadline_passed(_Row({"x": 1}, deadline=now), now)
    assert not deadline_passed(_Row({"x": 1}, deadline=now + timedelta(seconds=1)), now)
    assert not deadline_passed(_Row(None, deadline=past), now)
    assert not deadline_passed(_Row({"x": 1}, deadline=None), now)
    later = now + timedelta(hours=1)
    assert clamp_to_deadline(_Row({"x": 1}, deadline=now), later) == now
    assert clamp_to_deadline(_Row({"x": 1}, deadline=later), now) == now
    assert clamp_to_deadline(_Row(None, deadline=now), later) == later
    assert clamp_to_deadline(_Row({"x": 1}, deadline=None), later) == later
    naive = now.replace(tzinfo=None)
    assert deadline_passed(_Row({"x": 1}, deadline=naive), now)
    assert clamp_to_deadline(_Row({"x": 1}, deadline=naive), later) == now


# ============================================================================
# D. The conditional completion covers ordinary tasks too
# ============================================================================


async def test_an_ordinary_task_cancelled_mid_step_is_not_reported_completed(
    calendar_runner
) -> None:
    """`advance` reports what the database says: a task cancelled while its
    last step ran is refused as no longer active, never "completed"."""
    from tests.test_task_runner import authorized_task, step

    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    transport = calendar_runner.transport
    answer = transport.handle_async_request

    async def cancelled_meanwhile(request):
        await db_session.execute(
            update(Task).where(Task.id == task_id).values(
                state=TaskState.CANCELLED, cancelled_at=datetime.now(timezone.utc),
            ).execution_options(synchronize_session=False)
        )
        return await answer(request)

    transport.handle_async_request = cancelled_meanwhile
    result = await runner.advance(task_id)
    assert result.outcome.value == "refused" and result.reason == "task_not_active"
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalars().one()
    await db_session.refresh(task)
    assert task.state is TaskState.CANCELLED and task.completed_at is None
