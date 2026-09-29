"""Stage 6F: the one background runtime.

Every behavioural test drives the real path: the runtime's discovery and
claim against the database, the real `TaskRunner`, the real authorization
service, and the real dispatcher running `list_workspace_files` against a
temporary workspace. Nothing on that path is mocked.

`list_workspace_files` is LOW risk and needs a person, so it exercises the
Stage 6E boundary directly: without a standing grant the runtime must wait,
and with one it may proceed.

Time is injected. No test sleeps to prove a scheduling decision.

A note on the fixture. `session_factory` is an in-memory SQLite database on
a `StaticPool`, so "separate sessions" here share one connection. These
tests prove the conditional-UPDATE *logic*; true isolation between
concurrent connections is proven against live PostgreSQL.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.authorization.grants import GrantService
from app.background.runtime import (
    CLAIM_LEASE_SECONDS,
    CONTENTION_BACKOFF_SECONDS,
    FAILURE_BACKOFF_SECONDS,
    HARD_MAX_TASKS_PER_TICK,
    BackgroundRuntime,
    claim_task,
    discover_due_tasks,
    run_due_tasks,
    tasks_per_tick,
)
from app.execution.models import Execution
from app.execution.states import ExecutionState
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks.models import MAX_CONSECUTIVE_FAILURES, Task, TaskEvent, TaskStep
from app.tasks.schemas import TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState
from app.tools.schemas import RiskLevel

pytestmark = pytest.mark.anyio

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")
WS = "list_workspace_files"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


@pytest.fixture
def now() -> datetime:
    """Close to the real clock, because the runner checks authorization and
    grant expiry against it; every scheduling decision is relative to this."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def goal() -> Goal:
    return Goal(summary="List the workspace", source_intent=IntentType.ACTION)


def step(key, order, deps=(), capability=WS, arguments=None) -> PlanTask:
    return PlanTask(
        id=key, title=f"Step {key}", dependencies=list(deps), order=order,
        depth=len(deps), capability=capability,
        arguments={} if arguments is None else arguments,
    )


async def scheduled_task(
    session_factory, settings, *steps, owner=OWNER, at=None, grant=False
):
    """A task a person has planned, approved, queued and scheduled."""
    async with session_factory() as session:
        service = TaskService(session, settings=settings, owner_id=owner)
        created = await service.create_for_user("List the workspace")
        assert (await service.attach_plan(
            created.task_id, Plan(goal=goal(), tasks=list(steps or [step("a", 1)]))
        )).ok
        await service.authorize_plan(created.task_id)
        # The plan-level approval is a person's transition.
        await service.transition(created.task_id, TaskState.QUEUED, actor="user")
        if grant:
            await GrantService(session, owner_id=owner).create(WS, RiskLevel.LOW)
        result = await service.schedule_background(created.task_id, now=at)
        assert result.ok, result
        await session.commit()
        return created.task_id


async def load(session_factory, task_id):
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        steps = (await session.execute(
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.sequence)
        )).scalars().all()
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.sequence)
        )).scalars().all()
        return task, steps, [e.event_type.value for e in events]


async def count(session_factory, model, *where):
    async with session_factory() as session:
        return (await session.execute(
            select(func.count()).select_from(model).where(*where)
        )).scalar()


def utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


# ============================================================================
# A. Discovery
# ============================================================================


async def test_a_due_task_is_discovered(session_factory, execution_settings, now) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now)
    assert await discover_due_tasks(session_factory, now, 10) == [(task_id, OWNER)]


async def test_a_future_task_is_ignored(session_factory, execution_settings, now) -> None:
    later = now + timedelta(minutes=5)
    await scheduled_task(session_factory, execution_settings, at=later)

    assert await discover_due_tasks(session_factory, now, 10) == []
    # Just before, exactly at, just after.
    assert await discover_due_tasks(session_factory, later - timedelta(seconds=1), 10) == []
    assert len(await discover_due_tasks(session_factory, later, 10)) == 1
    assert len(await discover_due_tasks(session_factory, later + timedelta(seconds=1), 10)) == 1


async def test_an_unscheduled_task_is_never_discovered(
    session_factory, execution_settings, now
) -> None:
    """A task nobody asked to run unattended is not background work."""
    async with session_factory() as session:
        service = TaskService(session, settings=execution_settings)
        created = await service.create_for_user("Not scheduled")
        await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[step("a", 1)]))
        await service.authorize_plan(created.task_id)
        await service.transition(created.task_id, TaskState.QUEUED)
        await session.commit()

    assert await discover_due_tasks(session_factory, now + timedelta(days=1), 10) == []


async def test_the_empty_queue_is_a_quiet_tick(session_factory, execution_settings, now) -> None:
    assert await run_due_tasks(session_factory, execution_settings, now=now) == (0, 0, 0)


# ============================================================================
# B. Claiming
# ============================================================================


async def test_a_task_is_claimed_exactly_once(session_factory, execution_settings, now) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now)

    assert await claim_task(session_factory, task_id, OWNER, now) is True
    assert await claim_task(session_factory, task_id, OWNER, now) is False

    task, _, events = await load(session_factory, task_id)
    # Moved to the lease horizon -- written as a literal offset.
    assert utc(task.next_run_at) == now + timedelta(seconds=300)
    assert events.count("background_claimed") == 1


async def test_two_pollers_produce_one_claim(session_factory, execution_settings, now) -> None:
    """Two runtimes, each with its own sessions, racing for one due task.

    Run one after the other rather than with `gather`: this fixture's SQLite
    database sits on a single `StaticPool` connection, which cannot hold two
    transactions at once, so a concurrent attempt fails in the driver before
    it reaches the claim. What this proves is the conditional UPDATE -- the
    second claim finds the task no longer due. Genuine concurrency, on
    separate connections, is verified against live PostgreSQL.
    """
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)

    first = BackgroundRuntime(session_factory, execution_settings)
    second = BackgroundRuntime(session_factory, execution_settings)
    a = await first.tick(now=now)
    b = await second.tick(now=now)

    assert (a.tasks_claimed, b.tasks_claimed) == (1, 0)
    assert await count(session_factory, Execution) == 1


async def test_a_claim_needs_the_right_owner(session_factory, execution_settings, now) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now)
    assert await claim_task(session_factory, task_id, OTHER, now) is False
    assert await claim_task(session_factory, task_id, OWNER, now) is True


async def test_an_expired_claim_becomes_due_again(
    session_factory, execution_settings, now
) -> None:
    """A process that dies holding a claim strands the task only for a lease."""
    task_id = await scheduled_task(session_factory, execution_settings, at=now)
    assert await claim_task(session_factory, task_id, OWNER, now)

    during = now + timedelta(seconds=299)
    after = now + timedelta(seconds=300)
    assert await discover_due_tasks(session_factory, during, 10) == []
    assert await discover_due_tasks(session_factory, after, 10) == [(task_id, OWNER)]


# ============================================================================
# C. Execution goes through the runner and the authorization service
# ============================================================================


async def test_without_a_grant_the_task_waits_for_a_person(
    session_factory, execution_settings, now
) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now)

    assert await run_due_tasks(session_factory, execution_settings, now=now) == (1, 1, 0)

    task, steps, events = await load(session_factory, task_id)
    assert task.next_run_at is None, "a task waiting for a person is still polled"
    assert steps[0].state is TaskStepState.PENDING
    assert "runner_blocked" in events
    assert events[-1] == "background_unscheduled"


async def test_an_awaiting_approval_task_is_not_hammered(
    session_factory, execution_settings, now
) -> None:
    """Approval starvation: waiting must cost nothing."""
    task_id = await scheduled_task(session_factory, execution_settings, at=now)

    for minutes in (0, 1, 5, 60):
        await run_due_tasks(
            session_factory, execution_settings, now=now + timedelta(minutes=minutes)
        )

    _, _, events = await load(session_factory, task_id)
    assert events.count("background_claimed") == 1
    assert await count(session_factory, Execution, Execution.tool_name == WS) == 1


async def test_a_standing_grant_permits_unattended_execution(
    session_factory, execution_settings, now
) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)

    assert await run_due_tasks(session_factory, execution_settings, now=now) == (1, 1, 1)

    task, steps, events = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert task.next_run_at is None
    assert steps[0].state is TaskStepState.COMPLETED
    assert "standing_grant_used" in events

    async with session_factory() as session:
        execution = (await session.execute(select(Execution))).scalars().one()
    # Through the real envelope: approved, fingerprinted, dispatched.
    assert execution.state is ExecutionState.SUCCEEDED
    assert execution.approved_fingerprint is not None


async def test_an_expired_grant_does_not_permit_it(
    session_factory, execution_settings, now
) -> None:
    from app.authorization.models import ApprovalGrant

    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    async with session_factory() as session:
        grant = (await session.execute(select(ApprovalGrant))).scalars().one()
        # Both moved back, so the row still satisfies `expires_at > created_at`.
        grant.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        grant.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now=now)
    task, steps, events = await load(session_factory, task_id)
    assert steps[0].state is TaskStepState.PENDING
    assert task.next_run_at is None
    assert "standing_grant_used" not in events


async def test_a_revoked_grant_does_not_permit_it(
    session_factory, execution_settings, now
) -> None:
    from app.authorization.models import ApprovalGrant

    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    async with session_factory() as session:
        grant = (await session.execute(select(ApprovalGrant))).scalars().one()
        await GrantService(session, owner_id=OWNER).revoke(grant.id, reason="stop")
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now=now)
    _, steps, events = await load(session_factory, task_id)
    assert steps[0].state is TaskStepState.PENDING
    assert "standing_grant_used" not in events


async def test_a_multi_step_task_advances_one_step_per_tick(
    session_factory, execution_settings, now
) -> None:
    task_id = await scheduled_task(
        session_factory, execution_settings, step("a", 1), step("b", 2, ["a"]),
        at=now, grant=True,
    )

    first = await run_due_tasks(session_factory, execution_settings, now=now)
    assert first == (1, 1, 1)
    task, steps, _ = await load(session_factory, task_id)
    assert [s.state for s in steps] == [TaskStepState.COMPLETED, TaskStepState.PENDING]
    assert task.state is TaskState.RUNNING
    assert utc(task.next_run_at) == now, "more work was not rescheduled"

    second = await run_due_tasks(session_factory, execution_settings, now=now)
    assert second == (1, 1, 1)
    task, steps, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert task.next_run_at is None


async def test_a_completed_task_is_never_rerun(session_factory, execution_settings, now) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    await run_due_tasks(session_factory, execution_settings, now=now)

    for minutes in (1, 10, 600):
        report = await run_due_tasks(
            session_factory, execution_settings, now=now + timedelta(minutes=minutes)
        )
        assert report == (0, 0, 0)
    assert await count(session_factory, Execution) == 1


async def test_a_cancelled_task_is_never_executed(
    session_factory, execution_settings, now
) -> None:
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    async with session_factory() as session:
        await TaskService(session, settings=execution_settings).cancel(task_id)
        await session.commit()

    assert await run_due_tasks(session_factory, execution_settings, now=now) == (0, 0, 0)
    assert await count(session_factory, Execution) == 0


async def test_the_poller_does_not_modify_task_arguments(
    session_factory, execution_settings, now
) -> None:
    task_id = await scheduled_task(
        session_factory, execution_settings, step("a", 1, arguments={"path": "."}),
        at=now, grant=True,
    )
    _, before, _ = await load(session_factory, task_id)
    await run_due_tasks(session_factory, execution_settings, now=now)
    _, after, _ = await load(session_factory, task_id)
    assert after[0].arguments == before[0].arguments == {"path": "."}


# ============================================================================
# D. Failure
# ============================================================================


async def test_a_failed_step_stops_polling_and_is_not_retried(
    session_factory, execution_settings, now
) -> None:
    """Zero automatic retries of a failed step: a poisoned task cannot
    consume unlimited tool calls."""
    task_id = await scheduled_task(
        session_factory, execution_settings,
        step("a", 1, arguments={"path": "../outside"}), at=now, grant=True,
    )
    await run_due_tasks(session_factory, execution_settings, now=now)

    task, steps, events = await load(session_factory, task_id)
    assert steps[0].state is TaskStepState.FAILED
    assert task.state is not TaskState.COMPLETED
    assert task.next_run_at is None
    assert "step_failed" in events

    for minutes in (1, 60):
        await run_due_tasks(session_factory, execution_settings, now=now + timedelta(minutes=minutes))
    assert await count(session_factory, Execution) == 1


async def test_runner_errors_are_bounded_then_the_task_is_blocked(
    session_factory, execution_settings, now, monkeypatch
) -> None:
    from app.tasks.runner import TaskRunner

    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)

    async def explode(self, task_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(TaskRunner, "_advance", explode)

    moment = now
    for attempt in (1, 2):
        await run_due_tasks(session_factory, execution_settings, now=moment)
        task, _, _ = await load(session_factory, task_id)
        assert task.failure_count == attempt
        # Doubling from 60 seconds: 60, then 120. Literal.
        expected = {1: 60, 2: 120}[attempt]
        assert utc(task.next_run_at) == moment + timedelta(seconds=expected)
        moment = utc(task.next_run_at)

    await run_due_tasks(session_factory, execution_settings, now=moment)
    task, _, events = await load(session_factory, task_id)
    # The third failure is the bound. Literal, not MAX_CONSECUTIVE_FAILURES.
    assert task.failure_count == 3
    assert task.state is TaskState.BLOCKED
    assert task.next_run_at is None
    assert "task_blocked" in events

    assert await run_due_tasks(session_factory, execution_settings, now=moment + timedelta(days=1)) == (0, 0, 0)


async def test_a_successful_step_resets_the_failure_count(
    session_factory, execution_settings, now
) -> None:
    task_id = await scheduled_task(
        session_factory, execution_settings, step("a", 1), step("b", 2, ["a"]),
        at=now, grant=True,
    )
    async with session_factory() as session:
        task = (await session.execute(select(Task))).scalars().one()
        task.failure_count = 2
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now=now)
    task, _, _ = await load(session_factory, task_id)
    assert task.failure_count == 0


# ============================================================================
# E. Restart
# ============================================================================


async def test_restart_preserves_pending_work(session_factory, execution_settings, now) -> None:
    """Nothing is held in memory: a fresh runtime finds the same work."""
    task_id = await scheduled_task(
        session_factory, execution_settings, step("a", 1), step("b", 2, ["a"]),
        at=now, grant=True,
    )
    await BackgroundRuntime(session_factory, execution_settings).tick(now=now)

    fresh = BackgroundRuntime(session_factory, execution_settings)
    report = await fresh.tick(now=now)
    assert report.tasks_advanced == 1
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED


async def test_restart_does_not_replay_completed_work(
    session_factory, execution_settings, now
) -> None:
    await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    await BackgroundRuntime(session_factory, execution_settings).tick(now=now)

    for _ in range(3):
        await BackgroundRuntime(session_factory, execution_settings).tick(
            now=now + timedelta(hours=1)
        )
    assert await count(session_factory, Execution) == 1


async def test_a_crash_after_the_claim_is_recovered_after_the_lease(
    session_factory, execution_settings, now
) -> None:
    """Crash B: the claim committed, the work never ran."""
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    assert await claim_task(session_factory, task_id, OWNER, now)
    # ... and the process dies here.

    assert await run_due_tasks(session_factory, execution_settings, now=now) == (0, 0, 0)
    after = now + timedelta(seconds=300)
    assert await run_due_tasks(session_factory, execution_settings, now=after) == (1, 1, 1)
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED


async def test_a_crash_during_the_work_leaves_nothing_half_done(
    session_factory, execution_settings, now, monkeypatch
) -> None:
    """Crash C: the work transaction rolls back. No step is left `running`
    and no execution row survives, so nothing is wedged."""
    from app.background import runtime as runtime_module

    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)

    async def die_before_commit(session, task_id, result, now):
        raise SystemExit("process killed")

    monkeypatch.setattr(runtime_module, "_reschedule", die_before_commit)
    with pytest.raises(SystemExit):
        await runtime_module.advance_claimed_task(
            session_factory, execution_settings, task_id, OWNER, now
        )
    monkeypatch.undo()

    task, steps, _ = await load(session_factory, task_id)
    assert steps[0].state is TaskStepState.PENDING
    assert steps[0].execution_id is None
    assert await count(session_factory, Execution) == 0


# ============================================================================
# F. Owners, bounds and coexistence
# ============================================================================


async def test_each_task_runs_as_its_own_owner(session_factory, execution_settings, now) -> None:
    """No request context: the task's own owner is the identity used, and a
    grant belonging to one owner never lets another's task proceed."""
    mine = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    theirs = await scheduled_task(session_factory, execution_settings, owner=OTHER, at=now)

    await run_due_tasks(session_factory, execution_settings, now=now)
    my_task, _, _ = await load(session_factory, mine)
    their_task, their_steps, _ = await load(session_factory, theirs)

    assert my_task.state is TaskState.COMPLETED
    assert their_steps[0].state is TaskStepState.PENDING, "another owner's grant was used"


def test_the_per_tick_bound_is_clamped() -> None:
    from types import SimpleNamespace

    assert HARD_MAX_TASKS_PER_TICK == 20
    assert tasks_per_tick(SimpleNamespace(BACKGROUND_MAX_TASKS_PER_TICK=5)) == 5
    assert tasks_per_tick(SimpleNamespace(BACKGROUND_MAX_TASKS_PER_TICK=10_000)) == 20
    assert tasks_per_tick(SimpleNamespace(BACKGROUND_MAX_TASKS_PER_TICK=0)) == 1
    assert tasks_per_tick(SimpleNamespace(BACKGROUND_MAX_TASKS_PER_TICK="x")) == 5


def test_the_safety_constants_are_what_they_say() -> None:
    """Literal-pinned, so changing any has to be argued for."""
    assert CLAIM_LEASE_SECONDS == 300
    assert CONTENTION_BACKOFF_SECONDS == 30
    assert FAILURE_BACKOFF_SECONDS == 60
    assert MAX_CONSECUTIVE_FAILURES == 3


async def test_multiple_due_tasks_are_bounded_per_tick(
    session_factory, execution_settings, now
) -> None:
    execution_settings.BACKGROUND_MAX_TASKS_PER_TICK = 2
    for _ in range(3):
        await scheduled_task(session_factory, execution_settings, at=now)

    discovered, claimed, _ = await run_due_tasks(session_factory, execution_settings, now=now)
    assert (discovered, claimed) == (2, 2)
    discovered, claimed, _ = await run_due_tasks(session_factory, execution_settings, now=now)
    assert (discovered, claimed) == (1, 1)


async def test_reminders_and_tasks_share_one_tick(
    session_factory, execution_settings, now
) -> None:
    from app.reminders.models import Recurrence, Reminder, ReminderNotification, ReminderState

    async with session_factory() as session:
        session.add(Reminder(
            text="stand up", state=ReminderState.SCHEDULED, recurrence=Recurrence.ONCE,
            next_run_at=now, timezone_name="Asia/Kolkata",
        ))
        await session.commit()
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)

    report = await BackgroundRuntime(session_factory, execution_settings).tick(now=now)
    assert report.reminders_fired == 1
    assert report.tasks_advanced == 1
    assert await count(session_factory, ReminderNotification) == 1
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED


async def test_a_reminder_is_still_exactly_once_under_the_shared_loop(
    session_factory, execution_settings, now
) -> None:
    from app.reminders.models import Recurrence, Reminder, ReminderNotification, ReminderState

    async with session_factory() as session:
        session.add(Reminder(
            text="once", state=ReminderState.SCHEDULED, recurrence=Recurrence.ONCE,
            next_run_at=now, timezone_name="Asia/Kolkata",
        ))
        await session.commit()

    runtime = BackgroundRuntime(session_factory, execution_settings)
    for minutes in (0, 1, 60):
        await runtime.tick(now=now + timedelta(minutes=minutes))
    assert await count(session_factory, ReminderNotification) == 1


async def test_disabled_task_runtime_does_no_task_work(
    session_factory, execution_settings, now
) -> None:
    execution_settings.BACKGROUND_TASKS_ENABLED = False
    await scheduled_task(session_factory, execution_settings, at=now, grant=True)

    report = await BackgroundRuntime(session_factory, execution_settings).tick(now=now)
    assert report.tasks_discovered == 0
    assert await count(session_factory, Execution) == 0


# ============================================================================
# G. The loop itself
# ============================================================================


async def test_the_loop_starts_once_and_stops_cleanly(session_factory, execution_settings) -> None:
    runtime = BackgroundRuntime(session_factory, execution_settings)
    runtime.start()
    first = runtime._task
    runtime.start()  # idempotent
    assert runtime._task is first
    assert await runtime.wait_for_ticks(1)
    await runtime.stop()
    assert not runtime.running
    assert first.done()
    await runtime.stop()  # stopping twice is not an error


async def test_the_real_loop_advances_a_scheduled_task(
    session_factory, execution_settings
) -> None:
    """The application path: a started runtime, its own clock, real work."""
    task_id = await scheduled_task(session_factory, execution_settings, grant=True)

    runtime = BackgroundRuntime(session_factory, execution_settings)
    runtime.start()
    try:
        assert await runtime.wait_for_ticks(1)
    finally:
        await runtime.stop()

    task, _, events = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert "background_claimed" in events and "task_completed" in events


async def test_the_application_lifespan_runs_one_runtime(
    session_factory, execution_settings, monkeypatch
) -> None:
    """Through `app.main.lifespan` itself: started once, stored once, stopped."""
    import app.database.session as db_session_module
    import app.main as main_module

    execution_settings.REMINDER_POLL_SECONDS = 1
    task_id = await scheduled_task(session_factory, execution_settings, grant=True)

    class FakeProvider:
        name = "fake"

    async def noop_async(*_a, **_k):
        return True

    monkeypatch.setattr(main_module, "get_settings", lambda: execution_settings)
    monkeypatch.setattr(main_module, "configure_logging", lambda **_k: None)
    monkeypatch.setattr(main_module, "init_engine", lambda _s: None)
    monkeypatch.setattr(main_module, "check_database_connection", noop_async)
    monkeypatch.setattr(main_module, "init_provider", lambda _s: FakeProvider())
    monkeypatch.setattr(main_module, "dispose_provider", noop_async)
    monkeypatch.setattr(main_module, "dispose_engine", noop_async)
    monkeypatch.setattr(db_session_module, "get_session_factory", lambda: session_factory)

    app = main_module.create_app()
    async with main_module.lifespan(app):
        runtime = app.state.background_runtime
        assert runtime is not None and runtime.running
        assert await runtime.wait_for_ticks(1)

        # A second lifespan for the same app reuses the running runtime.
        async with main_module.lifespan(app):
            assert app.state.background_runtime is runtime

    assert not runtime.running
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED


async def test_a_task_scheduled_before_approval_cannot_be_scheduled(
    session_factory, execution_settings
) -> None:
    """Scheduling does not perform the person's plan approval."""
    async with session_factory() as session:
        service = TaskService(session, settings=execution_settings)
        created = await service.create_for_user("Needs a person")
        await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[step("a", 1)]))
        await service.authorize_plan(created.task_id)  # -> awaiting_approval

        result = await service.schedule_background(created.task_id)
        assert result.outcome is TaskOutcome.INVALID_TRANSITION
        assert result.reason == "task_not_queued"

        unauthorised = await service.create_for_user("No plan")
        refused = await service.schedule_background(unauthorised.task_id)
        assert refused.reason == "plan_not_authorized"


# ============================================================================
# H. Gaps found by mutation testing
# ============================================================================


async def test_a_task_cancelled_after_discovery_cannot_be_claimed(
    session_factory, execution_settings, now
) -> None:
    """M6: the race between discovery and claim.

    Discovery already filters by state, so a cancelled task is never
    *found* -- which hid that the claim's own state condition is the only
    thing protecting the window between the two queries. A person cancelling
    in that window must win.
    """
    task_id = await scheduled_task(session_factory, execution_settings, at=now, grant=True)
    assert await discover_due_tasks(session_factory, now, 10) == [(task_id, OWNER)]

    # Cancelled between discovery and claim. `next_run_at` is left set, as a
    # cancellation genuinely leaves it.
    async with session_factory() as session:
        await TaskService(session, settings=execution_settings).cancel(task_id)
        await session.commit()

    assert await claim_task(session_factory, task_id, OWNER, now) is False
    assert await count(session_factory, Execution) == 0


async def test_another_owners_task_runs_with_that_owners_grant(
    session_factory, execution_settings, now
) -> None:
    """M22: acting as the wrong owner fails *closed*, so asserting only that
    a task did not run could not tell the difference.

    The positive case can. A task belonging to another owner, holding that
    owner's own grant, completes -- which is only possible if the runtime
    acted as the task's owner rather than as a default identity.
    """
    theirs = await scheduled_task(
        session_factory, execution_settings, owner=OTHER, at=now, grant=True
    )
    await run_due_tasks(session_factory, execution_settings, now=now)

    task, steps, events = await load(session_factory, theirs)
    assert task.state is TaskState.COMPLETED, events
    assert steps[0].state is TaskStepState.COMPLETED
    assert "standing_grant_used" in events
