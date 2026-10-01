"""Stage 6G: monitoring.

A monitoring task is a planned, approved task whose one step is checked
repeatedly until a typed condition holds. The behavioural tests drive the
real path end to end: the runtime's discovery and claim, the real
`TaskRunner.check`, the real authorization service with a real standing
grant, the real execution service and dispatcher running workspace tools
against a temporary directory, and the application's own evaluator.
Nothing on that path is mocked except where a test injects a crash.

Time is injected for scheduling decisions. No test sleeps.

The fixture's `session_factory` is in-memory SQLite on a `StaticPool`, so
separate sessions share one connection: these tests prove the conditional
UPDATE logic, and live PostgreSQL proves isolation between connections.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.authorization.grants import GrantService
from app.background.runtime import FAILURE_BACKOFF_SECONDS, run_due_tasks
from app.execution.models import Execution
from app.execution.states import ExecutionState
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks import monitoring
from app.tasks.models import MAX_CONSECUTIVE_FAILURES, Task, TaskEvent, TaskStep
from app.tasks.monitoring import (
    CheckResult,
    SpecRefused,
    evaluate,
    interval_of,
    parse_spec,
)
from app.tasks.runner import TaskRunner
from app.tasks.schemas import RunnerOutcome, TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState
from app.tools.schemas import RiskLevel

pytestmark = pytest.mark.anyio

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")
LIST = "list_workspace_files"
READ = "read_text_file"
INTERVAL = 300

#: Synthetic text a check observes. If it ever appears in the journal or a
#: log line, observed content has leaked.
OBSERVED_SENTINEL = "observed-sentinel-7f3a"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def spec(kind="count", path="files", operator="gte", expected=2, interval=INTERVAL):
    return {
        "condition": {
            "kind": kind, "path": path, "operator": operator, "expected": expected,
        },
        "interval_seconds": interval,
    }


def goal() -> Goal:
    return Goal(summary="Watch the workspace", source_intent=IntentType.ACTION)


def one_step(capability=LIST, arguments=None) -> Plan:
    return Plan(goal=goal(), tasks=[PlanTask(
        id="watch", title="Check", dependencies=[], order=1, depth=0,
        capability=capability, arguments={} if arguments is None else arguments,
    )])


async def monitoring_task(
    session_factory, settings, monitor=None, *, capability=LIST, arguments=None,
    owner=OWNER, at=None, grant=True, schedule=True,
):
    """A monitoring task a person has planned, configured, approved and queued."""
    async with session_factory() as session:
        service = TaskService(session, settings=settings, owner_id=owner)
        created = await service.create_for_user("Watch the workspace")
        assert (await service.attach_plan(
            created.task_id, one_step(capability, arguments)
        )).ok
        configured = await service.configure_monitoring(
            created.task_id, monitor or spec()
        )
        assert configured.ok, configured
        await service.authorize_plan(created.task_id)
        await service.transition(created.task_id, TaskState.QUEUED, actor="user")
        if grant:
            await GrantService(session, owner_id=owner).create(capability, RiskLevel.LOW)
        if schedule:
            assert (await service.schedule_background(created.task_id, now=at)).ok
        await session.commit()
        return created.task_id


async def load(session_factory, task_id):
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        steps = (await session.execute(
            select(TaskStep).where(TaskStep.task_id == task_id)
        )).scalars().all()
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.sequence)
        )).scalars().all()
        return task, steps, events


def kinds(events):
    return [e.event_type.value for e in events]


async def executions(session_factory):
    async with session_factory() as session:
        return (await session.execute(
            select(Execution).order_by(Execution.created_at)
        )).scalars().all()


def utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def touch(workspace, *names):
    for name in names:
        (workspace / name).write_text("x", encoding="utf-8")


# ============================================================================
# A. The spec: typed, bounded, refused rather than repaired
# ============================================================================


def test_a_valid_spec_parses_to_exactly_what_was_given() -> None:
    parsed = parse_spec(spec())
    assert parsed.model_dump(mode="json") == {
        "condition": {"kind": "count", "path": "files", "operator": "gte", "expected": 2},
        "interval_seconds": 300,
    }


@pytest.mark.parametrize("interval", [299, 0, -1, 604_801, True, 300.0, "300", None])
def test_an_interval_out_of_bounds_is_refused_not_clamped(interval) -> None:
    with pytest.raises(SpecRefused) as refused:
        parse_spec(spec(interval=interval))
    assert refused.value.reason == "interval_out_of_range"


@pytest.mark.parametrize("interval", [300, 3600, 604_800])
def test_an_interval_within_bounds_is_kept_exactly(interval) -> None:
    assert parse_spec(spec(interval=interval)).interval_seconds == interval


@pytest.mark.parametrize("path", [
    "__class__", "files.__len__", "Files", "files[0]", "files.0", "a.b.c.d.e",
    "", "files ", "files..count", "_private", "a" * 41, "files;drop",
])
def test_a_path_that_is_not_plain_keys_is_refused(path) -> None:
    with pytest.raises(SpecRefused) as refused:
        parse_spec(spec(path=path))
    assert refused.value.reason == "invalid_condition"


@pytest.mark.parametrize("kind,operator,expected", [
    ("eval", "eq", 1),                 # unknown kind
    ("count", "contains", 1),          # operator not allowed for the kind
    ("count", "matches", 1),           # unknown operator
    ("count", "gte", -1),              # negative count
    ("count", "gte", True),            # a bool is not a count
    ("count", "gte", 1.5),             # a count is whole
    ("contains", "contains", "   "),   # empty needle
    ("contains", "contains", "x" * 201),
    ("contains", "contains", 3),       # contains needs text
    ("contains", "eq", "ready"),       # contains accepts only `contains`
    ("contains", "ne", "ready"),
    ("contains", "gt", "ready"),
    ("count", "contains", 3),
    ("value", "contains", "ready"),
    ("value", "gt", "abc"),            # ordering needs a number
    ("value", "lt", True),
    ("value", "eq", 10 ** 10),
    ("value", "eq", float("nan")),
    ("value", "eq", float("inf")),
])
def test_an_ill_typed_condition_is_refused(kind, operator, expected) -> None:
    with pytest.raises(SpecRefused) as refused:
        parse_spec(spec(kind=kind, operator=operator, expected=expected))
    assert refused.value.reason == "invalid_condition"


@pytest.mark.parametrize("raw,reason", [
    (None, "monitor_not_an_object"),
    ("count >= 2", "monitor_not_an_object"),
    ([spec()], "monitor_not_an_object"),
    ({**spec(), "code": "print(1)"}, "invalid_monitor"),
    ({"interval_seconds": 300}, "invalid_condition"),
    ({"condition": {**spec()["condition"], "script": "x"}, "interval_seconds": 300},
     "invalid_condition"),
])
def test_a_malformed_spec_is_refused(raw, reason) -> None:
    with pytest.raises(SpecRefused) as refused:
        parse_spec(raw)
    assert refused.value.reason == reason


def test_interval_of_reads_a_stored_spec_and_refuses_a_broken_one() -> None:
    assert interval_of(spec(interval=900)) == 900
    assert interval_of({"interval_seconds": 900}) is None
    assert interval_of(None) is None


# ============================================================================
# B. Evaluation: satisfied, not satisfied, unable -- never an exception
# ============================================================================


def cond(**kwargs):
    return parse_spec(spec(**kwargs)).condition


@pytest.mark.parametrize("files,result", [
    ([], CheckResult.NOT_SATISFIED),
    (["a"], CheckResult.NOT_SATISFIED),
    (["a", "b"], CheckResult.SATISFIED),
    (["a", "b", "c"], CheckResult.SATISFIED),
])
def test_a_count_condition(files, result) -> None:
    evaluation = evaluate(cond(), {"files": files})
    assert evaluation.result is result
    assert evaluation.observed_number == float(len(files))


@pytest.mark.parametrize("operator,expected,observed,met", [
    ("eq", 3, 3, True), ("eq", 3, 4, False), ("ne", 3, 4, True),
    ("gt", 3, 3, False), ("gt", 3, 4, True), ("gte", 3, 3, True),
    ("lt", 3, 3, False), ("lt", 3, 2, True), ("lte", 3, 3, True),
])
def test_every_comparison_operator(operator, expected, observed, met) -> None:
    evaluation = evaluate(
        cond(kind="value", path="count", operator=operator, expected=expected),
        {"count": observed},
    )
    assert evaluation.result is (
        CheckResult.SATISFIED if met else CheckResult.NOT_SATISFIED
    )


def test_contains_is_case_insensitive_over_text_and_lists() -> None:
    condition = cond(kind="contains", path="content", operator="contains",
                     expected="READY")
    assert evaluate(condition, {"content": "status: ready"}).result is CheckResult.SATISFIED
    assert evaluate(condition, {"content": "status: busy"}).result is CheckResult.NOT_SATISFIED
    assert evaluate(condition, {"content": ["x", "Ready!"]}).result is CheckResult.SATISFIED
    # Only the length of what was observed is reported, never the text.
    assert evaluate(condition, {"content": "abcde"}).observed_chars == 5


def test_a_nested_path_follows_dictionary_keys() -> None:
    condition = cond(kind="value", path="a.b.c", operator="eq", expected=True)
    assert evaluate(condition, {"a": {"b": {"c": True}}}).result is CheckResult.SATISFIED


@pytest.mark.parametrize("condition_kwargs,data,reason", [
    ({}, {"other": []}, "path_not_found"),
    ({}, None, "path_not_found"),
    ({}, "files", "path_not_found"),
    ({}, {"files": "abc"}, "not_a_list"),
    ({}, {"files": {"a": 1}}, "not_a_list"),
    ({"kind": "contains", "path": "files", "operator": "contains", "expected": "a"},
     {"files": [1, 2]}, "not_text"),
    ({"kind": "value", "path": "files", "operator": "eq", "expected": 1},
     {"files": "1"}, "type_mismatch"),
    ({"kind": "value", "path": "files", "operator": "eq", "expected": 1},
     {"files": True}, "type_mismatch"),
    ({"kind": "value", "path": "files", "operator": "gt", "expected": 1},
     {"files": float("nan")}, "type_mismatch"),
    ({"kind": "value", "path": "files", "operator": "gt", "expected": 1},
     {"files": float("inf")}, "type_mismatch"),
    ({"kind": "value", "path": "files", "operator": "lt", "expected": 1},
     {"files": float("-inf")}, "type_mismatch"),
    ({"kind": "value", "path": "files", "operator": "eq", "expected": True},
     {"files": 1}, "type_mismatch"),
    ({"kind": "value", "path": "files", "operator": "eq", "expected": "x"},
     {"files": 1}, "type_mismatch"),
])
def test_data_that_does_not_answer_is_unable_never_false(
    condition_kwargs, data, reason
) -> None:
    evaluation = evaluate(cond(**condition_kwargs), data)
    assert evaluation.result is CheckResult.UNABLE
    assert evaluation.reason == reason


def test_attributes_are_never_followed() -> None:
    class Shaped:
        files = ["a", "b", "c"]

    assert evaluate(cond(), Shaped()).result is CheckResult.UNABLE


def test_an_evaluation_error_is_unable_not_an_exception() -> None:
    class Hostile(dict):
        def __contains__(self, key):
            raise RuntimeError("boom")

    evaluation = evaluate(cond(), Hostile())
    assert evaluation.result is CheckResult.UNABLE
    assert evaluation.reason == "evaluation_error"


# ============================================================================
# C. Configuration: once, before approval, one read-only step
# ============================================================================


async def planned(session, settings, plan=None, owner=OWNER):
    service = TaskService(session, settings=settings, owner_id=owner)
    created = await service.create_for_user("Watch the workspace")
    assert (await service.attach_plan(created.task_id, plan or one_step())).ok
    return service, created.task_id


async def test_configuring_persists_the_validated_spec_and_journals_it(
    db_session, execution_settings
) -> None:
    service, task_id = await planned(db_session, execution_settings)
    result = await service.configure_monitoring(
        task_id, spec(kind="contains", path="content", operator="contains",
                      expected=OBSERVED_SENTINEL)
    )
    assert result.ok

    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.monitor["condition"]["expected"] == OBSERVED_SENTINEL
    assert task.check_count == 0

    event = (await db_session.execute(
        select(TaskEvent).where(TaskEvent.event_type == "monitoring_configured")
    )).scalars().one()
    assert event.actor == "user"
    # The kind, operator and interval -- never the expected value.
    assert event.event_metadata == {
        "kind": "contains", "operator": "contains", "interval_seconds": 300,
    }


async def test_a_side_effecting_capability_cannot_be_monitored(
    db_session, execution_settings
) -> None:
    service, task_id = await planned(
        db_session, execution_settings,
        one_step("create_text_file", {"path": "a.txt", "content": "x"}),
    )
    result = await service.configure_monitoring(task_id, spec())
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "capability_not_monitorable"
    assert (await db_session.execute(select(Task))).scalars().one().monitor is None


async def test_a_plan_of_two_steps_cannot_be_monitored(
    db_session, execution_settings
) -> None:
    plan = Plan(goal=goal(), tasks=[
        PlanTask(id="a", title="A", order=1, depth=0, capability=LIST, arguments={}),
        PlanTask(id="b", title="B", order=2, depth=0, capability=LIST, arguments={}),
    ])
    service, task_id = await planned(db_session, execution_settings, plan)
    result = await service.configure_monitoring(task_id, spec())
    assert result.reason == "monitoring_needs_exactly_one_step"


async def test_monitoring_is_configured_before_approval_and_only_once(
    db_session, execution_settings
) -> None:
    service, task_id = await planned(db_session, execution_settings)
    assert (await service.configure_monitoring(task_id, spec())).ok

    again = await service.configure_monitoring(task_id, spec(expected=99))
    assert again.reason == "monitoring_already_configured"

    service2, task2 = await planned(db_session, execution_settings)
    await service2.authorize_plan(task2)
    late = await service2.configure_monitoring(task2, spec())
    assert late.reason == "monitoring_must_precede_authorization"

    stored = {t.id: t.monitor for t in (await db_session.execute(select(Task))).scalars()}
    assert stored[task_id]["condition"]["expected"] == 2
    assert stored[task2] is None


async def test_an_invalid_spec_is_refused_and_nothing_is_stored(
    db_session, execution_settings
) -> None:
    service, task_id = await planned(db_session, execution_settings)
    result = await service.configure_monitoring(task_id, spec(interval=10))
    assert result.reason == "interval_out_of_range"
    assert (await db_session.execute(select(Task))).scalars().one().monitor is None
    assert (await db_session.execute(
        select(func.count()).select_from(TaskEvent)
        .where(TaskEvent.event_type == "monitoring_configured")
    )).scalar() == 0


async def test_another_owners_task_cannot_be_configured(
    db_session, execution_settings
) -> None:
    _, task_id = await planned(db_session, execution_settings)
    other = TaskService(db_session, settings=execution_settings, owner_id=OTHER)
    result = await other.configure_monitoring(task_id, spec())
    assert result.outcome is TaskOutcome.NOT_FOUND


# ============================================================================
# D. The check, through the one runtime
# ============================================================================


async def test_a_condition_not_met_schedules_the_next_check(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "one.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)

    discovered, claimed, advanced = await run_due_tasks(
        session_factory, execution_settings, now
    )
    assert (discovered, claimed, advanced) == (1, 1, 1)

    task, steps, events = await load(session_factory, task_id)
    assert task.state is TaskState.RUNNING
    assert task.check_count == 1
    assert utc(task.next_run_at) == now + timedelta(seconds=INTERVAL)
    assert task.failure_count == 0
    # The step is the template for every check. It is not consumed.
    assert [s.state for s in steps] == [TaskStepState.PENDING]
    assert steps[0].execution_id is None
    # One tool call spent; `max_steps` is not -- it is one step, repeated.
    assert task.spent["max_tool_calls"] == 1
    assert task.spent["max_steps"] == 0

    [execution] = await executions(session_factory)
    assert execution.state is ExecutionState.SUCCEEDED
    assert execution.idempotency_key == f"task:{task_id}:check:1"

    observation = [e for e in events if e.event_type.value == "observation_recorded"]
    assert len(observation) == 1
    assert observation[0].event_metadata["result"] == "not_satisfied"
    assert observation[0].event_metadata["observed"] == "1.0"
    assert "monitoring_check_started" in kinds(events)
    assert "monitoring_triggered" not in kinds(events)
    assert "task_completed" not in kinds(events)


async def test_nothing_is_checked_before_the_interval(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await run_due_tasks(session_factory, execution_settings, now)

    just_before = now + timedelta(seconds=INTERVAL - 1)
    assert await run_due_tasks(session_factory, execution_settings, just_before) == (0, 0, 0)
    task, _, _ = await load(session_factory, task_id)
    assert task.check_count == 1
    assert len(await executions(session_factory)) == 1


async def test_a_condition_met_completes_the_task_and_stops(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "one.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await run_due_tasks(session_factory, execution_settings, now)

    touch(workspace, "two.txt")
    later = now + timedelta(seconds=INTERVAL)
    assert await run_due_tasks(session_factory, execution_settings, later) == (1, 1, 1)

    task, steps, events = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert task.next_run_at is None
    assert task.check_count == 2
    assert steps[0].state is TaskStepState.COMPLETED

    runs = await executions(session_factory)
    assert [e.idempotency_key for e in runs] == [
        f"task:{task_id}:check:1", f"task:{task_id}:check:2",
    ]
    # The step records the check that proved the condition.
    assert steps[0].execution_id == runs[1].id

    sequence = kinds(events)
    assert sequence.index("monitoring_triggered") < sequence.index("task_completed")
    assert sequence[-1] == "background_unscheduled"
    assert events[-1].event_metadata["outcome"] == "condition_met"

    # Done means done: nothing is ever checked again.
    much_later = later + timedelta(days=30)
    assert await run_due_tasks(session_factory, execution_settings, much_later) == (0, 0, 0)
    assert len(await executions(session_factory)) == 2


async def test_a_condition_met_on_the_first_check(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt", "c.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await run_due_tasks(session_factory, execution_settings, now)
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert task.check_count == 1


async def test_a_contains_check_never_journals_or_logs_what_it_read(
    session_factory, execution_settings, workspace, now, caplog
) -> None:
    (workspace / "status.txt").write_text(
        f"{OBSERVED_SENTINEL} status: ready", encoding="utf-8"
    )
    task_id = await monitoring_task(
        session_factory, execution_settings,
        spec(kind="contains", path="content", operator="contains", expected="READY"),
        capability=READ, arguments={"path": "status.txt"}, at=now,
    )
    with caplog.at_level("DEBUG"):
        await run_due_tasks(session_factory, execution_settings, now)

    task, _, events = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    observation = next(e for e in events if e.event_type.value == "observation_recorded")
    assert observation.event_metadata["observed_chars"] == len(
        f"{OBSERVED_SENTINEL} status: ready"
    )
    for event in events:
        assert OBSERVED_SENTINEL not in str(event.event_metadata)
    assert OBSERVED_SENTINEL not in caplog.text


# ============================================================================
# E. Unable to evaluate, and execution failure: counted, bounded, never "false"
# ============================================================================


async def test_unable_to_evaluate_is_a_failure_not_a_false(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(
        session_factory, execution_settings, spec(path="missing"), at=now
    )
    await run_due_tasks(session_factory, execution_settings, now)

    task, _, events = await load(session_factory, task_id)
    assert task.state is TaskState.RUNNING
    assert task.failure_count == 1
    # Never retried sooner than the monitor's own interval.
    assert utc(task.next_run_at) == now + timedelta(
        seconds=max(INTERVAL, FAILURE_BACKOFF_SECONDS)
    )
    failed = [e for e in events if e.event_type.value == "monitoring_check_failed"]
    assert [e.event_metadata for e in failed] == [{"check": 1, "reason": "path_not_found"}]
    observation = next(e for e in events if e.event_type.value == "observation_recorded")
    assert observation.event_metadata["result"] == "unable"


async def test_repeated_failures_block_the_task(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(
        session_factory, execution_settings, spec(path="missing"), at=now
    )
    moment = now
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        assert (await run_due_tasks(session_factory, execution_settings, moment))[1] == 1
        moment = moment + timedelta(days=1)

    task, _, events = await load(session_factory, task_id)
    assert task.state is TaskState.BLOCKED
    assert task.next_run_at is None
    assert task.check_count == MAX_CONSECUTIVE_FAILURES
    assert kinds(events).count("monitoring_check_failed") == MAX_CONSECUTIVE_FAILURES
    assert await run_due_tasks(session_factory, execution_settings, moment) == (0, 0, 0)


async def test_a_success_resets_the_failure_count(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(
        session_factory, execution_settings,
        spec(kind="contains", path="content", operator="contains", expected="ready"),
        capability=READ, arguments={"path": "status.txt"}, at=now,
    )
    # No file yet: the read fails, which is an execution failure.
    await run_due_tasks(session_factory, execution_settings, now)
    task, _, events = await load(session_factory, task_id)
    assert task.failure_count == 1
    failed = next(e for e in events if e.event_type.value == "monitoring_check_failed")
    assert failed.event_metadata["check"] == 1
    assert "observation_recorded" not in kinds(events)

    (workspace / "status.txt").write_text("busy", encoding="utf-8")
    await run_due_tasks(session_factory, execution_settings, utc(task.next_run_at))
    task, _, _ = await load(session_factory, task_id)
    assert task.failure_count == 0
    assert task.check_count == 2
    assert task.state is TaskState.RUNNING


# ============================================================================
# F. Approval: a check needs exactly what a step needs
# ============================================================================


async def test_without_a_grant_the_check_waits_for_a_person(
    session_factory, execution_settings, workspace, now
) -> None:
    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(
        session_factory, execution_settings, at=now, grant=False
    )
    await run_due_tasks(session_factory, execution_settings, now)

    task, steps, events = await load(session_factory, task_id)
    assert task.next_run_at is None  # not hammered while it waits
    # The check number goes back, so the next attempt reuses this identity.
    assert task.check_count == 0
    assert task.spent["max_tool_calls"] == 0
    [execution] = await executions(session_factory)
    assert execution.state is ExecutionState.PROPOSED
    assert "observation_recorded" not in kinds(events)

    # A person approves that execution through the existing path, and
    # reschedules the task.
    async with session_factory() as session:
        runner = TaskRunner(TaskService(session, settings=execution_settings, owner_id=OWNER))
        await runner._executions.approve(execution.id)
        assert (await TaskService(
            session, settings=execution_settings, owner_id=OWNER
        ).schedule_background(task_id, now=now)).ok
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now)
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    runs = await executions(session_factory)
    assert [e.id for e in runs] == [execution.id]  # the same execution, reused
    assert runs[0].state is ExecutionState.SUCCEEDED


async def test_an_expired_grant_means_waiting_again(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    await run_due_tasks(session_factory, execution_settings, now)

    async with session_factory() as session:
        from app.authorization.models import ApprovalGrant

        for grant in (await session.execute(select(ApprovalGrant))).scalars():
            await GrantService(session, owner_id=OWNER).revoke(grant.id)
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now + timedelta(seconds=INTERVAL))
    task, _, events = await load(session_factory, task_id)
    assert task.next_run_at is None
    assert task.check_count == 1
    assert events[-1].event_metadata["reason"] == "awaiting_human_approval"


# ============================================================================
# G. Refusals: tampering, the wrong entry point, ownership, budget
# ============================================================================


async def test_advance_refuses_a_monitoring_task_and_check_refuses_others(
    db_session, execution_settings
) -> None:
    service, task_id = await planned(db_session, execution_settings)
    await service.configure_monitoring(task_id, spec())
    await service.authorize_plan(task_id)
    await service.transition(task_id, TaskState.QUEUED, actor="user")

    _, plain_id = await planned(db_session, execution_settings)
    await service.authorize_plan(plain_id)
    await service.transition(plain_id, TaskState.QUEUED, actor="user")

    runner = TaskRunner(service)
    advanced = await runner.advance(task_id)
    assert (advanced.outcome, advanced.reason) == (
        RunnerOutcome.REFUSED, "monitoring_task_use_check",
    )
    checked = await runner.check(plain_id)
    assert (checked.outcome, checked.reason) == (
        RunnerOutcome.REFUSED, "not_a_monitoring_task",
    )
    assert (await db_session.execute(select(func.count()).select_from(Execution))).scalar() == 0


async def test_a_tampered_spec_is_refused_at_check_time(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.monitor = {
            "condition": {"kind": "value", "path": "__class__", "operator": "eq",
                          "expected": 1},
            "interval_seconds": 300,
        }
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now)
    task, _, events = await load(session_factory, task_id)
    assert task.next_run_at is None
    assert task.check_count == 0
    assert await executions(session_factory) == []
    refused = next(e for e in events if e.event_type.value == "runner_refused")
    assert refused.event_metadata == {
        "reason": "invalid_monitoring_config", "detail": "invalid_condition",
    }


async def test_a_tampered_capability_is_refused_at_check_time(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        step = (await session.execute(
            select(TaskStep).where(TaskStep.task_id == task_id)
        )).scalars().one()
        step.capability = "create_text_file"
        step.arguments = {"path": "planted.txt", "content": "x"}
        await session.commit()

    await run_due_tasks(session_factory, execution_settings, now)
    task, _, events = await load(session_factory, task_id)
    assert task.next_run_at is None
    assert task.check_count == 0
    assert await executions(session_factory) == []
    assert not (workspace / "planted.txt").exists()
    assert events[-2].event_metadata["reason"] == "capability_not_monitorable"


async def test_another_owner_cannot_check_the_task(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        runner = TaskRunner(TaskService(session, settings=execution_settings, owner_id=OTHER))
        result = await runner.check(task_id)
        await session.commit()
    assert (result.outcome, result.reason) == (RunnerOutcome.REFUSED, "task_not_found")
    task, _, _ = await load(session_factory, task_id)
    assert task.check_count == 0
    assert await executions(session_factory) == []


async def test_the_tool_call_budget_bounds_the_number_of_checks(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.budget = {**task.budget, "max_tool_calls": 2}
        await session.commit()

    moment = now
    for _ in range(4):
        await run_due_tasks(session_factory, execution_settings, moment)
        moment = moment + timedelta(seconds=INTERVAL)

    task, _, events = await load(session_factory, task_id)
    assert task.check_count == 2
    assert task.spent["max_tool_calls"] == 2
    assert len(await executions(session_factory)) == 2
    assert task.next_run_at is None
    assert events[-1].event_type.value == "background_unscheduled"


# ============================================================================
# H. Exactly once per check, and crash recovery
# ============================================================================


async def test_a_check_number_is_claimed_exactly_once(
    session_factory, execution_settings, workspace, now
) -> None:
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        runner = TaskRunner(TaskService(session, settings=execution_settings, owner_id=OWNER))
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        assert await runner._claim_check(task, 1) is True
        assert await runner._claim_check(task, 1) is False  # a stale reader loses
        assert await runner._claim_check(task, 3) is False  # no skipping ahead
        await session.commit()


async def test_a_release_only_hands_back_the_number_it_holds(
    session_factory, execution_settings, workspace, now
) -> None:
    """A stale release must not rewind a number someone else has moved on."""
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.check_count = 3
        await session.commit()

        runner = TaskRunner(TaskService(session, settings=execution_settings, owner_id=OWNER))
        await runner._release_check(task, 2)   # holds 2, but the count is 3
        await session.commit()
        assert (await session.execute(
            select(Task.check_count).where(Task.id == task_id)
        )).scalar() == 3

        await runner._release_check(task, 3)   # holds 3: this one is released
        await session.commit()
        assert (await session.execute(
            select(Task.check_count).where(Task.id == task_id)
        )).scalar() == 2


async def test_a_contended_check_backs_off_and_is_not_a_failure(
    session_factory, execution_settings, workspace, now, monkeypatch
) -> None:
    """Another worker holding the check is contention, like a held step."""
    from app.background.runtime import CONTENTION_BACKOFF_SECONDS

    task_id = await monitoring_task(session_factory, execution_settings, at=now)

    async def lost(self, task, sequence):
        return False

    monkeypatch.setattr(TaskRunner, "_claim_check", lost)
    assert await run_due_tasks(session_factory, execution_settings, now) == (1, 1, 0)

    task, _, events = await load(session_factory, task_id)
    assert utc(task.next_run_at) == now + timedelta(seconds=CONTENTION_BACKOFF_SECONDS)
    assert task.failure_count == 0
    assert task.state is TaskState.RUNNING
    assert await executions(session_factory) == []
    assert "background_unscheduled" not in kinds(events)


async def test_a_second_runner_with_a_stale_count_does_no_work(
    session_factory, execution_settings, workspace, now
) -> None:
    """Two workers read `check_count == 0`; only one may perform check 1."""
    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalars().one()
        task.check_count = 1  # another worker claimed check 1 meanwhile
        await session.commit()

    async with session_factory() as session:
        service = TaskService(session, settings=execution_settings, owner_id=OWNER)
        await TaskRunner(service).mark_task_running(task_id)
        runner = TaskRunner(service)
        original = runner._claim_check

        async def stale(task, sequence):
            return await original(task, 1)  # as if it had read 0

        runner._claim_check = stale
        result = await runner.check(task_id)
        await session.commit()

    assert (result.outcome, result.reason) == (
        RunnerOutcome.BLOCKED, "check_already_claimed",
    )
    assert await executions(session_factory) == []


async def test_a_crash_mid_check_rolls_back_and_the_retry_reuses_the_identity(
    session_factory, execution_settings, workspace, now, monkeypatch
) -> None:
    from app.execution.service import ExecutionService

    touch(workspace, "a.txt", "b.txt")
    task_id = await monitoring_task(session_factory, execution_settings, at=now)

    async def crash(self, execution_id):
        raise RuntimeError("process died")

    with monkeypatch.context() as patched:
        patched.setattr(ExecutionService, "run_returning_outcome", crash)
        await run_due_tasks(session_factory, execution_settings, now)

    task, _, events = await load(session_factory, task_id)
    assert task.check_count == 0          # the claim rolled back with the work
    assert await executions(session_factory) == []
    assert task.failure_count == 1
    assert "monitoring_check_started" not in kinds(events)

    await run_due_tasks(session_factory, execution_settings, utc(task.next_run_at))
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    [execution] = await executions(session_factory)
    assert execution.idempotency_key == f"task:{task_id}:check:1"


async def test_a_restart_resumes_from_the_database(
    session_factory, execution_settings, workspace, now
) -> None:
    """Nothing about the schedule lives in memory: a fresh runtime picks up."""
    from app.background.runtime import BackgroundRuntime

    task_id = await monitoring_task(session_factory, execution_settings, at=now)
    first = BackgroundRuntime(session_factory, execution_settings)
    await first.tick(now)

    touch(workspace, "a.txt", "b.txt")
    second = BackgroundRuntime(session_factory, execution_settings)
    report = await second.tick(now + timedelta(seconds=INTERVAL))
    assert report.tasks_claimed == 1
    task, _, _ = await load(session_factory, task_id)
    assert task.state is TaskState.COMPLETED
    assert task.check_count == 2


def _sqlite_migrated(tmp_path, target="head"):
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


def test_the_migration_gives_check_count_its_default_and_constraint(tmp_path) -> None:
    import sqlite3

    database, run = _sqlite_migrated(tmp_path)
    run("upgrade", "head")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row for row in connection.execute("pragma table_info(tasks)")
        }
        ddl = connection.execute(
            "select sql from sqlite_master where name = 'tasks'"
        ).fetchone()[0]
    # (cid, name, type, notnull, default, pk)
    assert columns["check_count"][2:5] == ("INTEGER", 1, "0")
    assert columns["monitor"][3] == 0           # nullable
    assert "ck_tasks_check_count_non_negative CHECK (check_count >= 0)" in ddl


def test_the_migration_round_trips_and_removes_only_what_it_added(tmp_path) -> None:
    import sqlite3

    database, run = _sqlite_migrated(tmp_path)
    run("upgrade", "head")
    with sqlite3.connect(database) as connection:
        before = {r[1] for r in connection.execute("pragma table_info(tasks)")}
    run("downgrade", "0016")
    with sqlite3.connect(database) as connection:
        after = {r[1] for r in connection.execute("pragma table_info(tasks)")}
    assert before - after == {"monitor", "check_count"}
    assert after - before == set()
    run("upgrade", "head")


def test_the_monitorable_set_is_literal() -> None:
    assert monitoring.MONITORABLE_CAPABILITIES == frozenset({
        "web_search", "calendar_list_events", "gmail_list_messages",
        "gmail_get_message", "list_workspace_files", "read_text_file",
    })
