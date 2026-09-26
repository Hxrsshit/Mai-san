"""Stage 6D: the task runner.

One task, one step, one transition per invocation. Every behavioural test
drives the real `TaskRunner` against the real `ExecutionService`, the real
authorization service and the real dispatcher -- the only thing stubbed is
the Google socket, through the existing calendar transport.

`calendar_list_events` is the capability used throughout, because it is the
one registered tool that is both executable and marked as needing no human
approval. A step whose capability *does* need approval is tested separately,
and blocks.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.execution.models import Execution
from app.execution.states import ExecutionState
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks.models import Task, TaskEvent, TaskStep
from app.tasks.runner import TaskRunner
from app.tasks.schemas import RunnerOutcome, TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState

pytestmark = pytest.mark.anyio

#: Executable, and policy says no person is needed.
AUTO = "calendar_list_events"
AUTO_ARGS = {
    "starts_at": "2026-10-01T00:00:00+00:00",
    "ends_at": "2026-10-02T00:00:00+00:00",
    "max_results": 5,
}
#: Executable, and policy says a person is needed.
GATED = "web_search"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


def goal() -> Goal:
    return Goal(summary="Do the thing", source_intent=IntentType.ACTION)


def step(key, order, deps=(), capability=AUTO, arguments=None) -> PlanTask:
    return PlanTask(
        id=key, title=f"Step {key}", dependencies=list(deps), order=order,
        depth=len(deps), capability=capability,
        arguments=AUTO_ARGS if arguments is None else arguments,
    )


async def authorized_task(service, *steps, objective="Do the thing"):
    """A task with a validated, authorised plan. The runner's precondition."""
    created = await service.create_for_user(objective)
    assert (await service.attach_plan(
        created.task_id, Plan(goal=goal(), tasks=list(steps))
    )).ok
    result = await service.authorize_plan(created.task_id)
    assert result.ok, result
    return created.task_id


# ============================================================================
# A. One step per invocation
# ============================================================================


async def test_a_single_ready_step_runs(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.TASK_COMPLETED
    assert result.step_key == "a"
    assert result.execution_id is not None

    row = (await db_session.execute(select(TaskStep))).scalars().one()
    assert row.state is TaskStepState.COMPLETED
    assert row.started_at is not None and row.completed_at is not None
    assert row.execution_id == result.execution_id

    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.state is ExecutionState.SUCCEEDED
    assert execution.tool_name == AUTO


async def test_one_invocation_advances_exactly_one_step(
    calendar_runner
) -> None:
    """Three independent steps, three invocations -- never two at once."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(
        service, step("a", 1), step("b", 2), step("c", 3)
    )

    first = await runner.advance(task_id)
    assert first.outcome is RunnerOutcome.STEP_COMPLETED
    assert first.step_key == "a"
    done = (await db_session.execute(
        select(func.count()).select_from(TaskStep).where(
            TaskStep.state == TaskStepState.COMPLETED
        )
    )).scalar()
    assert done == 1, "one invocation advanced more than one step"

    assert (await runner.advance(task_id)).step_key == "b"
    last = await runner.advance(task_id)
    assert last.step_key == "c"
    assert last.outcome is RunnerOutcome.TASK_COMPLETED


async def test_the_runner_is_safe_to_call_when_there_is_nothing_to_do(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    await runner.advance(task_id)

    for _ in range(3):
        again = await runner.advance(task_id)
        assert again.outcome is RunnerOutcome.REFUSED
        assert again.reason == "task_is_terminal"

    assert (await db_session.execute(
        select(func.count()).select_from(Execution)
    )).scalar() == 1


# ============================================================================
# B. Dependencies
# ============================================================================


async def test_b_cannot_run_before_a(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2, ["a"]))

    first = await runner.advance(task_id)
    assert first.step_key == "a"

    rows = {s.step_key: s for s in (
        await db_session.execute(select(TaskStep))
    ).scalars().all()}
    assert rows["b"].state is TaskStepState.PENDING
    assert rows["b"].execution_id is None

    second = await runner.advance(task_id)
    assert second.step_key == "b"
    assert second.outcome is RunnerOutcome.TASK_COMPLETED


async def test_a_chain_runs_in_order(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(
        service, step("a", 1), step("b", 2, ["a"]), step("c", 3, ["b"])
    )

    order = []
    for _ in range(3):
        order.append((await runner.advance(task_id)).step_key)
    assert order == ["a", "b", "c"]


async def test_c_requires_both_branches(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(
        service, step("a", 1), step("b", 2), step("c", 3, ["a", "b"])
    )

    assert (await runner.advance(task_id)).step_key == "a"
    # After only A, C is still blocked -- B runs next, not C.
    assert (await runner.advance(task_id)).step_key == "b"
    last = await runner.advance(task_id)
    assert last.step_key == "c"
    assert last.outcome is RunnerOutcome.TASK_COMPLETED


async def test_a_dependent_of_a_failed_step_stays_blocked(
    calendar_runner, db_session
) -> None:
    """No skip-or-abort cascade. The dependent waits, and the task does not
    complete -- documented as a limitation rather than guessed at."""
    service, runner, session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2, ["a"]))

    rows = {s.step_key: s for s in (
        await session.execute(select(TaskStep))
    ).scalars().all()}
    rows["a"].state = TaskStepState.FAILED
    await session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "no_runnable_step"

    task = (await session.execute(select(Task))).scalars().one()
    assert task.state is not TaskState.COMPLETED
    assert (await session.execute(select(Execution))).scalars().all() == []


# ============================================================================
# C. Completion
# ============================================================================


async def test_a_task_completes_only_when_every_step_did(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2))

    first = await runner.advance(task_id)
    assert first.outcome is RunnerOutcome.STEP_COMPLETED
    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.state is not TaskState.COMPLETED
    assert task.completed_at is None

    second = await runner.advance(task_id)
    assert second.outcome is RunnerOutcome.TASK_COMPLETED
    await db_session.refresh(task)
    assert task.state is TaskState.COMPLETED
    assert task.completed_at is not None


async def test_completion_is_read_back_not_inferred(calendar_runner) -> None:
    """The proof is the persisted step states, not what this call did."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2))
    await runner.advance(task_id)

    # Force `b` into a non-completed state behind the runner's back.
    rows = {s.step_key: s for s in (
        await db_session.execute(select(TaskStep))
    ).scalars().all()}
    rows["b"].state = TaskStepState.SKIPPED
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.state is not TaskState.COMPLETED


async def test_only_the_runner_may_complete_a_task(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    for state in (TaskState.RUNNING, TaskState.COMPLETED):
        result = await service.transition(task_id, state)
        assert result.outcome is TaskOutcome.REFUSED
        assert result.reason == "state_not_reachable_in_this_stage"


# ============================================================================
# D. Approval, authorization and capability
# ============================================================================


async def test_a_capability_needing_a_person_blocks_the_runner(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    created = await service.create_for_user("Needs approval")
    await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[
        PlanTask(id="a", title="Search", order=1, depth=0,
                 capability=GATED, arguments={"query": "x"})
    ]))
    authorized = await service.authorize_plan(created.task_id)
    assert authorized.state is TaskState.AWAITING_APPROVAL

    result = await runner.advance(created.task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "task_not_queued"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_the_runner_never_approves_a_gated_capability(
    calendar_runner
) -> None:
    """Reached past the task gate, the step gate still refuses.

    Forced to `queued` so the run reaches the per-step decision. Policy said
    a person is needed for `web_search`; the runner declines to pretend one
    is present, records why, and leaves the step pending for a later
    invocation once someone has approved it themselves.
    """
    service, runner, db_session = calendar_runner
    created = await service.create_for_user("Needs approval")
    await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[
        PlanTask(id="a", title="Search", order=1, depth=0,
                 capability=GATED, arguments={"query": "x"})
    ]))
    await service.authorize_plan(created.task_id)

    task = (await db_session.execute(select(Task))).scalars().one()
    task.state = TaskState.QUEUED
    await db_session.flush()

    result = await runner.advance(created.task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "awaiting_human_approval"

    # The execution exists and was never approved or run.
    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.state is ExecutionState.PROPOSED
    assert execution.approved_at is None

    # And the step went back to pending, not left claimed by nobody.
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    assert row.state is TaskStepState.PENDING
    assert row.started_at is None


async def test_an_unauthorized_task_is_refused(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    created = await service.create_for_user("Not authorised")
    await service.attach_plan(
        created.task_id, Plan(goal=goal(), tasks=[step("a", 1)])
    )

    result = await runner.advance(created.task_id)
    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "plan_not_authorized"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_an_expired_authorization_blocks_execution(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    task = (await db_session.execute(select(Task))).scalars().one()
    ttl = service._settings.TASK_AUTHORIZATION_TTL_SECONDS
    task.authorized_at = datetime.now(timezone.utc) - timedelta(seconds=ttl + 60)
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "authorization_expired"
    assert (await db_session.execute(select(Execution))).scalars().all() == []

    kinds = {e.event_type.value for e in (
        await db_session.execute(select(TaskEvent))
    ).scalars().all()}
    assert "runner_refused" in kinds


async def test_a_capability_withdrawn_after_authorization_is_refused(
    calendar_runner
) -> None:
    """Re-bound at run time, not trusted from authorization time."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.capability = "no_longer_a_tool"
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "unknown_capability"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


# ============================================================================
# E. Terminal states and cancellation
# ============================================================================


@pytest.mark.parametrize("terminal", [
    TaskState.CANCELLED, TaskState.FAILED,
])
async def test_a_terminal_task_never_runs(terminal, calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    if terminal is TaskState.FAILED:
        # `queued -> failed` is not a declared edge, so the task reaches it
        # the way the state machine actually permits.
        await service.transition(task_id, TaskState.BLOCKED, reason="stuck")
    await service.transition(task_id, terminal, reason="test")

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "task_is_terminal"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_cancelling_before_the_claim_prevents_execution(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2))
    await runner.advance(task_id)

    await service.cancel(task_id, reason="user_changed_mind")
    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.REFUSED

    rows = {s.step_key: s for s in (
        await db_session.execute(select(TaskStep))
    ).scalars().all()}
    assert rows["b"].state is TaskStepState.CANCELLED
    assert rows["b"].execution_id is None
    assert (await db_session.execute(
        select(func.count()).select_from(Execution)
    )).scalar() == 1


# ============================================================================
# F. Idempotency, concurrency and restart
# ============================================================================


async def test_repeated_invocation_creates_at_most_one_execution(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    for _ in range(4):
        await runner.advance(task_id)

    assert (await db_session.execute(
        select(func.count()).select_from(Execution)
    )).scalar() == 1


async def test_concurrent_invocations_advance_one_step_once(
    calendar_runner
) -> None:
    """The claim decides. One wins; the other is told the step is taken."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    results = await asyncio.gather(
        runner.advance(task_id), runner.advance(task_id),
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, BaseException)], results

    advanced = [r for r in results if r.advanced]
    assert len(advanced) == 1, [r.outcome.value for r in results]

    assert (await db_session.execute(
        select(func.count()).select_from(Execution)
    )).scalar() == 1
    rows = (await db_session.execute(select(TaskStep))).scalars().all()
    assert len([r for r in rows if r.state is TaskStepState.COMPLETED]) == 1


async def test_state_survives_a_restart_mid_plan(calendar_runner) -> None:
    harness = calendar_runner
    service, runner, db_session = harness
    task_id = await authorized_task(service, step("a", 1), step("b", 2, ["a"]))
    await runner.advance(task_id)
    await db_session.commit()

    # New service and runner objects reading the same rows -- a restart in
    # every sense that matters. The execution service is the harness's,
    # because a default one would reach the real integration registry rather
    # than this test's stubbed socket.
    fresh_service = TaskService(db_session, settings=service._settings)
    fresh_runner = TaskRunner(fresh_service, executions=harness.executions)
    task = await fresh_service.get_detail(task_id)
    by_key = {s.step_key: s for s in task.steps}
    assert by_key["a"].state is TaskStepState.COMPLETED
    assert by_key["a"].execution_id is not None
    assert by_key["b"].state is TaskStepState.PENDING

    result = await fresh_runner.advance(task_id)
    assert result.outcome is RunnerOutcome.TASK_COMPLETED


async def test_a_step_claimed_but_not_finished_is_not_reclaimed(
    calendar_runner
) -> None:
    """A crash after the claim leaves a `running` step, not a lost one.

    Stage 6D deliberately does not reap it: deciding that a claimed step is
    abandoned needs a lease, and a lease is scheduler machinery. The step
    stays `running` and the runner reports the task blocked.
    """
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2, ["a"]))

    row = (await db_session.execute(
        select(TaskStep).where(TaskStep.step_key == "a")
    )).scalars().one()
    row.state = TaskStepState.RUNNING
    row.started_at = datetime.now(timezone.utc)
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "no_runnable_step"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


# ============================================================================
# G. Budget
# ============================================================================


async def test_the_step_budget_stops_the_runner(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    created = await service.create_for_user("Bounded", budget={"max_steps": 1})
    await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[
        step("a", 1), step("b", 2)
    ]))
    await service.authorize_plan(created.task_id)

    first = await runner.advance(created.task_id)
    assert first.outcome is RunnerOutcome.STEP_COMPLETED

    second = await runner.advance(created.task_id)
    assert second.outcome is RunnerOutcome.BUDGET_EXCEEDED
    assert second.reason == "max_steps"

    assert (await db_session.execute(
        select(func.count()).select_from(Execution)
    )).scalar() == 1
    kinds = {e.event_type.value for e in (
        await db_session.execute(select(TaskEvent))
    ).scalars().all()}
    assert "budget_exceeded" in kinds


async def test_spending_is_recorded_as_it_happens(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2))

    await runner.advance(task_id)
    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.spent["max_steps"] == 1
    assert task.spent["max_tool_calls"] == 1
    # Never touched: the runner makes no model call and measures no seconds.
    assert task.spent["max_model_calls"] == 0
    assert task.spent["max_seconds"] == 0

    await runner.advance(task_id)
    await db_session.refresh(task)
    assert task.spent["max_steps"] == 2


def test_only_measurable_bounds_are_enforced() -> None:
    """Claiming to enforce a bound nothing measures would be a fabrication."""
    from types import SimpleNamespace

    over = SimpleNamespace(
        budget={"max_steps": 1, "max_model_calls": 1, "max_seconds": 1},
        spent={"max_steps": 0, "max_model_calls": 99, "max_seconds": 99},
    )
    assert TaskService.budget_exceeded(over) is None

    stepped = SimpleNamespace(
        budget={"max_steps": 2}, spent={"max_steps": 2}
    )
    assert TaskService.budget_exceeded(stepped) == "max_steps"


# ============================================================================
# H. Activity
# ============================================================================


async def test_the_run_is_reconstructable_from_the_journal(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2, ["a"]))
    await runner.advance(task_id)
    await runner.advance(task_id)

    events = (await db_session.execute(
        select(TaskEvent).order_by(TaskEvent.sequence)
    )).scalars().all()
    kinds = [e.event_type.value for e in events]

    assert kinds == [
        "task_created", "plan_attached", "state_changed", "approval_granted",
        "step_started", "execution_created", "step_completed",
        "step_started", "execution_created", "step_completed",
        "task_completed",
    ]
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))


async def test_a_refusal_is_recorded_not_silent(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.capability = "gone"
    await db_session.flush()

    await runner.advance(task_id)
    kinds = [e.event_type.value for e in (
        await db_session.execute(select(TaskEvent).order_by(TaskEvent.sequence))
    ).scalars().all()]
    assert kinds[-1] == "runner_refused"


async def test_the_runner_moves_a_queued_task_to_running(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2))

    assert await runner.mark_task_running(task_id) is True
    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.state is TaskState.RUNNING

    # Idempotent: it is only a queued task's transition.
    assert await runner.mark_task_running(task_id) is False

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.STEP_COMPLETED


# ============================================================================
# I. Ownership
# ============================================================================


async def test_another_owner_cannot_advance_a_task(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    theirs = TaskService(
        db_session, settings=service._settings,
        owner_id=uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb"),
    )
    their_runner = TaskRunner(theirs)
    result = await their_runner.advance(task_id)

    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "task_not_found"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


# ============================================================================
# J. Gaps found by mutation testing
# ============================================================================


async def test_two_runners_on_separate_sessions_claim_a_step_once(
    calendar_runner, session_factory
) -> None:
    """C1-C3: the earlier concurrency test never reached the claim.

    Two runners on one session serialise, so the second saw a step already
    completed rather than contending for it. Separate sessions are the only
    way the conditional UPDATE is actually exercised.
    """
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    await db_session.commit()

    async with session_factory() as first, session_factory() as second:
        _, runner_a = calendar_runner.build(first)
        _, runner_b = calendar_runner.build(second)

        a = await runner_a.advance(task_id)
        await first.commit()
        b = await runner_b.advance(task_id)
        await second.commit()

    assert len([r for r in (a, b) if r.advanced]) == 1, (a.outcome, b.outcome)

    async with session_factory() as session:
        executions = (await session.execute(select(Execution))).scalars().all()
        assert len(executions) == 1
        steps = (await session.execute(select(TaskStep))).scalars().all()
        assert len([s for s in steps if s.state is TaskStepState.COMPLETED]) == 1


async def test_a_lost_claim_stops_the_invocation(
    calendar_runner, monkeypatch
) -> None:
    """The branch a race takes. Forced, because a race is not reproducible."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    async def lost(_step):
        return False

    monkeypatch.setattr(runner, "_claim", lost)
    result = await runner.advance(task_id)

    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "step_already_claimed"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_the_claim_is_conditional_on_the_step_being_pending(
    calendar_runner
) -> None:
    """C1/C2 directly: the same step cannot be claimed twice."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    row = (await db_session.execute(select(TaskStep))).scalars().one()

    assert await runner._claim(row) is True
    assert await runner._claim(row) is False, "a claimed step was claimed again"


async def test_the_execution_key_survives_a_lost_link(
    calendar_runner
) -> None:
    """E1: the step link hid the derived key, as it did in Stage 6C."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    await runner.advance(task_id)

    first = (await db_session.execute(select(Execution))).scalars().one()
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    # A crash between creating the execution and linking it.
    row.execution_id = None
    row.state = TaskStepState.PENDING
    row.completed_at = None
    task = (await db_session.execute(select(Task))).scalars().one()
    task.state = TaskState.QUEUED
    task.completed_at = None
    await db_session.flush()

    await runner.advance(task_id)
    executions = (await db_session.execute(select(Execution))).scalars().all()
    assert len(executions) == 1, "a retry created a second execution"
    assert executions[0].id == first.id


async def test_the_execution_carries_the_bound_name_not_the_stored_string(
    calendar_runner
) -> None:
    """E3: every earlier test had the two already equal.

    Stage 6C writes the canonical name, so `step.capability` and
    `binding.capability` matched in every test. A non-canonical spelling
    written after authorization tells them apart.
    """
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))

    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.capability = "  Calendar_List_Events  "
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.TASK_COMPLETED

    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.tool_name == AUTO

    # Note for anyone reading a mutation report: passing `step.capability`
    # here instead of `binding.capability` is an *equivalent* mutation.
    # `ExecutionService.create` canonicalises the name again through the
    # registry, so both spellings store the same value. The runner passes
    # the bound one because it is the one the application chose, not because
    # the difference is observable -- defence in depth, not a live guard.


async def test_a_failing_tool_marks_the_step_failed(calendar_runner) -> None:
    """X5: no earlier test reached the failure path at all."""
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2, ["a"]))

    # The provider refuses. Everything below the socket is real.
    calendar_runner.transport._status = 500

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.STEP_FAILED
    assert result.step_key == "a"

    row = (await db_session.execute(
        select(TaskStep).where(TaskStep.step_key == "a")
    )).scalars().one()
    assert row.state is TaskStepState.FAILED
    assert row.completed_at is not None

    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.state is not TaskState.COMPLETED

    kinds = [e.event_type.value for e in (
        await db_session.execute(select(TaskEvent).order_by(TaskEvent.sequence))
    ).scalars().all()]
    assert "step_failed" in kinds
    assert "task_completed" not in kinds

    # And the dependent never becomes runnable.
    assert (await runner.advance(task_id)).outcome is RunnerOutcome.BLOCKED


async def test_a_task_with_no_steps_is_never_completed(
    calendar_runner
) -> None:
    """X4: the last guard before `completed` is written.

    Unreachable through any supported path -- `attach_plan` refuses an empty
    plan -- so the state is reached directly. The guard exists because
    writing `completed` is the one claim the runner must never fabricate.
    """
    from sqlalchemy import delete

    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1))
    await db_session.execute(delete(TaskStep).where(TaskStep.task_id == task_id))
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.state is not TaskState.COMPLETED
    assert task.completed_at is None


async def test_a_human_approval_lets_the_runner_proceed(
    calendar_runner
) -> None:
    """The other half of the approval gate, found by live verification.

    The first version checked only what policy said, never whether a person
    had actually approved -- so an approval could never take effect and the
    step blocked forever. The runner still never approves a gated capability
    itself; it notices when someone else has.
    """
    service, runner, db_session = calendar_runner
    created = await service.create_for_user("Search once approved")
    await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[
        PlanTask(id="a", title="Search", order=1, depth=0,
                 capability=GATED, arguments={"query": "x"})
    ]))
    await service.authorize_plan(created.task_id)
    task = (await db_session.execute(select(Task))).scalars().one()
    task.state = TaskState.QUEUED
    await db_session.flush()

    blocked = await runner.advance(created.task_id)
    assert blocked.reason == "awaiting_human_approval"

    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.state is ExecutionState.PROPOSED

    # A person approves that specific execution, through the existing path.
    await runner._executions.approve(execution.id)
    await db_session.refresh(execution)
    assert execution.state is ExecutionState.APPROVED

    # Now the runner proceeds. What is being tested is that the gate opens:
    # the step is attempted rather than blocked again. The stub socket
    # answers a search with a calendar payload, so the tool itself fails --
    # which is the honest outcome and proves the dispatcher was reached.
    result = await runner.advance(created.task_id)
    assert result.reason != "awaiting_human_approval"
    assert result.outcome in {
        RunnerOutcome.STEP_COMPLETED, RunnerOutcome.TASK_COMPLETED,
        RunnerOutcome.STEP_FAILED,
    }, result

    await db_session.refresh(execution)
    assert execution.state is not ExecutionState.APPROVED, "never attempted"
    # And no second execution: the derived key returned the approved one.
    assert (await db_session.execute(
        select(func.count()).select_from(Execution)
    )).scalar() == 1


async def test_a_blocked_invocation_spends_nothing(calendar_runner) -> None:
    """Found by live verification: a two-step task recorded four spent steps.

    An invocation that blocks awaiting approval does no work, so it must
    cost nothing -- otherwise an approval-gated task exhausts its budget
    simply by being polled while it waits.
    """
    service, runner, db_session = calendar_runner
    created = await service.create_for_user("Search once approved")
    await service.attach_plan(created.task_id, Plan(goal=goal(), tasks=[
        PlanTask(id="a", title="Search", order=1, depth=0,
                 capability=GATED, arguments={"query": "x"})
    ]))
    await service.authorize_plan(created.task_id)
    task = (await db_session.execute(select(Task))).scalars().one()
    task.state = TaskState.QUEUED
    await db_session.flush()

    for _ in range(3):
        blocked = await runner.advance(created.task_id)
        assert blocked.reason == "awaiting_human_approval"

    await db_session.refresh(task)
    assert task.spent["max_steps"] == 0, task.spent
    assert task.spent["max_tool_calls"] == 0, task.spent


async def test_spending_counts_work_not_invocations(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await authorized_task(service, step("a", 1), step("b", 2))

    await runner.advance(task_id)
    await runner.advance(task_id)
    # Two steps ran; further invocations are refused and cost nothing.
    for _ in range(3):
        await runner.advance(task_id)

    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.spent["max_steps"] == 2
    assert task.spent["max_tool_calls"] == 2
