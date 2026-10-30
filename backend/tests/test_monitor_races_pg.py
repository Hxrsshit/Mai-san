"""Stage 6P: the monitoring races, on real PostgreSQL, with real connections.

SQLite has one writer and no row locks, so it cannot show these races. Each
test here gets its own throwaway database, created from
`TEST_POSTGRES_ADMIN_URL` (a URL to a maintenance database on a server where
the user may CREATE DATABASE), and drops it afterwards. The real `mai`
database is refused outright and never touched. Without the variable the
module is skipped, never silently passed.

Gmail's socket is stubbed (`tests.test_monitor_runtime.gmail`); every token is
a synthetic sentinel. Every assertion is about committed database state.

    TEST_POSTGRES_ADMIN_URL=postgresql+asyncpg://USER:PASS@127.0.0.1:5432/postgres
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update

from app.authorization.models import ApprovalGrant
from app.background.runtime import MAX_CONSECUTIVE_FAILURES, run_due_tasks
from app.core.owner import CONFIGURED_OWNER_ID
from app.execution.models import Execution
from app.services.conversation_service import ConversationService
from app.tasks.models import Task, TaskEvent, TaskNotification, TaskNotificationKind
from app.tasks.notifications import record_outcome
from app.tasks.requests import MonitorOutcome, MonitorRequestService
from app.tasks.runner import TaskRunner
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from tests.support.stub_transport import gmail_list_payload
from tests.test_monitor_runtime import gmail  # noqa: F401  (the stubbed Gmail path)
from tests.scopes import ADMIN_SCOPE

ADMIN_URL = os.environ.get("TEST_POSTGRES_ADMIN_URL", "")

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not ADMIN_URL, reason="TEST_POSTGRES_ADMIN_URL is not set"),
]

OWNER = CONFIGURED_OWNER_ID
REQUEST = "Watch my Gmail for an email from apple.com about invoice and tell me when it arrives."
OTHER_REQUEST = "Watch my Gmail for an email from google.com and tell me when it arrives."
#: How long a hooked transaction holds its lock so the other one must wait.
HOLD = 0.6


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


@pytest_asyncio.fixture
async def pg():
    """A fresh, uniquely named database on the server; dropped afterwards."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.main  # noqa: F401  (registers every model)
    from app.database.metadata import Base

    url = make_url(ADMIN_URL)
    assert url.get_backend_name() == "postgresql", "these races need PostgreSQL"
    assert url.database != "mai", "never the real database"
    name = f"mai_pgtest_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(url.set(database=name), pool_size=10)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


# --- helpers ------------------------------------------------------------------


def now():
    return datetime.now(timezone.utc)


async def new_conversation(F):
    async with F() as session:
        cid = (await ConversationService(session, ADMIN_SCOPE).create_conversation()).id
        await session.commit()
        return cid


async def say(F, settings, cid, message, owner=OWNER):
    async with F() as session:
        result = await MonitorRequestService(session, settings, owner).handle(cid, message)
        await session.commit()
        return result


async def cancel(F, settings, task_id):
    async with F() as session:
        result = await MonitorRequestService(session, settings, OWNER).cancel(task_id)
        await session.commit()
        return result


async def scheduled(F, settings, message=REQUEST):
    cid = await new_conversation(F)
    assert (await say(F, settings, cid, message)).outcome is MonitorOutcome.PROPOSED
    confirmed = await say(F, settings, cid, "yes")
    assert confirmed.outcome is MonitorOutcome.SCHEDULED
    return confirmed.task_id


async def row(F, task_id):
    async with F() as session:
        return (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()


async def count(F, model, *where):
    async with F() as session:
        query = select(func.count()).select_from(model)
        for condition in where:
            query = query.where(condition)
        return (await session.execute(query)).scalar_one()


async def live_grants(F):
    return await count(F, ApprovalGrant, ApprovalGrant.revoked_at.is_(None))


async def reasons(F, task_id, event="task_cancelled"):
    async with F() as session:
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.event_type == event)
        )).scalars().all()
        return [(e.event_metadata or {}).get("reason") for e in events]


async def make_due(F, task_id):
    async with F() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            next_run_at=now() - timedelta(seconds=1)))
        await session.commit()


def holding(monkeypatch, cls, name, entered):
    """Wrap a method so its transaction signals, then holds its lock a moment."""
    original = getattr(cls, name)

    async def held(self, *args, **kwargs):
        entered.set()
        await asyncio.sleep(HOLD)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, held)


# ============================================================================
# A. Confirmation against everything else a turn can do (6O M1)
# ============================================================================


async def test_confirmation_wins_against_a_replacement_and_nobody_is_misled(
    pg, calendar_settings, monkeypatch
) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    claimed = asyncio.Event()
    holding(monkeypatch, TaskService, "schedule_background", claimed)

    async def replace():
        await claimed.wait()                 # the confirmation holds the row
        return await say(F, settings, cid, OTHER_REQUEST)

    confirmed, replaced = await asyncio.gather(say(F, settings, cid, "yes"), replace())

    assert confirmed.outcome is MonitorOutcome.SCHEDULED
    assert confirmed.reply.startswith("Done. I'm monitoring")
    task = await row(F, first.task_id)
    assert task.state is TaskState.QUEUED and task.cancelled_at is None    # it stayed started
    assert await reasons(F, first.task_id) == []
    assert replaced.outcome is MonitorOutcome.PROPOSED
    assert replaced.reply.startswith("Your earlier proposal had just been confirmed")
    assert "cancelled the earlier proposal" not in replaced.reply
    assert await live_grants(F) == 1


async def test_a_replacement_that_wins_leaves_nothing_started(
    pg, calendar_settings, monkeypatch
) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    withdrawn = asyncio.Event()
    holding(monkeypatch, MonitorRequestService, "_propose", withdrawn)

    async def confirm():
        await withdrawn.wait()               # the replacement holds the row
        return await say(F, settings, cid, "yes")

    replaced, confirmed = await asyncio.gather(say(F, settings, cid, OTHER_REQUEST), confirm())

    assert replaced.reply.startswith("I've cancelled the earlier proposal.")
    assert confirmed.outcome in (MonitorOutcome.NOT_PENDING, MonitorOutcome.PROPOSED,
                                 MonitorOutcome.SCHEDULED)
    # Whatever the second turn confirmed, it was never the withdrawn proposal.
    task = await row(F, first.task_id)
    assert task.state is TaskState.CANCELLED and task.authorized_at is None
    assert await reasons(F, first.task_id) == ["proposal_replaced"]
    if confirmed.outcome is MonitorOutcome.NOT_PENDING:
        assert "nothing was started" in confirmed.reply
        assert await count(F, ApprovalGrant) == 0


async def test_a_decline_that_loses_to_a_confirmation_tells_the_truth(
    pg, calendar_settings, monkeypatch
) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    claimed = asyncio.Event()
    holding(monkeypatch, TaskService, "schedule_background", claimed)

    async def decline():
        await claimed.wait()
        return await say(F, settings, cid, "no")

    confirmed, declined = await asyncio.gather(say(F, settings, cid, "yes"), decline())
    assert confirmed.outcome is MonitorOutcome.SCHEDULED
    assert declined.outcome is MonitorOutcome.ALREADY_MONITORING
    assert declined.reply.startswith("That proposal had already been confirmed")
    assert (await row(F, first.task_id)).state is TaskState.QUEUED
    assert await live_grants(F) == 1


async def test_a_confirmation_that_loses_to_a_decline_starts_nothing(
    pg, calendar_settings, monkeypatch
) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    declining = asyncio.Event()
    original = TaskService.cancel

    async def held_cancel(self, task_id, reason=None):
        result = await original(self, task_id, reason=reason)   # row now locked
        declining.set()
        await asyncio.sleep(HOLD)
        return result

    monkeypatch.setattr(TaskService, "cancel", held_cancel)

    async def confirm():
        await declining.wait()
        return await say(F, settings, cid, "yes")

    declined, confirmed = await asyncio.gather(say(F, settings, cid, "no"), confirm())
    assert declined.outcome is MonitorOutcome.DECLINED
    assert confirmed.outcome in (MonitorOutcome.NOT_PENDING, MonitorOutcome.NOT_MONITOR)
    assert "Done" not in confirmed.reply
    task = await row(F, first.task_id)
    assert task.state is TaskState.CANCELLED and task.authorized_at is None
    assert await count(F, ApprovalGrant) == 0


async def test_two_confirmations_activate_exactly_once(pg, calendar_settings) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    results = await asyncio.gather(say(F, settings, cid, "yes"), say(F, settings, cid, "go ahead"))
    outcomes = sorted(r.outcome.value for r in results)
    assert outcomes in (["already_monitoring", "scheduled"], ["not_monitor", "scheduled"])
    assert await count(F, ApprovalGrant) == 1
    task = await row(F, first.task_id)
    assert task.state is TaskState.QUEUED
    async with F() as session:
        scheduled_events = (await session.execute(select(func.count()).select_from(TaskEvent).where(
            TaskEvent.task_id == first.task_id, TaskEvent.event_type == "background_scheduled"
        ))).scalar_one()
    assert scheduled_events == 1


async def test_two_replacements_withdraw_the_old_proposal_once(pg, calendar_settings) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    a, b = await asyncio.gather(
        say(F, settings, cid, OTHER_REQUEST),
        say(F, settings, cid, OTHER_REQUEST.replace("google.com", "stripe.com")),
    )
    # The original proposal is withdrawn exactly once, whatever the order.
    assert await reasons(F, first.task_id) == ["proposal_replaced"]
    assert {a.outcome, b.outcome} == {MonitorOutcome.PROPOSED}
    # Every "I've cancelled the earlier proposal" is true: if the two turns
    # ran one after the other, the second withdrew the first one's new
    # proposal -- and that withdrawal is in the journal too.
    async with F() as session:
        withdrawn = (await session.execute(select(func.count()).select_from(TaskEvent).where(
            TaskEvent.event_type == "task_cancelled",
        ))).scalar_one()
    claimed = sum(r.reply.startswith("I've cancelled the earlier proposal.") for r in (a, b))
    assert claimed >= 1 and claimed == withdrawn
    assert await count(F, ApprovalGrant) == 0


async def test_cancelling_during_activation_waits_then_cancels(
    pg, calendar_settings, monkeypatch
) -> None:
    F, settings = pg, calendar_settings
    cid = await new_conversation(F)
    first = await say(F, settings, cid, REQUEST)
    claimed = asyncio.Event()
    holding(monkeypatch, TaskService, "schedule_background", claimed)

    async def cancel_it():
        await claimed.wait()
        return await cancel(F, settings, first.task_id)

    confirmed, cancelled = await asyncio.gather(say(F, settings, cid, "yes"), cancel_it())
    assert confirmed.outcome is MonitorOutcome.SCHEDULED
    assert cancelled.ok                       # waited for the activation, then won
    task = await row(F, first.task_id)
    assert task.state is TaskState.CANCELLED
    assert await live_grants(F) == 0


# ============================================================================
# B. Cancellation against the runtime
# ============================================================================


async def test_cancel_between_claim_and_start_wins(
    pg, calendar_settings, gmail, monkeypatch, caplog  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    task_id = await scheduled(F, settings)
    original = TaskRunner.mark_task_running

    async def cancelled_first(self, tid):
        assert (await cancel(F, settings, tid)).ok            # another connection
        return await original(self, tid)

    monkeypatch.setattr(TaskRunner, "mark_task_running", cancelled_first)
    await run_due_tasks(F, settings)

    task = await row(F, task_id)
    assert task.state is TaskState.CANCELLED and task.check_count == 0
    assert gmail.urls == [] and await count(F, Execution) == 0
    assert await count(F, TaskNotification) == 0
    assert "Background task pass failed" not in caplog.text


async def test_cancel_before_the_check_claim_means_no_gmail_read(
    pg, calendar_settings, gmail, monkeypatch  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(F, settings)
    # Already running (a later check), so this transaction holds no lock
    # before its claim -- the window a concurrent cancel can commit in. On a
    # first check, `mark_task_running` locks the row first and a cancel simply
    # waits (covered above and below).
    async with F() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(state=TaskState.RUNNING))
        await session.commit()
    original = TaskRunner._claim_check

    async def cancelled_first(self, task, sequence):
        assert (await cancel(F, settings, task.id)).ok
        return await original(self, task, sequence)

    monkeypatch.setattr(TaskRunner, "_claim_check", cancelled_first)
    await run_due_tasks(F, settings)
    task = await row(F, task_id)
    assert task.state is TaskState.CANCELLED and task.completed_at is None
    assert gmail.urls == [] and await count(F, Execution) == 0
    assert await count(F, TaskNotification) == 0


async def test_cancel_during_the_gmail_read_waits_and_the_completion_stands(
    pg, calendar_settings, gmail  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(F, settings)
    answer = gmail.handle_async_request
    reading, release = asyncio.Event(), asyncio.Event()

    async def held(request):
        if request.url.path.endswith("/messages"):
            reading.set()
            await release.wait()
        return await answer(request)

    gmail.handle_async_request = held

    async def cancel_it():
        await reading.wait()
        pending = asyncio.ensure_future(cancel(F, settings, task_id))
        await asyncio.sleep(HOLD)
        waited = not pending.done()
        release.set()
        return waited, await pending

    _, (waited, cancelled) = await asyncio.gather(run_due_tasks(F, settings), cancel_it())
    task = await row(F, task_id)
    assert waited
    assert task.state is TaskState.COMPLETED and task.cancelled_at is None
    assert cancelled.outcome.value == "terminal"
    assert await count(F, TaskNotification, TaskNotification.task_id == task_id) == 1
    assert await live_grants(F) == 0                       # released on completion


async def test_a_failure_counted_after_a_cancel_changes_nothing(
    pg, calendar_settings, gmail  # noqa: F811
) -> None:
    """The check failed and committed; the owner cancelled; then the runtime
    counts the failure -- on the third failure, the one that would block."""
    from app.background.runtime import _record_failure

    F, settings = pg, calendar_settings
    task_id = await scheduled(F, settings)
    async with F() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            state=TaskState.RUNNING, failure_count=MAX_CONSECUTIVE_FAILURES - 1))
        await session.commit()
    assert (await cancel(F, settings, task_id)).ok
    await _record_failure(F, settings, task_id, OWNER, now())
    task = await row(F, task_id)
    # Not counted, not blocked, not rescheduled: the failure write matched
    # nothing. (A cancelled task's stale `next_run_at` is never claimed --
    # the claim requires an active state.)
    assert task.state is TaskState.CANCELLED
    assert task.failure_count == MAX_CONSECUTIVE_FAILURES - 1
    assert await run_due_tasks(F, settings) == (0, 0, 0)
    assert await count(F, TaskNotification) == 0
    assert await reasons(F, task_id, "task_blocked") == []


# ============================================================================
# C. Stale writes against terminal states (Known defect 8)
# ============================================================================


@pytest.mark.parametrize("terminal", ["cancelled", "completed"])
async def test_a_stale_transition_never_reopens_a_terminal_task(
    pg, calendar_settings, gmail, terminal  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    if terminal == "completed":
        gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(F, settings)
    async with F() as stale:                             # reads it while active
        service = TaskService(stale, settings=settings, owner_id=OWNER)
        copy = await service.get(task_id)
        assert copy.state is TaskState.QUEUED
        if terminal == "cancelled":
            assert (await cancel(F, settings, task_id)).ok
        else:
            await run_due_tasks(F, settings)
        result = await service.transition(task_id, TaskState.BLOCKED, actor="system", reason="late")
        await stale.commit()
    assert result.reason == "state_changed_concurrently"
    assert result.outcome.value == "terminal"
    task = await row(F, task_id)
    assert task.state.value == terminal
    assert await reasons(F, task_id, "task_blocked") == []


# ============================================================================
# D. Grants (6O M2)
# ============================================================================


async def test_grants_are_released_on_completion_and_on_block(
    pg, calendar_settings, gmail  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    gmail.listing = gmail_list_payload(1)
    done = await scheduled(F, settings)
    await run_due_tasks(F, settings)
    assert (await row(F, done)).state is TaskState.COMPLETED
    assert await live_grants(F) == 0

    gmail.listing = {"resultSizeEstimate": 0}
    gmail._status = 500
    failing = await scheduled(F, settings, REQUEST.replace("invoice", "receipt"))
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        await make_due(F, failing)
        await run_due_tasks(F, settings)
    assert (await row(F, failing)).state is TaskState.BLOCKED
    assert await count(F, TaskNotification, TaskNotification.task_id == failing,
                       TaskNotification.kind == TaskNotificationKind.MONITORING_FAILED) == 1
    assert await live_grants(F) == 0


async def test_a_grant_another_active_monitor_needs_is_kept(
    pg, calendar_settings, gmail  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    keeper = await scheduled(F, settings, REQUEST.replace("invoice", "receipt"))
    async with F() as session:
        await session.execute(update(Task).where(Task.id == keeper).values(
            next_run_at=now() + timedelta(hours=1)))
        await session.commit()
    gmail.listing = gmail_list_payload(1)
    done = await scheduled(F, settings)
    await run_due_tasks(F, settings)
    assert (await row(F, done)).state is TaskState.COMPLETED
    assert await live_grants(F) == 2
    assert (await cancel(F, settings, keeper)).ok
    assert await live_grants(F) == 0


# ============================================================================
# E. Notifications
# ============================================================================


async def test_two_writers_record_one_notification(
    pg, calendar_settings, gmail  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(F, settings)
    await run_due_tasks(F, settings)
    async with F() as session:                          # forget it, keep the outcome
        await session.execute(TaskNotification.__table__.delete())
        await session.commit()

    async def write():
        async with F() as session:
            task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
            note = await record_outcome(session, task, TaskNotificationKind.CONDITION_MET)
            await session.commit()
            return note is not None

    written = await asyncio.gather(write(), write())
    assert sorted(written) == [False, True]
    assert await count(F, TaskNotification) == 1


async def test_a_cancelled_monitor_is_never_notified(
    pg, calendar_settings, gmail  # noqa: F811
) -> None:
    F, settings = pg, calendar_settings
    gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(F, settings)
    assert (await cancel(F, settings, task_id)).ok
    await make_due(F, task_id)
    await run_due_tasks(F, settings)
    async with F() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        for kind in TaskNotificationKind:
            assert await record_outcome(session, task, kind) is None
        await session.commit()
    assert await count(F, TaskNotification) == 0
    assert gmail.urls == []
