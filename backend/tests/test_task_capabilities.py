"""Stage 6C: capability binding, authorization, and the execution boundary.

A persisted plan is not permission to execute. These tests drive the real
service against the real registry and the real authorization service; nothing
is mocked except the clock-free parts that have no clock.

Nothing here runs a tool. Several tests exist to prove it.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.execution.models import Execution, ExecutionEvent
from app.execution.states import ExecutionState
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks.capabilities import BindingStatus, bind_plan, bind_step
from app.tasks.models import Task, TaskEvent, TaskStep
from app.tasks.schemas import TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState

pytestmark = pytest.mark.anyio

#: A capability that is declared, executable, and needs approval.
REAL = "web_search"
REAL_ARGS = {"query": "kerala in october"}
#: Declared, permitted, and with no implementation in this deployment.
UNAVAILABLE = "future_send_email"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401  (import registers)


@pytest.fixture
def service(db_session, execution_settings) -> TaskService:
    """A service on a deployment that *can* execute.

    `execution_settings` rather than `settings`: creating an execution record
    goes through `ExecutionService`, which refuses outright when execution is
    switched off -- the configuration Mai ships in, and the one the rest of
    the suite exercises. A separate test below pins that refusal.
    """
    return TaskService(db_session, settings=execution_settings)


def goal(summary: str = "Do the thing") -> Goal:
    return Goal(summary=summary, source_intent=IntentType.ACTION)


def step(key, order, deps=(), capability=REAL, arguments=None, **kw) -> PlanTask:
    return PlanTask(
        id=key, title=f"Step {key}", dependencies=list(deps), order=order,
        depth=len(deps), capability=capability,
        arguments=REAL_ARGS if arguments is None else arguments, **kw,
    )


def plan_of(*steps, **kw) -> Plan:
    return Plan(goal=goal(), tasks=list(steps), **kw)


async def planned(service, *steps, objective="Do the thing"):
    created = await service.create_for_user(objective)
    result = await service.attach_plan(created.task_id, plan_of(*steps))
    assert result.ok, result
    return created.task_id


# ============================================================================
# A. Binding a capability name
# ============================================================================


def test_a_known_executable_capability_binds() -> None:
    binding = bind_step("a", REAL, REAL_ARGS)
    assert binding.status is BindingStatus.NEEDS_APPROVAL
    assert binding.capability == REAL
    assert binding.bindable


def test_an_unknown_capability_does_not_bind() -> None:
    binding = bind_step("a", "definitely_not_a_tool", {})
    assert binding.status is BindingStatus.UNKNOWN
    assert binding.reason == "unknown_capability"
    assert binding.capability is None
    assert not binding.bindable


def test_a_declared_but_unavailable_capability_does_not_bind() -> None:
    """Permitted is not the same as possible."""
    binding = bind_step("a", UNAVAILABLE, {})
    assert binding.status is BindingStatus.UNAVAILABLE
    assert binding.reason == "capability_unavailable"
    assert not binding.bindable


def test_invalid_arguments_do_not_bind() -> None:
    binding = bind_step("a", REAL, {"not_a_field": 1})
    assert binding.status is BindingStatus.INVALID_ARGUMENTS
    assert not binding.bindable


def test_a_step_with_no_capability_does_not_bind() -> None:
    for empty in (None, "", "   "):
        binding = bind_step("a", empty, {})
        assert binding.status is BindingStatus.NO_CAPABILITY
        assert binding.reason == "step_declares_no_capability"


def test_binding_a_plan_is_all_or_nothing() -> None:
    class S:
        def __init__(self, key, cap):
            self.step_key, self.capability, self.arguments = key, cap, REAL_ARGS

    ok = bind_plan([S("a", REAL), S("b", REAL)])
    assert ok.ok and len(ok.bindings) == 2

    mixed = bind_plan([S("a", REAL), S("b", "nope")])
    assert not mixed.ok
    assert mixed.reason == "unknown_capability"


def test_a_refusal_names_only_the_first_fault() -> None:
    """Reporting every one would let a caller enumerate the registry."""
    class S:
        def __init__(self, key, cap):
            self.step_key, self.capability, self.arguments = key, cap, {}

    result = bind_plan([S("a", "nope_one"), S("b", "nope_two")])
    assert result.reason == "unknown_capability"


# ============================================================================
# B. Authorization -- a plan is not permission
# ============================================================================


async def test_a_plan_is_not_permission_until_authorized(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1))

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.PLANNED
    assert row.authorized_at is None
    # Binding has not happened, so no step carries a capability yet.
    steps = (await db_session.execute(select(TaskStep))).scalars().all()
    assert all(s.capability is None for s in steps)


async def test_authorizing_binds_every_step_and_records_the_decision(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1), step("b", 2, ["a"]))
    result = await service.authorize_plan(task_id)

    assert result.outcome is TaskOutcome.UPDATED
    assert result.state is TaskState.AWAITING_APPROVAL

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.authorized_at is not None

    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    assert [s.capability for s in steps] == [REAL, REAL]
    assert all(s.arguments == REAL_ARGS for s in steps)

    events = (await db_session.execute(select(TaskEvent))).scalars().all()
    assert "approval_requested" in {e.event_type.value for e in events}


async def test_the_bound_name_is_the_registrys_not_the_models(
    service, db_session
) -> None:
    """The model's spelling never reaches the column."""
    task_id = await planned(service, step("a", 1, capability="  WEB_SEARCH  "))
    assert (await service.authorize_plan(task_id)).ok

    stored = (await db_session.execute(select(TaskStep))).scalars().one()
    assert stored.capability == "web_search"


@pytest.mark.parametrize("capability, reason", [
    ("definitely_not_a_tool", "unknown_capability"),
    (UNAVAILABLE, "capability_unavailable"),
    (None, "step_declares_no_capability"),
])
async def test_an_unbindable_plan_is_not_authorized(
    capability, reason, service, db_session
) -> None:
    task_id = await planned(
        service, step("a", 1), step("b", 2, ["a"], capability=capability)
    )
    result = await service.authorize_plan(task_id)

    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == reason

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.authorized_at is None
    assert row.state is TaskState.PLANNED
    # All or nothing: the step that *would* have bound did not.
    steps = (await db_session.execute(select(TaskStep))).scalars().all()
    assert all(s.capability is None for s in steps)


async def test_invalid_arguments_prevent_authorization(service, db_session) -> None:
    task_id = await planned(service, step("a", 1, arguments={"nope": 1}))
    result = await service.authorize_plan(task_id)

    assert result.outcome is TaskOutcome.REFUSED
    assert (await db_session.execute(select(Task))).scalars().one().authorized_at is None


async def test_a_plan_cannot_be_authorized_twice(service) -> None:
    task_id = await planned(service, step("a", 1))
    assert (await service.authorize_plan(task_id)).ok

    again = await service.authorize_plan(task_id)
    assert again.outcome is TaskOutcome.REFUSED
    assert again.reason == "already_authorized"


async def test_a_task_with_no_plan_cannot_be_authorized(service) -> None:
    created = await service.create_for_user("No plan")
    result = await service.authorize_plan(created.task_id)
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "no_plan_attached"


async def test_a_terminal_task_cannot_be_authorized(service) -> None:
    task_id = await planned(service, step("a", 1))
    await service.cancel(task_id)
    result = await service.authorize_plan(task_id)
    assert result.outcome is TaskOutcome.TERMINAL


async def test_another_owner_cannot_authorize(db_session, execution_settings) -> None:
    mine = TaskService(db_session, settings=execution_settings)
    task_id = await planned(mine, step("a", 1))

    theirs = TaskService(
        db_session, settings=execution_settings,
        owner_id=uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb"),
    )
    assert (await theirs.authorize_plan(task_id)).outcome is TaskOutcome.NOT_FOUND
    assert (await db_session.execute(select(Task))).scalars().one().authorized_at is None


# ============================================================================
# C. Dependency-aware readiness
# ============================================================================


async def test_only_dependency_free_steps_are_runnable(service) -> None:
    task_id = await planned(
        service, step("a", 1), step("b", 2, ["a"]), step("c", 3, ["a", "b"])
    )
    await service.authorize_plan(task_id)

    assert [s.step_key for s in await service.runnable_steps(task_id)] == ["a"]


async def test_completing_a_dependency_unlocks_its_dependent(service) -> None:
    task_id = await planned(
        service, step("a", 1), step("b", 2, ["a"]), step("c", 3, ["a", "b"])
    )
    await service.authorize_plan(task_id)

    await service.mark_step_started(task_id, "a")
    await service.mark_step_completed(task_id, "a")
    assert [s.step_key for s in await service.runnable_steps(task_id)] == ["b"]

    await service.mark_step_started(task_id, "b")
    await service.mark_step_completed(task_id, "b")
    assert [s.step_key for s in await service.runnable_steps(task_id)] == ["c"]


async def test_a_failed_dependency_never_unlocks_its_dependent(service) -> None:
    """A dependent of a failed step cannot become runnable by waiting."""
    task_id = await planned(service, step("a", 1), step("b", 2, ["a"]))
    await service.authorize_plan(task_id)

    await service.mark_step_started(task_id, "a")
    await service.mark_step_failed(task_id, "a", reason="provider_failed")

    assert await service.runnable_steps(task_id) == []


async def test_a_blocked_step_cannot_be_started(service, db_session) -> None:
    task_id = await planned(service, step("a", 1), step("b", 2, ["a"]))
    await service.authorize_plan(task_id)

    result = await service.mark_step_started(task_id, "b")
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "dependencies_incomplete"

    blocked = (await db_session.execute(
        select(TaskStep).where(TaskStep.step_key == "b")
    )).scalars().one()
    assert blocked.state is TaskStepState.PENDING
    assert blocked.started_at is None


async def test_readiness_follows_the_graph_not_the_sequence(service) -> None:
    """Two independent steps are both runnable, whatever their order."""
    task_id = await planned(
        service, step("a", 1), step("b", 2), step("c", 3, ["a", "b"])
    )
    await service.authorize_plan(task_id)
    assert [s.step_key for s in await service.runnable_steps(task_id)] == ["a", "b"]


async def test_an_unauthorized_plan_has_no_runnable_steps(service) -> None:
    """Readiness is downstream of permission."""
    task_id = await planned(service, step("a", 1))
    assert await service.runnable_steps(task_id) == []


# ============================================================================
# D. Execution creation
# ============================================================================


async def test_a_ready_step_creates_exactly_one_execution(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)

    result = await service.create_step_execution(task_id, "a")
    assert result.outcome is TaskOutcome.UPDATED

    executions = (await db_session.execute(select(Execution))).scalars().all()
    assert len(executions) == 1
    assert executions[0].tool_name == REAL
    # Proposed, never run. Approving is the existing lifecycle's.
    assert executions[0].state is ExecutionState.PROPOSED

    step_row = (await db_session.execute(select(TaskStep))).scalars().one()
    assert step_row.execution_id == executions[0].id


async def test_creating_an_execution_twice_creates_one(service, db_session) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)

    first = await service.create_step_execution(task_id, "a")
    second = await service.create_step_execution(task_id, "a")

    assert first.outcome is TaskOutcome.UPDATED
    assert second.reason == "execution_already_created"
    count = (
        await db_session.execute(select(func.count()).select_from(Execution))
    ).scalar()
    assert count == 1


async def test_an_unauthorized_plan_creates_no_execution(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1))
    result = await service.create_step_execution(task_id, "a")

    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "plan_not_authorized"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_a_dependency_blocked_step_creates_no_execution(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1), step("b", 2, ["a"]))
    await service.authorize_plan(task_id)

    result = await service.create_step_execution(task_id, "b")
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "dependencies_incomplete"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_the_execution_carries_the_bound_capability_not_the_plan_text(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1, capability=" Web_Search "))
    await service.authorize_plan(task_id)
    await service.create_step_execution(task_id, "a")

    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.tool_name == "web_search"


async def test_an_unknown_step_creates_no_execution(service) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)
    result = await service.create_step_execution(task_id, "nowhere")
    assert result.outcome is TaskOutcome.NOT_FOUND
    assert result.reason == "step_not_found"


# ============================================================================
# E. Step lifecycle
# ============================================================================


async def test_the_step_lifecycle_records_times_and_events(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)

    await service.mark_step_started(task_id, "a")
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    assert row.state is TaskStepState.RUNNING
    assert row.started_at is not None
    assert row.completed_at is None

    await service.mark_step_completed(task_id, "a")
    await db_session.refresh(row)
    assert row.state is TaskStepState.COMPLETED
    assert row.completed_at is not None

    kinds = [e.event_type.value for e in (
        await db_session.execute(select(TaskEvent).order_by(TaskEvent.sequence))
    ).scalars().all()]
    assert "step_started" in kinds and "step_completed" in kinds


async def test_a_completed_step_cannot_be_moved_again(service) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)
    await service.mark_step_started(task_id, "a")
    await service.mark_step_completed(task_id, "a")

    for again in (service.mark_step_started, service.mark_step_completed):
        result = await again(task_id, "a")
        assert result.outcome is TaskOutcome.TERMINAL
        assert result.reason == "step_is_terminal"


async def test_a_step_cannot_complete_without_starting(service) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)

    result = await service.mark_step_completed(task_id, "a")
    assert result.outcome is TaskOutcome.INVALID_TRANSITION
    assert result.reason == "undeclared_step_transition"


async def test_step_lifecycle_needs_an_authorized_plan(service) -> None:
    task_id = await planned(service, step("a", 1))
    result = await service.mark_step_started(task_id, "a")
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "plan_not_authorized"


# ============================================================================
# F. The task itself never reaches an executing state
# ============================================================================


async def test_the_task_never_reaches_running_or_completed(
    service, db_session
) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)
    await service.create_step_execution(task_id, "a")
    await service.mark_step_started(task_id, "a")
    await service.mark_step_completed(task_id, "a")

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.AWAITING_APPROVAL
    assert row.completed_at is None
    assert row.current_step is None
    assert row.spent == {
        "max_steps": 0, "max_tool_calls": 0, "max_model_calls": 0,
        "max_seconds": 0,
    }

    for state in (TaskState.RUNNING, TaskState.COMPLETED):
        result = await service.transition(task_id, state)
        assert result.outcome is TaskOutcome.REFUSED
        assert result.reason == "state_not_reachable_in_this_stage"


async def test_no_execution_is_ever_run(service, db_session) -> None:
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)
    await service.create_step_execution(task_id, "a")
    await service.mark_step_started(task_id, "a")

    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.state is ExecutionState.PROPOSED
    assert execution.started_at is None
    assert execution.completed_at is None
    assert execution.result_summary is None


# ============================================================================
# G. Restart and persistence
# ============================================================================


async def test_everything_survives_a_restart(service, db_session) -> None:
    task_id = await planned(service, step("a", 1), step("b", 2, ["a"]))
    await service.authorize_plan(task_id)
    await service.create_step_execution(task_id, "a")
    await service.mark_step_started(task_id, "a")
    await service.mark_step_completed(task_id, "a")
    await db_session.commit()

    fresh = TaskService(db_session, settings=service._settings)
    task = await fresh.get_detail(task_id)
    assert task.authorized_at is not None
    by_key = {s.step_key: s for s in task.steps}
    assert by_key["a"].state is TaskStepState.COMPLETED
    assert by_key["a"].execution_id is not None
    assert by_key["a"].capability == REAL
    # And readiness recomputes from the graph after the restart.
    assert [s.step_key for s in await fresh.runnable_steps(task_id)] == ["b"]


async def test_a_deployment_that_cannot_execute_creates_no_execution(
    db_session, settings
) -> None:
    """Mai's shipping configuration. Authorising still works; creating does not.

    Worth its own test because the two are separable: a plan can be bound and
    authorised on a deployment that has no execution enabled at all, and the
    refusal has to come from the execution service rather than from anything
    the task layer decides for itself.
    """
    off = TaskService(db_session, settings=settings)
    assert off._settings.EXECUTION_ENABLED is False

    task_id = await planned(off, step("a", 1))
    assert (await off.authorize_plan(task_id)).ok

    result = await off.create_step_execution(task_id, "a")
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "execution_disabled"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


# ============================================================================
# H. Gaps found by mutation testing
# ============================================================================


async def test_a_plan_cannot_be_authorized_from_a_blocked_state(
    service, db_session
) -> None:
    """A4: every test authorised from `planned`, so the state check was moot.

    `planned -> blocked` is a declared edge, so a task can hold a valid plan
    and still not be in a state where authorising it makes sense.
    """
    task_id = await planned(service, step("a", 1))
    await service.transition(task_id, TaskState.BLOCKED, reason="waiting")

    result = await service.authorize_plan(task_id)
    assert result.outcome is TaskOutcome.INVALID_TRANSITION
    assert result.reason == "cannot_authorize_from_state"

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.authorized_at is None


async def test_a_step_without_a_bound_capability_creates_no_execution(
    service, db_session
) -> None:
    """E3: unreachable through the normal path, so it needed a direct one.

    Authorisation is all-or-nothing, so every step of an authorised plan has
    a capability. The guard exists for a future path that authorises without
    binding, and this reaches that state deliberately.
    """
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)

    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.capability = None
    await db_session.flush()

    result = await service.create_step_execution(task_id, "a")
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "step_declares_no_capability"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_the_execution_key_is_derived_from_the_step_not_generated(
    service, db_session
) -> None:
    """E4: the `execution_id` guard hid the idempotency key entirely.

    A derived key means the same step of the same task is one action however
    many times it is requested -- including after the link between them is
    lost. A generated key would make every retry a new execution.
    """
    task_id = await planned(service, step("a", 1))
    await service.authorize_plan(task_id)
    assert (await service.create_step_execution(task_id, "a")).ok

    first = (await db_session.execute(select(Execution))).scalars().one()
    # Lose the link, as a crash between the two writes would.
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.execution_id = None
    await db_session.flush()

    assert (await service.create_step_execution(task_id, "a")).ok
    executions = (await db_session.execute(select(Execution))).scalars().all()
    assert len(executions) == 1, "a retry created a second execution"
    assert executions[0].id == first.id

    await db_session.refresh(row)
    assert row.execution_id == first.id


def test_a_step_may_not_supply_more_arguments_than_the_bound() -> None:
    """X3: the bound was never exercised from a test."""
    from pydantic import ValidationError

    from app.planning import limits

    assert limits.MAX_ARGUMENT_KEYS == 20
    # 21 keys, one past the bound. Literal, not derived from the constant.
    with pytest.raises(ValidationError):
        PlanTask(
            id="a", title="A", order=1, depth=0, capability=REAL,
            arguments={f"k{n}": 1 for n in range(21)},
        )
    PlanTask(
        id="a", title="A", order=1, depth=0, capability=REAL,
        arguments={f"k{n}": 1 for n in range(20)},
    )


def test_an_oversized_argument_value_is_refused() -> None:
    """X4: refused rather than truncated -- a shortened argument is a
    different call from the one the plan described."""
    from pydantic import ValidationError

    from app.planning import limits

    assert limits.MAX_ARGUMENT_VALUE_CHARS == 2_000
    with pytest.raises(ValidationError):
        PlanTask(
            id="a", title="A", order=1, depth=0, capability=REAL,
            arguments={"query": "x" * 2001},
        )
    PlanTask(
        id="a", title="A", order=1, depth=0, capability=REAL,
        arguments={"query": "x" * 2000},
    )


def test_a_whitespace_only_capability_is_stored_as_none() -> None:
    """C11's remaining behaviour: emptiness, which `canonical` does not own."""
    assert PlanTask(id="a", title="A", order=1, depth=0, capability="   ").capability is None
    assert PlanTask(id="a", title="A", order=1, depth=0, capability="\t\n ").capability is None
    # Case is left alone here; the registry folds it at lookup.
    assert PlanTask(
        id="a", title="A", order=1, depth=0, capability=" Web_Search "
    ).capability == "Web_Search"
