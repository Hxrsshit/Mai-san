"""Stage 6P: task-state concurrency, grant cleanup and truthful race replies.

In-memory SQLite, one connection. A "concurrent" change is simulated the way
it looks to the code under test: a committed row that differs from the copy
the code already holds, written with `synchronize_session=False` so that copy
stays stale. Real two-connection races are in `test_monitor_races_pg.py`.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.authorization.models import ApprovalGrant
from app.background.runtime import MAX_CONSECUTIVE_FAILURES, run_due_tasks
from app.core.owner import CONFIGURED_OWNER_ID
from app.execution.models import Execution
from app.services.conversation_service import ConversationService
from app.tasks.models import Task, TaskEvent, TaskNotification, TaskNotificationKind
from app.tasks.requests import MonitorOutcome, MonitorRequestService
from app.tasks.runner import TaskRunner
from app.tasks.schemas import TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from tests.support.stub_transport import gmail_list_payload
from tests.test_monitor_runtime import gmail  # noqa: F401  (the stubbed Gmail path)
from tests.scopes import ADMIN_SCOPE

pytestmark = pytest.mark.anyio

OWNER = CONFIGURED_OWNER_ID
REQUEST = (
    "Watch my Gmail for an email from apple.com about invoice and tell me "
    "when it arrives."
)


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


def now():
    return datetime.now(timezone.utc)


async def conversation(session):
    return (await ConversationService(session, ADMIN_SCOPE).create_conversation()).id


def monitors(session, settings, owner=OWNER):
    return MonitorRequestService(session, settings=settings, owner_id=owner)


async def behind_the_back(session, task_id, **values):
    """A committed change this session's copy does not know about."""
    await session.execute(
        update(Task).where(Task.id == task_id).values(**values)
        .execution_options(synchronize_session=False)
    )


async def scheduled(session_factory, settings, message=REQUEST):
    async with session_factory() as session:
        conv = await conversation(session)
        await monitors(session, settings).handle(conv, message)
        await session.commit()
    async with session_factory() as session:
        result = await monitors(session, settings).handle(conv, "yes")
        assert result.outcome is MonitorOutcome.SCHEDULED
        await session.commit()
    return result.task_id


async def load(session_factory, task_id):
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.sequence)
        )).scalars().all()
        return task, [(e.event_type.value, (e.event_metadata or {}).get("reason")) for e in events]


async def count(session_factory, model, *where):
    async with session_factory() as session:
        query = select(func.count()).select_from(model)
        for condition in where:
            query = query.where(condition)
        return (await session.execute(query)).scalar_one()


async def live_grants(session_factory):
    return await count(session_factory, ApprovalGrant, ApprovalGrant.revoked_at.is_(None))


# ============================================================================
# A. Known defect 8: a stale copy can no longer overwrite newer state
# ============================================================================


@pytest.mark.parametrize("newer, values", [
    (TaskState.CANCELLED, {"cancelled_at": None}),
    (TaskState.COMPLETED, {"completed_at": None}),
    (TaskState.BLOCKED, {}),
])
async def test_a_stale_transition_writes_nothing_and_says_so(
    db_session, settings, newer, values
) -> None:
    service = TaskService(db_session, settings=settings, owner_id=OWNER)
    created = await service.create_for_user("Something to do")
    task = await service.get(created.task_id)          # this session's copy: proposed
    stamped = {key: now() for key in values}
    await behind_the_back(db_session, created.task_id, state=newer, **stamped)

    result = await service.transition(created.task_id, TaskState.CANCELLED, reason="late")

    assert result.reason == "state_changed_concurrently"
    assert result.state is newer
    assert result.outcome is (
        TaskOutcome.TERMINAL if newer in (TaskState.CANCELLED, TaskState.COMPLETED)
        else TaskOutcome.INVALID_TRANSITION
    )
    assert task.state is newer                          # the copy was refreshed
    fresh = (await db_session.execute(
        select(Task.state).where(Task.id == created.task_id)
    )).scalar_one()
    assert fresh is newer
    events = (await db_session.execute(
        select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == created.task_id)
    )).scalar_one()
    assert events == 1                                  # only `task_created`


async def test_a_current_transition_still_succeeds(db_session, settings) -> None:
    service = TaskService(db_session, settings=settings, owner_id=OWNER)
    created = await service.create_for_user("Something to do")
    result = await service.cancel(created.task_id, reason="mine")
    assert result.outcome is TaskOutcome.CANCELLED
    task = await service.get(created.task_id)
    assert task.state is TaskState.CANCELLED and task.cancelled_at is not None


async def test_a_stale_authorization_writes_nothing(db_session, settings) -> None:
    conv = await conversation(db_session)
    proposal = await monitors(db_session, settings).handle(conv, REQUEST)
    service = TaskService(db_session, settings=settings, owner_id=OWNER)
    stale = await service.get_detail(proposal.task_id)
    await behind_the_back(db_session, proposal.task_id, state=TaskState.CANCELLED, cancelled_at=now())

    async def the_copy_read_before(task_id):
        return stale                        # still says: planned, unauthorised

    service.get_detail = the_copy_read_before
    assert stale.state is TaskState.PLANNED
    result = await service.authorize_plan(proposal.task_id)
    assert result.reason == "state_changed_concurrently"
    task = await service.refresh(proposal.task_id)
    assert task.state is TaskState.CANCELLED and task.authorized_at is None


async def test_mark_running_never_reopens_a_cancelled_task(db_session, settings) -> None:
    conv = await conversation(db_session)
    await monitors(db_session, settings).handle(conv, REQUEST)
    confirmed = await monitors(db_session, settings).handle(conv, "yes")
    service = TaskService(db_session, settings=settings, owner_id=OWNER)
    task = await service.get(confirmed.task_id)
    assert task.state is TaskState.QUEUED
    await behind_the_back(db_session, task.id, state=TaskState.CANCELLED, cancelled_at=now())

    assert await TaskRunner(service).mark_task_running(task.id) is False
    assert task.state is TaskState.CANCELLED            # refreshed, not stale


# ============================================================================
# B. The runtime: a cancellation that lands mid-flight wins, quietly
# ============================================================================


async def test_cancel_between_claim_and_start_means_no_read_and_no_error(
    session_factory, calendar_settings, gmail, monkeypatch, caplog  # noqa: F811
) -> None:
    task_id = await scheduled(session_factory, calendar_settings)
    original = TaskRunner.mark_task_running

    async def cancelled_first(self, tid):
        await behind_the_back(self._session, tid, state=TaskState.CANCELLED, cancelled_at=now())
        return await original(self, tid)

    monkeypatch.setattr(TaskRunner, "mark_task_running", cancelled_first)
    await run_due_tasks(session_factory, calendar_settings)

    # The simulated cancel shares this one SQLite transaction, which the
    # runtime leaves uncommitted when it stops -- so the row is back to
    # queued here. What matters: it was never marked running or read.
    # (The committed two-connection version is in the PostgreSQL tests.)
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.QUEUED and task.check_count == 0
    assert gmail.urls == []
    assert await count(session_factory, Execution) == 0
    assert "Background task pass failed" not in caplog.text


async def test_cancel_before_the_check_claim_means_no_read(
    session_factory, calendar_settings, gmail, monkeypatch  # noqa: F811
) -> None:
    task_id = await scheduled(session_factory, calendar_settings)
    original = TaskRunner._claim_check

    async def cancelled_first(self, task, sequence):
        await behind_the_back(self._session, task.id, state=TaskState.CANCELLED, cancelled_at=now())
        return await original(self, task, sequence)

    monkeypatch.setattr(TaskRunner, "_claim_check", cancelled_first)
    await run_due_tasks(session_factory, calendar_settings)

    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED and task.check_count == 0
    assert gmail.urls == []
    assert await count(session_factory, Execution) == 0
    # The journal says why the runtime stopped: the task was no longer
    # active -- not "another worker held it".
    assert events[-1] == ("background_unscheduled", "task_not_active")


async def test_a_cancelled_task_is_never_rescheduled(
    session_factory, calendar_settings, gmail, monkeypatch  # noqa: F811
) -> None:
    """The check ran; the task was cancelled before the runtime rescheduled it."""
    from app.background import runtime

    task_id = await scheduled(session_factory, calendar_settings)
    original = runtime._reschedule

    async def cancelled_first(session, tid, result, moment):
        await behind_the_back(session, tid, state=TaskState.CANCELLED, cancelled_at=now())
        return await original(session, tid, result, moment)

    monkeypatch.setattr(runtime, "_reschedule", cancelled_first)
    moment = now()
    await run_due_tasks(session_factory, calendar_settings, now=moment)
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED
    # Still the claim's lease, never the next interval.
    lease = task.next_run_at.replace(tzinfo=timezone.utc)
    assert lease < moment + timedelta(seconds=600)


async def test_failure_bookkeeping_skips_a_cancelled_task(
    session_factory, calendar_settings, gmail  # noqa: F811
) -> None:
    from app.background.runtime import _record_failure

    task_id = await scheduled(session_factory, calendar_settings)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            state=TaskState.CANCELLED, cancelled_at=now(),
            failure_count=MAX_CONSECUTIVE_FAILURES - 1,
        ))
        await session.commit()
    await _record_failure(session_factory, calendar_settings, task_id, OWNER, now())
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.CANCELLED
    assert task.failure_count == MAX_CONSECUTIVE_FAILURES - 1
    assert await count(session_factory, TaskNotification) == 0


async def test_failure_bookkeeping_loses_quietly_to_a_concurrent_cancel(
    session_factory, calendar_settings, gmail, monkeypatch  # noqa: F811
) -> None:
    """Cancelled after the failure was read, before it was blocked: nothing is
    written, nobody is told the monitor gave up, and nothing raises."""
    from app.background.runtime import _record_failure

    task_id = await scheduled(session_factory, calendar_settings)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(
            state=TaskState.RUNNING, failure_count=MAX_CONSECUTIVE_FAILURES - 1,
        ))
        await session.commit()
    original = TaskService.transition

    async def cancelled_first(self, tid, target, actor="user", reason=None):
        if target is TaskState.BLOCKED:
            await behind_the_back(self._session, tid, state=TaskState.CANCELLED, cancelled_at=now())
        return await original(self, tid, target, actor=actor, reason=reason)

    monkeypatch.setattr(TaskService, "transition", cancelled_first)
    await _record_failure(session_factory, calendar_settings, task_id, OWNER, now())
    # The whole failure transaction -- which on SQLite also carried the
    # simulated cancel -- was rolled back: nothing was counted, nothing was
    # blocked, nobody was notified, and nothing raised.
    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.RUNNING
    assert task.failure_count == MAX_CONSECUTIVE_FAILURES - 1
    assert await count(session_factory, TaskNotification) == 0
    assert ("task_blocked", "background_failures_exhausted") not in events


async def test_a_failure_write_loses_to_a_cancel_landing_after_its_read(
    session_factory, calendar_settings, gmail, monkeypatch  # noqa: F811
) -> None:
    """The cancel lands between `_record_failure`'s read and its write. The
    write must match nothing: no count, no schedule, on a cancelled task."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from app.background.runtime import _record_failure

    task_id = await scheduled(session_factory, calendar_settings)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(state=TaskState.RUNNING))
        await session.commit()
    original = AsyncSession.execute
    armed = {"on": True}

    async def read_then_cancelled(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        if armed["on"] and isinstance(statement, Select) and "FROM tasks" in str(statement):
            armed["on"] = False
            await original(self, update(Task).where(Task.id == task_id).values(
                state=TaskState.CANCELLED, cancelled_at=now(),
            ).execution_options(synchronize_session=False))
        return result

    monkeypatch.setattr(AsyncSession, "execute", read_then_cancelled)
    await _record_failure(session_factory, calendar_settings, task_id, OWNER, now())
    monkeypatch.setattr(AsyncSession, "execute", original)
    task, _ = await load(session_factory, task_id)
    # Rolled back as a whole (on SQLite the simulated cancel went with it):
    # crucially, no failure was counted on the task.
    assert task.failure_count == 0


async def test_expiry_that_lost_to_a_cancel_journals_nothing_of_its_own(
    db_session, settings
) -> None:
    from app.background.runtime import _expire_monitor

    conv = await conversation(db_session)
    await monitors(db_session, settings).handle(conv, REQUEST)
    confirmed = await monitors(db_session, settings).handle(conv, "yes")
    service = TaskService(db_session, settings=settings, owner_id=OWNER)
    await service.get(confirmed.task_id)                # a copy that says queued
    await behind_the_back(db_session, confirmed.task_id, state=TaskState.CANCELLED, cancelled_at=now())
    await _expire_monitor(db_session, settings, confirmed.task_id, OWNER)
    events = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.task_id == confirmed.task_id)
    )).scalars().all()
    reasons = [(e.event_type.value, (e.event_metadata or {}).get("reason")) for e in events]
    assert ("background_unscheduled", "monitor_expired") not in reasons
    assert ("task_cancelled", "expired") not in reasons


# ============================================================================
# C. Grant cleanup (6O M2)
# ============================================================================


async def test_a_completed_monitor_releases_its_grant(
    session_factory, calendar_settings, gmail  # noqa: F811
) -> None:
    gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(session_factory, calendar_settings)
    await run_due_tasks(session_factory, calendar_settings)
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert await live_grants(session_factory) == 0
    async with session_factory() as session:
        [grant] = (await session.execute(select(ApprovalGrant))).scalars().all()
    assert grant.revoked_reason == "monitor_ended"


async def test_a_completed_monitor_keeps_a_grant_another_monitor_needs(
    session_factory, calendar_settings, gmail  # noqa: F811
) -> None:
    other = await scheduled(session_factory, calendar_settings, REQUEST.replace("invoice", "receipt"))
    async with session_factory() as session:            # active, not due now
        await session.execute(update(Task).where(Task.id == other).values(
            next_run_at=now() + timedelta(hours=1)))
        await session.commit()
    gmail.listing = gmail_list_payload(1)
    task_id = await scheduled(session_factory, calendar_settings)
    await run_due_tasks(session_factory, calendar_settings)
    task, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert await live_grants(session_factory) == 2


async def test_a_blocked_monitor_releases_its_grant(
    session_factory, calendar_settings, gmail  # noqa: F811
) -> None:
    gmail._status = 500
    task_id = await scheduled(session_factory, calendar_settings)
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        async with session_factory() as session:
            await session.execute(update(Task).where(Task.id == task_id).values(
                next_run_at=now() - timedelta(seconds=1)))
            await session.commit()
        await run_due_tasks(session_factory, calendar_settings)
    task, events = await load(session_factory, task_id)
    assert task.state is TaskState.BLOCKED
    assert await count(session_factory, TaskNotification,
                       TaskNotification.kind == TaskNotificationKind.MONITORING_FAILED) == 1
    assert await live_grants(session_factory) == 0


async def test_a_running_monitor_keeps_its_grant(
    session_factory, calendar_settings, gmail  # noqa: F811
) -> None:
    """A check that does not match releases nothing."""
    await scheduled(session_factory, calendar_settings)
    await run_due_tasks(session_factory, calendar_settings)
    assert await live_grants(session_factory) == 1


# ============================================================================
# D. Truthful replies when a concurrent turn answered first (6O M1)
# ============================================================================


async def stale_pending(db_session, settings):
    """A proposal as one turn saw it, then confirmed by another turn."""
    conv = await conversation(db_session)
    proposal = await monitors(db_session, settings).handle(conv, REQUEST)
    pending, _, _ = await monitors(db_session, settings)._pending_for(conv)
    confirmed = await monitors(db_session, settings).handle(conv, "yes")
    assert confirmed.outcome is MonitorOutcome.SCHEDULED
    return conv, proposal.task_id, pending


@pytest.mark.parametrize("answer, outcome", [
    ("no", MonitorOutcome.ALREADY_MONITORING),
    ("yes, every hour", MonitorOutcome.ALREADY_MONITORING),
])
async def test_an_answer_that_lost_the_race_tells_the_truth(
    db_session, settings, monkeypatch, answer, outcome
) -> None:
    conv, task_id, pending = await stale_pending(db_session, settings)

    async def as_seen_before(self, conversation_id):
        return pending, [], []

    monkeypatch.setattr(MonitorRequestService, "_pending_for", as_seen_before)
    result = await monitors(db_session, settings).handle(conv, answer)
    assert result.outcome is outcome
    assert result.reply.startswith("That proposal had already been confirmed")
    task = await TaskService(db_session, settings=settings, owner_id=OWNER).refresh(task_id)
    assert task.state is TaskState.QUEUED                # the confirmation stands
    assert (await db_session.execute(
        select(func.count()).select_from(ApprovalGrant).where(ApprovalGrant.revoked_at.is_(None))
    )).scalar_one() == 1


async def test_a_replacement_that_lost_the_race_keeps_the_monitor_and_says_so(
    db_session, settings, monkeypatch
) -> None:
    conv, task_id, pending = await stale_pending(db_session, settings)

    async def as_seen_before(self, conversation_id):
        return pending, [], []

    monkeypatch.setattr(MonitorRequestService, "_pending_for", as_seen_before)
    result = await monitors(db_session, settings).handle(
        conv, "watch my gmail for an email from google.com and tell me when it arrives"
    )
    assert result.outcome is MonitorOutcome.PROPOSED and result.task_id != task_id
    assert result.reply.startswith(
        "Your earlier proposal had just been confirmed, so that monitor is active. I can set this up."
    )
    assert "cancelled the earlier proposal" not in result.reply
    task = await TaskService(db_session, settings=settings, owner_id=OWNER).refresh(task_id)
    assert task.state is TaskState.QUEUED


async def test_a_confirmation_that_lost_the_race_starts_nothing_and_says_so(
    db_session, settings
) -> None:
    conv = await conversation(db_session)
    await monitors(db_session, settings).handle(conv, REQUEST)
    pending, _, _ = await monitors(db_session, settings)._pending_for(conv)
    declined = await monitors(db_session, settings).handle(conv, "no")
    assert declined.outcome is MonitorOutcome.DECLINED

    result = await monitors(db_session, settings)._confirm(pending)
    assert result.outcome is MonitorOutcome.NOT_PENDING
    assert "nothing was started" in result.reply
    assert "Done" not in result.reply
    assert (await db_session.execute(select(func.count()).select_from(ApprovalGrant))).scalar_one() == 0


async def test_a_withdrawal_never_touches_an_activated_monitor(db_session, settings) -> None:
    conv, task_id, pending = await stale_pending(db_session, settings)
    assert await monitors(db_session, settings)._discard(pending, "proposal_replaced") is False
    task = await TaskService(db_session, settings=settings, owner_id=OWNER).refresh(task_id)
    assert task.state is TaskState.QUEUED and task.cancelled_at is None
