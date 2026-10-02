"""Stage 6H: notifications for monitoring outcomes.

Behavioural tests drive the real 6G path -- the runtime's discovery and claim,
`TaskRunner.check`, the real authorization service with a real standing
grant, the real execution service running `list_workspace_files` against a
temporary workspace, the evaluator, the task transition -- and assert what
`task_notifications` and the journal hold afterwards. Nothing on that path is
mocked except where a test injects a crash.

The fixture's `session_factory` is in-memory SQLite on a `StaticPool`, so
these tests prove the logic and the constraint; isolation between concurrent
connections is proven against live PostgreSQL.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.authorization.grants import GrantService
from app.background.runtime import BackgroundRuntime, run_due_tasks
from app.execution.models import Execution
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks import notifications as notifications_module
from app.tasks.models import (
    MAX_CONSECUTIVE_FAILURES,
    Task,
    TaskEvent,
    TaskNotification,
    TaskNotificationKind,
)
from app.tasks.notifications import (
    MAX_LISTED,
    NOTIFIABLE_OUTCOMES,
    NotificationService,
    record_outcome,
)
from app.tasks.runner import TaskRunner
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from app.tools.schemas import RiskLevel

pytestmark = pytest.mark.anyio

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")
LIST = "list_workspace_files"
INTERVAL = 300
MET, FAILED = TaskNotificationKind.CONDITION_MET, TaskNotificationKind.MONITORING_FAILED

#: Synthetic text placed in the objective. If it ever appears in a
#: notification, its journal entry or a log line, content has leaked.
OBJECTIVE_SENTINEL = "objective-sentinel-6h-3c1e"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def spec(path="files", expected=2):
    return {
        "condition": {"kind": "count", "path": path, "operator": "gte",
                      "expected": expected},
        "interval_seconds": INTERVAL,
    }


def plan() -> Plan:
    return Plan(goal=Goal(summary="Watch", source_intent=IntentType.ACTION), tasks=[
        PlanTask(id="watch", title="Check", dependencies=[], order=1, depth=0,
                 capability=LIST, arguments={})
    ])


async def monitoring_task(session_factory, settings, monitor=None, *, owner=OWNER,
                          at=None, grant=True):
    async with session_factory() as session:
        service = TaskService(session, settings=settings, owner_id=owner)
        created = await service.create_for_user(f"Watch {OBJECTIVE_SENTINEL}")
        assert (await service.attach_plan(created.task_id, plan())).ok
        assert (await service.configure_monitoring(created.task_id, monitor or spec())).ok
        await service.authorize_plan(created.task_id)
        await service.transition(created.task_id, TaskState.QUEUED, actor="user")
        if grant:
            await GrantService(session, owner_id=owner).create(LIST, RiskLevel.LOW)
        assert (await service.schedule_background(created.task_id, now=at)).ok
        await session.commit()
        return created.task_id


async def ordinary_task(session_factory, settings, *, at=None):
    async with session_factory() as session:
        service = TaskService(session, settings=settings, owner_id=OWNER)
        created = await service.create_for_user("An ordinary task")
        assert (await service.attach_plan(created.task_id, plan())).ok
        await service.authorize_plan(created.task_id)
        await service.transition(created.task_id, TaskState.QUEUED, actor="user")
        await GrantService(session, owner_id=OWNER).create(LIST, RiskLevel.LOW)
        assert (await service.schedule_background(created.task_id, now=at)).ok
        await session.commit()
        return created.task_id


async def notes(session_factory, task_id=None):
    async with session_factory() as session:
        statement = select(TaskNotification).order_by(TaskNotification.check_number)
        if task_id is not None:
            statement = statement.where(TaskNotification.task_id == task_id)
        return (await session.execute(statement)).scalars().all()


async def task_and_events(session_factory, task_id):
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.sequence)
        )).scalars().all()
        return task, events


def kinds(events):
    return [e.event_type.value for e in events]


def touch(workspace, *names):
    for name in names:
        (workspace / name).write_text("x", encoding="utf-8")


def utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


async def fail_until_blocked(session_factory, settings, start, failures=MAX_CONSECUTIVE_FAILURES):
    moment = start
    for _ in range(failures):
        await run_due_tasks(session_factory, settings, moment)
        moment = moment + timedelta(days=1)
    return moment


# ============================================================================
# A. Condition met: exactly one notification, in the outcome's transaction
# ============================================================================


async def test_a_met_condition_produces_one_notification(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await run_due_tasks(session_factory, execution_settings, now)

    task, events = await task_and_events(session_factory, task_id)
    [note] = await notes(session_factory)
    assert task.state is TaskState.COMPLETED
    assert (note.owner_id, note.task_id, note.kind, note.check_number) == (
        OWNER, task_id, MET, 1,
    )
    assert note.read_at is None
    async with session_factory() as session:
        [execution] = (await session.execute(select(Execution))).scalars().all()
    assert note.execution_id == execution.id   # the check that proved it

    sequence = kinds(events)
    assert sequence.count("notification_created") == 1
    assert sequence.index("task_completed") < sequence.index("notification_created")
    created = next(e for e in events if e.event_type.value == "notification_created")
    assert created.event_metadata == {
        "kind": "condition_met", "notification_id": str(note.id), "check": 1,
    }


async def test_a_condition_that_stays_false_notifies_nothing(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    moment = now
    for _ in range(4):
        await run_due_tasks(session_factory, execution_settings, moment)
        moment = moment + timedelta(seconds=INTERVAL)

    task, events = await task_and_events(session_factory, task_id)
    assert task.check_count == 4
    assert task.state is TaskState.RUNNING
    assert await notes(session_factory) == []
    assert "notification_created" not in kinds(events)


async def test_a_met_condition_after_unmet_checks_carries_the_proving_check(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await run_due_tasks(session_factory, execution_settings, now)
    touch(workspace, "b.txt")
    await run_due_tasks(session_factory, execution_settings, now + timedelta(seconds=INTERVAL))

    [note] = await notes(session_factory, task_id)
    assert (note.kind, note.check_number) == (MET, 2)


async def test_repeated_ticks_and_a_restart_never_duplicate_it(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await BackgroundRuntime(session_factory, execution_settings).tick(now)

    # More passes of the same runtime, then a fresh one as after a restart.
    for offset in (1, INTERVAL, 86_400):
        await run_due_tasks(session_factory, execution_settings, now + timedelta(seconds=offset))
    await BackgroundRuntime(session_factory, execution_settings).tick(now + timedelta(days=30))

    # And direct, repeated invocations of the runner on the finished task.
    async with session_factory() as session:
        runner = TaskRunner(TaskService(session, settings=execution_settings, owner_id=OWNER))
        for _ in range(3):
            assert (await runner.check(task_id)).reason == "task_is_terminal"
        await session.commit()

    assert len(await notes(session_factory)) == 1
    _, events = await task_and_events(session_factory, task_id)
    assert kinds(events).count("notification_created") == 1


async def test_a_crash_while_notifying_rolls_back_the_outcome_too(
    session_factory, execution_settings, workspace, now, monkeypatch
) -> None:
    """The task cannot be completed without its notification, nor notified
    without being completed: one transaction, and the retry produces both."""
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    original = notifications_module.journal.record

    async def crash_on_notification(session, task_id, event_type, **kwargs):
        if event_type.value == "notification_created":
            raise RuntimeError("process died")
        return await original(session, task_id, event_type, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(notifications_module.journal, "record", crash_on_notification)
        await run_due_tasks(session_factory, execution_settings, now)

    task, events = await task_and_events(session_factory, task_id)
    # The whole work transaction rolled back -- even its queued -> running
    # transition -- so nothing about this pass survived, including completion.
    assert task.state is TaskState.QUEUED
    assert task.check_count == 0                    # the check rolled back too
    assert await notes(session_factory) == []
    assert "task_completed" not in kinds(events)

    await run_due_tasks(session_factory, execution_settings, utc(task.next_run_at))
    task, events = await task_and_events(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    [note] = await notes(session_factory)
    assert note.check_number == 1
    assert kinds(events).count("notification_created") == 1


# ============================================================================
# B. Failure: one notification when monitoring gives up, none before
# ============================================================================


async def test_failures_notify_only_when_the_task_is_blocked(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(
        session_factory, execution_settings, spec(path="missing"), at=now
    )
    moment = now
    for attempt in range(1, MAX_CONSECUTIVE_FAILURES + 1):
        await run_due_tasks(session_factory, execution_settings, moment)
        task, _ = await task_and_events(session_factory, task_id)
        expected = 1 if attempt == MAX_CONSECUTIVE_FAILURES else 0
        assert len(await notes(session_factory)) == expected, attempt
        moment = moment + timedelta(days=1)

    task, events = await task_and_events(session_factory, task_id)
    [note] = await notes(session_factory)
    assert task.state is TaskState.BLOCKED
    assert (note.kind, note.check_number, note.execution_id) == (
        FAILED, MAX_CONSECUTIVE_FAILURES, None,
    )
    sequence = kinds(events)
    assert sequence.index("background_unscheduled") < sequence.index("notification_created")
    # Blocked stays blocked: no further pass adds anything.
    await run_due_tasks(session_factory, execution_settings, moment + timedelta(days=9))
    assert len(await notes(session_factory)) == 1


async def test_runner_errors_that_block_a_monitor_notify_once(
    session_factory, execution_settings, workspace, now, monkeypatch
) -> None:
    from app.execution.service import ExecutionService

    task_id = await monitoring_task(session_factory, execution_settings, at=now)

    async def crash(self, execution_id):
        raise RuntimeError("process died")

    monkeypatch.setattr(ExecutionService, "run_returning_outcome", crash)
    await fail_until_blocked(session_factory, execution_settings, now)

    task, _ = await task_and_events(session_factory, task_id)
    [note] = await notes(session_factory)
    assert task.state is TaskState.BLOCKED
    # Every crashed check rolled back, so no check number was ever kept.
    assert (note.kind, note.check_number) == (FAILED, 0)


async def test_a_resumed_monitor_that_fails_again_is_a_new_outcome(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(
        session_factory, execution_settings, spec(path="missing"), at=now
    )
    moment = await fail_until_blocked(session_factory, execution_settings, now)

    # A person resumes the blocked task, and it fails again after new checks.
    async with session_factory() as session:
        service = TaskService(session, settings=execution_settings, owner_id=OWNER)
        assert (await service.transition(task_id, TaskState.QUEUED, actor="user")).ok
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.failure_count = 0
        assert (await service.schedule_background(task_id, now=moment)).ok
        await session.commit()
    await fail_until_blocked(session_factory, execution_settings, moment)

    assert [(n.kind, n.check_number) for n in await notes(session_factory)] == [
        (FAILED, 3), (FAILED, 6),
    ]


async def test_an_ordinary_task_that_is_blocked_notifies_nothing(
    session_factory, execution_settings, workspace, now, monkeypatch
) -> None:
    from app.execution.service import ExecutionService

    task_id = await ordinary_task(session_factory, execution_settings, at=now)

    async def crash(self, execution_id):
        raise RuntimeError("process died")

    monkeypatch.setattr(ExecutionService, "run", crash)
    await fail_until_blocked(session_factory, execution_settings, now)

    task, events = await task_and_events(session_factory, task_id)
    assert task.state is TaskState.BLOCKED
    assert await notes(session_factory) == []
    assert "notification_created" not in kinds(events)


async def test_a_crash_while_notifying_a_block_rolls_back_the_block(
    session_factory, execution_settings, workspace, now, monkeypatch
) -> None:
    task_id = await monitoring_task(
        session_factory, execution_settings, spec(path="missing"), at=now
    )
    moment = await fail_until_blocked(
        session_factory, execution_settings, now, MAX_CONSECUTIVE_FAILURES - 1
    )
    original = notifications_module.journal.record

    async def crash_on_notification(session, task_id, event_type, **kwargs):
        if event_type.value == "notification_created":
            raise RuntimeError("process died")
        return await original(session, task_id, event_type, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(notifications_module.journal, "record", crash_on_notification)
        await run_due_tasks(session_factory, execution_settings, moment)

    task, _ = await task_and_events(session_factory, task_id)
    assert task.state is TaskState.RUNNING     # the block did not survive alone
    assert await notes(session_factory) == []

    await run_due_tasks(session_factory, execution_settings, utc(task.next_run_at))
    task, _ = await task_and_events(session_factory, task_id)
    assert task.state is TaskState.BLOCKED
    assert len(await notes(session_factory)) == 1


async def test_waiting_for_approval_is_not_a_notified_outcome(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now, grant=False)
    await run_due_tasks(session_factory, execution_settings, now)
    task, _ = await task_and_events(session_factory, task_id)
    assert task.next_run_at is None
    assert await notes(session_factory) == []


# ============================================================================
# C. The writer's guards
# ============================================================================


async def _completed_monitor(session_factory, settings, workspace, now):
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, settings, at=now)
    await run_due_tasks(session_factory, settings, now)
    return task_id


async def test_the_unique_index_refuses_a_second_row_for_one_outcome(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await _completed_monitor(session_factory, execution_settings, workspace, now)
    async with session_factory() as session:
        session.add(TaskNotification(
            owner_id=OWNER, task_id=task_id, kind=MET, check_number=1,
        ))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
    assert len(await notes(session_factory)) == 1


async def test_recording_the_same_outcome_again_is_a_no_op(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await _completed_monitor(session_factory, execution_settings, workspace, now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        for _ in range(3):
            assert await record_outcome(session, task, MET) is None
        await session.commit()

    assert len(await notes(session_factory)) == 1
    _, events = await task_and_events(session_factory, task_id)
    assert kinds(events).count("notification_created") == 1


@pytest.mark.parametrize("state,kind", [
    (TaskState.RUNNING, TaskNotificationKind.CONDITION_MET),
    (TaskState.BLOCKED, TaskNotificationKind.CONDITION_MET),
    (TaskState.COMPLETED, TaskNotificationKind.MONITORING_FAILED),
    (TaskState.RUNNING, TaskNotificationKind.MONITORING_FAILED),
    (TaskState.CANCELLED, TaskNotificationKind.MONITORING_FAILED),
])
async def test_an_outcome_is_notified_only_in_the_state_it_produced(
    session_factory, execution_settings, workspace, now, state, kind
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.state = state
        assert await record_outcome(session, task, kind) is None
        await session.rollback()
    assert await notes(session_factory) == []


@pytest.mark.parametrize("kind", [
    "condition_met", "monitoring_failed", "CONDITION_MET", None, 1,
    "condition_met; DROP TABLE tasks",
])
async def test_a_kind_that_is_not_the_enum_is_refused(
    session_factory, execution_settings, workspace, now, kind
) -> None:
    task_id = await _completed_monitor(session_factory, execution_settings, workspace, now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.check_count = 99   # a fresh outcome identity, so only the type guard refuses
        assert await record_outcome(session, task, kind) is None
        await session.rollback()
    assert len(await notes(session_factory)) == 1


async def test_a_task_that_is_not_monitoring_is_refused(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await ordinary_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.state = TaskState.COMPLETED
        assert await record_outcome(session, task, MET) is None
        assert await record_outcome(session, None, MET) is None
        await session.rollback()
    assert await notes(session_factory) == []


async def test_the_owner_is_always_the_tasks_own(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, execution_settings, owner=OTHER, at=now)
    await run_due_tasks(session_factory, execution_settings, now)
    [note] = await notes(session_factory, task_id)
    assert note.owner_id == OTHER


# ============================================================================
# D. The read contract: owner-scoped, read once, nothing else
# ============================================================================


async def _two_owners(session_factory, settings, workspace, now):
    touch(workspace, "a.txt", "b.txt")
    mine = await monitoring_task(session_factory, settings, at=now)
    theirs = await monitoring_task(session_factory, settings, owner=OTHER, at=now)
    await run_due_tasks(session_factory, settings, now)
    return mine, theirs


async def test_each_owner_sees_only_their_own(
    session_factory, execution_settings, workspace, now
) -> None:
    mine, theirs = await _two_owners(session_factory, execution_settings, workspace, now)
    async with session_factory() as session:
        me = NotificationService(session, OWNER)
        them = NotificationService(session, OTHER)
        [my_note] = await me.unread()
        [their_note] = await them.unread()
        assert (my_note.task_id, their_note.task_id) == (mine, theirs)

        assert await me.for_task(theirs) == []
        assert await them.for_task(mine) == []
        assert await me.get(their_note.id) is None
        assert (await me.get(my_note.id)).id == my_note.id

        assert await me.mark_read(their_note.id) is False
        await session.commit()
    async with session_factory() as session:
        assert len(await NotificationService(session, OTHER).unread()) == 1


async def test_marking_read_happens_once(
    session_factory, execution_settings, workspace, now
) -> None:
    await _completed_monitor(session_factory, execution_settings, workspace, now)
    async with session_factory() as session:
        service = NotificationService(session, OWNER)
        [note] = await service.unread()
        assert await service.mark_read(note.id) is True
        assert await service.mark_read(note.id) is False
        assert await service.mark_read(uuid.uuid4()) is False
        await session.commit()
    async with session_factory() as session:
        service = NotificationService(session, OWNER)
        assert await service.unread() == []
        [stored] = await service.for_task(note.task_id)
        assert stored.read_at is not None
        assert utc(stored.read_at) >= utc(stored.created_at)


async def test_unread_is_oldest_first_and_bounded(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt")
    ids = []
    for _ in range(3):
        ids.append(await monitoring_task(session_factory, execution_settings, at=now))
    await run_due_tasks(session_factory, execution_settings, now)
    async with session_factory() as session:
        service = NotificationService(session, OWNER)
        listed = await service.unread()
        assert len(listed) == 3
        assert [n.created_at for n in listed] == sorted(n.created_at for n in listed)
        assert len(await service.unread(limit=2)) == 2
        assert len(await service.unread(limit=0)) == 1          # floor of one
        assert len(await service.unread(limit=10_000)) == 3     # ceiling of MAX_LISTED
    assert MAX_LISTED == 50


async def test_reading_creates_and_executes_nothing(
    session_factory, execution_settings, workspace, now
) -> None:
    await _completed_monitor(session_factory, execution_settings, workspace, now)

    async def counts():
        async with session_factory() as session:
            return tuple([
                (await session.execute(select(func.count()).select_from(m))).scalar()
                for m in (TaskNotification, Execution, TaskEvent, Task)
            ])

    before = await counts()
    async with session_factory() as session:
        service = NotificationService(session, OWNER)
        for _ in range(5):
            [note] = await service.unread()
            await service.for_task(note.task_id)
            await service.get(note.id)
        await session.commit()
    assert await counts() == before


# ============================================================================
# E. Payload: references and timestamps, never content
# ============================================================================


def test_the_notification_row_has_exactly_these_columns() -> None:
    assert sorted(c.name for c in TaskNotification.__table__.columns) == [
        "check_number", "created_at", "execution_id", "id", "kind", "owner_id",
        "read_at", "task_id",
    ]


def test_the_outcome_vocabulary_is_closed() -> None:
    assert sorted(k.value for k in TaskNotificationKind) == [
        "condition_met", "monitoring_failed",
    ]
    assert NOTIFIABLE_OUTCOMES == {
        TaskNotificationKind.CONDITION_MET: TaskState.COMPLETED,
        TaskNotificationKind.MONITORING_FAILED: TaskState.BLOCKED,
    }


async def test_no_content_reaches_the_notification_the_journal_or_the_logs(
    session_factory, execution_settings, workspace, now, caplog
) -> None:
    (workspace / f"{OBJECTIVE_SENTINEL}.txt").write_text(OBJECTIVE_SENTINEL)
    (workspace / "b.txt").write_text("x")
    with caplog.at_level("DEBUG"):
        task_id = await monitoring_task(session_factory, execution_settings, at=now)
        await run_due_tasks(session_factory, execution_settings, now)

    [note] = await notes(session_factory)
    row = {c.name: getattr(note, c.name) for c in TaskNotification.__table__.columns}
    assert OBJECTIVE_SENTINEL not in repr(row)
    _, events = await task_and_events(session_factory, task_id)
    created = next(e for e in events if e.event_type.value == "notification_created")
    assert OBJECTIVE_SENTINEL not in repr(created.event_metadata)
    notification_logs = [
        r for r in caplog.records if r.name == "app.tasks.notifications"
    ]
    assert notification_logs, "the writer logs its outcome"
    for record in notification_logs:
        assert OBJECTIVE_SENTINEL not in record.getMessage()
        assert OBJECTIVE_SENTINEL not in repr(record.__dict__)


# ============================================================================
# F. The migration, on SQLite (live PostgreSQL is verified separately)
# ============================================================================


def _alembic(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"

    def run(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=backend,
            env={"PATH": "/usr/bin:/bin", "DATABASE_URL": url,
                 "GROQ_API_KEY": "test-key", "LOG_LEVEL": "WARNING"},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    return tmp_path / "migrated.db", run


def test_the_migration_round_trips_and_touches_only_its_own_table(tmp_path) -> None:
    import sqlite3

    database, run = _alembic(tmp_path)

    def snapshot():
        with sqlite3.connect(database) as connection:
            return {
                row[0]: row[1] for row in connection.execute(
                    "select name, sql from sqlite_master where name not like 'sqlite_%'"
                )
            }

    run("upgrade", "0017")
    before = snapshot()
    # 6H's own revision, not head: later stages add their own tables (6M.1).
    run("upgrade", "0018")
    after = snapshot()
    added = set(after) - set(before)
    assert added == {
        "task_notifications",
        "uq_task_notifications_outcome",
        "ix_task_notifications_owner_id_read_at_created_at",
    }
    # Nothing that existed before changed.
    assert {k: after[k] for k in before} == before
    ddl = after["task_notifications"]
    for fragment in (
        "CONSTRAINT ck_task_notifications_check_number_non_negative",
        "CONSTRAINT ck_task_notifications_read_after_created",
        "ON DELETE CASCADE", "ON DELETE SET NULL",
    ):
        assert fragment in ddl, fragment
    assert "UNIQUE INDEX uq_task_notifications_outcome ON task_notifications (task_id, kind, check_number)" in after["uq_task_notifications_outcome"]

    run("downgrade", "0017")
    assert snapshot() == before
    run("upgrade", "0018")
    assert snapshot() == after
