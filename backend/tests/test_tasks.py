"""Stage 6A: the task model, its state machine, and the activity stream.

Nothing here executes a task, because nothing in Stage 6A can. Several tests
exist specifically to prove that: the states a runner would use are declared
but unreachable, and the events a runner would write are refused.

Expectations are literal wherever the value matters. A test that computes its
bound from the constant it guards cannot fail when the bound is widened --
the defect mutation testing found in 5D.2, 5F.1 and 5F.2.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models.conversation import Conversation
from app.planning.schemas import Goal, IntentType, Plan, PlanTask, Priority as PlanPriority
from app.tasks import events as journal
from app.tasks.models import (
    LOCAL_OWNER_ID,
    MAX_OBJECTIVE_CHARS,
    Priority,
    Task,
    TaskEvent,
    TaskEventType,
    TaskOrigin,
    TaskStep,
)
from app.tasks.schemas import DEFAULT_BUDGET, TaskOutcome
from app.tasks.service import MAX_LISTED, MAX_STEPS, TaskService
from app.tasks.states import (
    ALLOWED_STEP_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    STAGE_6A_REACHABLE,
    TERMINAL_STATES,
    WAITING_STATES,
    TaskState,
    TaskStepState,
    can_transition,
    is_terminal,
)

pytestmark = pytest.mark.anyio

CONVERSATION = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000aaaa")


@pytest.fixture
def service(db_session, settings) -> TaskService:
    return TaskService(db_session, settings=settings)


@pytest.fixture
async def conversation(db_session):
    db_session.add(Conversation(id=CONVERSATION, title="test"))
    await db_session.flush()
    await db_session.commit()


def a_plan(step_count: int = 3, assumptions=None) -> Plan:
    """A validated plan, built the way the planner builds one."""
    return Plan(
        goal=Goal(summary="Do the thing", source_intent=IntentType.ACTION),
        tasks=[
            PlanTask(
                id=f"step-{n}",
                title=f"Step {n}",
                priority=PlanPriority.MEDIUM,
                dependencies=[f"step-{n - 1}"] if n > 1 else [],
                order=n,
                depth=n - 1,
            )
            for n in range(1, step_count + 1)
        ],
        assumptions=list(assumptions or []),
    )


# --- Creation ---------------------------------------------------------------


async def test_a_user_turn_creates_a_task(service, db_session, conversation) -> None:
    result = await service.create_for_user(
        "Plan my trip to Kerala", conversation_id=CONVERSATION
    )

    assert result.outcome is TaskOutcome.CREATED
    assert result.state is TaskState.PROPOSED

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.objective == "Plan my trip to Kerala"
    assert row.origin is TaskOrigin.USER
    assert row.owner_id == LOCAL_OWNER_ID
    assert row.conversation_id == CONVERSATION
    # Nothing has run, so nothing has been spent.
    assert row.spent == {k: 0 for k in DEFAULT_BUDGET}
    assert row.plan is None
    assert row.current_step is None


async def test_creation_writes_exactly_one_event(service, db_session) -> None:
    result = await service.create_for_user("Book a dentist appointment")

    rows = (await db_session.execute(select(TaskEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type is TaskEventType.TASK_CREATED
    assert rows[0].actor == "user"
    assert rows[0].sequence == 1
    assert rows[0].task_id == result.task_id


async def test_the_objective_is_never_in_the_event_metadata(
    service, db_session
) -> None:
    """A task objective is the user's own words and may name anyone."""
    await service.create_for_user("Call Dr Chen about the biopsy results")

    event = (await db_session.execute(select(TaskEvent))).scalars().one()
    blob = str(event.event_metadata).lower()
    for word in ("chen", "biopsy", "call"):
        assert word not in blob, event.event_metadata
    assert event.event_metadata["objective_chars"] == 37


async def test_an_empty_objective_is_refused(service, db_session) -> None:
    for empty in ("", "   ", "\n\t "):
        result = await service.create_for_user(empty)
        assert result.outcome is TaskOutcome.REFUSED
        assert result.reason == "empty_objective"
    assert (await db_session.execute(select(Task))).scalars().all() == []


async def test_an_oversized_objective_is_refused_not_truncated(
    service, db_session
) -> None:
    """Truncating would leave a task working towards a different request."""
    # Literal-pinned: 2001 characters, one past the declared bound.
    result = await service.create_for_user("x" * 2001)
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "objective_too_long"
    assert (await db_session.execute(select(Task))).scalars().all() == []

    assert (await service.create_for_user("y" * 2000)).outcome is TaskOutcome.CREATED


def test_the_objective_bound_is_what_it_says() -> None:
    assert MAX_OBJECTIVE_CHARS == 2_000


# --- Budget -----------------------------------------------------------------


async def test_a_task_is_created_with_the_default_budget(service, db_session) -> None:
    await service.create_for_user("Something")
    row = (await db_session.execute(select(Task))).scalars().one()
    # Literal, so raising a ceiling has to be argued for here.
    assert row.budget == {
        "max_steps": 20,
        "max_tool_calls": 40,
        "max_model_calls": 20,
        "max_seconds": 900,
    }


async def test_a_caller_may_tighten_a_budget(service, db_session) -> None:
    result = await service.create_for_user("Something", budget={"max_steps": 3})
    assert result.outcome is TaskOutcome.CREATED
    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.budget["max_steps"] == 3
    assert row.budget["max_tool_calls"] == 40


@pytest.mark.parametrize("bad", [
    {"max_steps": 9999},          # louder than the ceiling
    {"max_steps": 0},             # not positive
    {"max_steps": -1},
    {"max_steps": True},          # a bool is not a count
    {"max_steps": "many"},
    {"unbounded": 1},             # a key nothing enforces
    {"max_tool_calls": 41},
])
async def test_a_budget_cannot_be_widened_or_invented(bad, service, db_session) -> None:
    result = await service.create_for_user("Something", budget=bad)
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "invalid_budget"
    assert (await db_session.execute(select(Task))).scalars().all() == []


# --- The plan ---------------------------------------------------------------


async def test_attaching_a_plan_materialises_its_steps(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    result = await service.attach_plan(created.task_id, a_plan(3))

    assert result.outcome is TaskOutcome.UPDATED
    assert result.state is TaskState.PLANNED

    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    assert [s.step_key for s in steps] == ["step-1", "step-2", "step-3"]
    assert [s.sequence for s in steps] == [1, 2, 3]
    assert all(s.state is TaskStepState.PENDING for s in steps)
    assert steps[1].depends_on == ["step-1"]
    # A reference to an execution, not a copy of one -- and nothing has run.
    assert all(s.execution_id is None for s in steps)


async def test_the_stored_plan_is_the_validated_one(service, db_session) -> None:
    """Not the raw proposal: what is persisted is what the application accepted."""
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(created.task_id, a_plan(2))

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.plan is not None
    assert row.plan["goal"]["summary"] == "Do the thing"
    assert [t["id"] for t in row.plan["tasks"]] == ["step-1", "step-2"]
    # The accepted plan carries the topological order the validator computed.
    assert [t["order"] for t in row.plan["tasks"]] == [1, 2]


async def test_plan_assumptions_become_events_not_a_column(
    service, db_session
) -> None:
    """The plan already owns its assumptions; a second copy is a second truth."""
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(
        created.task_id, a_plan(1, assumptions=["by 'recent' I mean 30 days"])
    )

    recorded = (await db_session.execute(
        select(TaskEvent).where(
            TaskEvent.event_type == TaskEventType.ASSUMPTION_RECORDED
        )
    )).scalars().all()
    assert len(recorded) == 1
    assert "30 days" in recorded[0].event_metadata["assumption"]
    assert not hasattr(Task, "assumptions")


async def test_a_plan_is_attached_once(service) -> None:
    created = await service.create_for_user("Do the thing")
    assert (await service.attach_plan(created.task_id, a_plan(2))).ok

    again = await service.attach_plan(created.task_id, a_plan(3))
    assert again.outcome is TaskOutcome.REFUSED
    assert again.reason == "plan_already_attached"


async def test_an_empty_plan_is_refused(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")

    class Empty:
        tasks: list = []
        assumptions: list = []

    result = await service.attach_plan(created.task_id, Empty())
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "empty_plan"
    assert (await db_session.execute(select(TaskStep))).scalars().all() == []


async def test_a_plan_cannot_exceed_the_step_bound(service) -> None:
    created = await service.create_for_user("Do the thing")

    class Oversized:
        # 21 steps, one past the bound. Literal, not derived from MAX_STEPS.
        tasks = [
            type("S", (), {"id": f"s{n}", "title": "t", "dependencies": [], "order": n})()
            for n in range(1, 22)
        ]
        assumptions: list = []

    result = await service.attach_plan(created.task_id, Oversized())
    assert result.outcome is TaskOutcome.REFUSED
    # The planner's reason, not a second one. Stage 6B removed the service's
    # duplicate size check: the validator owns plan size and refuses first.
    assert result.reason == "too_many_tasks"


def test_the_step_bound_matches_the_planner() -> None:
    """The two must agree, or a valid plan becomes an unattachable one."""
    from app.planning import limits

    assert MAX_STEPS == 20
    assert limits.MAX_TASKS == 20


# --- The state machine ------------------------------------------------------


def test_the_transition_table_is_exactly_this() -> None:
    """Literal-pinned. Widening the state machine must be a deliberate edit."""
    actual = {
        state.value: sorted(t.value for t in targets)
        for state, targets in ALLOWED_TRANSITIONS.items()
    }
    assert actual == {
        "proposed": ["blocked", "cancelled", "failed", "planned"],
        "planned": ["awaiting_approval", "blocked", "cancelled", "failed", "queued"],
        "awaiting_approval": ["blocked", "cancelled", "failed", "queued"],
        "queued": ["blocked", "cancelled", "paused", "running"],
        "running": [
            "awaiting_approval", "blocked", "cancelled", "completed",
            "failed", "paused",
        ],
        "paused": ["cancelled", "queued"],
        "blocked": ["awaiting_approval", "cancelled", "failed", "queued"],
        "completed": [],
        "failed": [],
        "cancelled": [],
    }


def test_the_step_transition_table_is_exactly_this() -> None:
    actual = {
        state.value: sorted(t.value for t in targets)
        for state, targets in ALLOWED_STEP_TRANSITIONS.items()
    }
    assert actual == {
        "pending": ["cancelled", "running", "skipped"],
        "running": ["cancelled", "completed", "failed"],
        "completed": [],
        "failed": [],
        "skipped": [],
        "cancelled": [],
    }


def test_the_terminal_states_are_exactly_three() -> None:
    assert sorted(s.value for s in TERMINAL_STATES) == [
        "cancelled", "completed", "failed"
    ]


def test_no_terminal_state_has_an_exit() -> None:
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
        for target in TaskState:
            assert not can_transition(state, target), (state, target)


def test_a_task_cannot_be_completed_without_having_run() -> None:
    """The structural reason Stage 6A cannot fabricate a finished task."""
    into_completed = [
        s.value for s, targets in ALLOWED_TRANSITIONS.items()
        if TaskState.COMPLETED in targets
    ]
    assert into_completed == ["running"]


def test_every_state_appears_in_the_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(TaskState)
    assert set(ALLOWED_STEP_TRANSITIONS) == set(TaskStepState)


def test_the_waiting_states_are_exactly_these() -> None:
    assert sorted(s.value for s in WAITING_STATES) == [
        "awaiting_approval", "blocked", "paused"
    ]


# --- Transitions through the service ---------------------------------------


async def test_a_declared_transition_is_performed(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    result = await service.transition(created.task_id, TaskState.BLOCKED,
                                      reason="needs_gmail")

    assert result.outcome is TaskOutcome.UPDATED
    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.BLOCKED


async def test_a_state_outside_this_stage_is_refused(service, db_session) -> None:
    """Refused by the stage boundary, before the transition table is read."""
    created = await service.create_for_user("Do the thing")
    # `awaiting_approval` became reachable in 6C, so the state that proves
    # the boundary is one execution would have to reach.
    result = await service.transition(created.task_id, TaskState.RUNNING)

    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "state_not_reachable_in_this_stage"
    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.PROPOSED


async def test_an_undeclared_transition_is_refused(service, db_session) -> None:
    """A move between two states this stage *can* reach, with no edge.

    The distinction matters and mutation testing found it: the first version
    of this test used `proposed -> awaiting_approval`, which the stage
    boundary refuses first -- so removing the transition guard entirely broke
    nothing any test could see. Both states below are in
    `STAGE_6A_REACHABLE`, so only the transition table can refuse them.
    """
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(created.task_id, a_plan(1))

    # planned -> proposed is not an edge: a task does not become unplanned.
    result = await service.transition(created.task_id, TaskState.PROPOSED)
    assert result.outcome is TaskOutcome.INVALID_TRANSITION
    assert result.reason == "undeclared_transition"

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.PLANNED


async def test_every_undeclared_pair_within_this_stage_is_refused(
    service, db_session
) -> None:
    """Exhaustive over the states Stage 6A can actually produce."""
    from app.tasks.states import RUNNER_ONLY_STATES

    for start in (TaskState.PROPOSED, TaskState.PLANNED, TaskState.BLOCKED):
        for target in STAGE_6A_REACHABLE:
            if target is start or can_transition(start, target):
                continue
            if target in RUNNER_ONLY_STATES:
                # Refused earlier, by the runner-only gate, with its own
                # reason. Covered by its own test above.
                continue
            created = await service.create_for_user("Probe")
            if start is not TaskState.PROPOSED:
                if start is TaskState.PLANNED:
                    await service.attach_plan(created.task_id, a_plan(1))
                else:
                    await service.transition(created.task_id, TaskState.BLOCKED)

            result = await service.transition(created.task_id, target)
            assert result.outcome is TaskOutcome.INVALID_TRANSITION, (start, target)
            assert result.reason == "undeclared_transition", (start, target)

            row = await service.get(created.task_id)
            assert row.state is start, (start, target)


async def test_a_terminal_task_cannot_be_moved(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    assert (await service.cancel(created.task_id)).outcome is TaskOutcome.CANCELLED

    for target in (TaskState.PLANNED, TaskState.BLOCKED, TaskState.FAILED):
        result = await service.transition(created.task_id, target)
        assert result.outcome is TaskOutcome.TERMINAL, target

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.CANCELLED


async def test_cancellation_stamps_a_time_and_cancels_pending_steps(
    service, db_session
) -> None:
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(created.task_id, a_plan(3))
    await service.cancel(created.task_id, reason="user_changed_mind")

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.CANCELLED
    assert row.cancelled_at is not None

    steps = (await db_session.execute(select(TaskStep))).scalars().all()
    assert all(s.state is TaskStepState.CANCELLED for s in steps)


async def test_failure_records_a_reason_and_counts(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    result = await service.transition(
        created.task_id, TaskState.FAILED, actor="system", reason="planning_failed"
    )

    assert result.outcome is TaskOutcome.UPDATED
    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.FAILED
    assert row.error_code == "planning_failed"
    assert row.failure_count == 1
    assert row.completed_at is None


# --- The no-execution boundary ---------------------------------------------


@pytest.mark.parametrize("state", [TaskState.RUNNING, TaskState.COMPLETED])
async def test_the_service_cannot_reach_a_runner_only_state(
    state, service, db_session
) -> None:
    """The whole point of the stage, asserted per state."""
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(created.task_id, a_plan(2))

    result = await service.transition(created.task_id, state)
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "state_not_reachable_in_this_stage"

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.PLANNED


def test_the_reachable_set_is_exactly_this() -> None:
    """Widened by Stage 6C, deliberately.

    Stage 6A pinned five states and said a stage that started moving a task
    towards execution would have to change this line. 6C is that stage:
    authorising a plan reaches `awaiting_approval` or `queued`.

    `running` and `completed` are still absent, and that is the boundary
    that matters -- reaching either means a step actually ran.
    """
    # Stage 6D widened this to every state, because a runner exists. What
    # bounds execution is no longer *which* states are reachable but *who*
    # may reach them: `RUNNER_ONLY_STATES` is refused to every caller of
    # `TaskService.transition`, and only `TaskRunner` writes them.
    assert sorted(s.value for s in STAGE_6A_REACHABLE) == sorted(
        s.value for s in TaskState
    )
    from app.tasks.states import RUNNER_ONLY_STATES

    assert sorted(s.value for s in RUNNER_ONLY_STATES) == ["completed", "running"]


#: Still refused after Stage 6D. Both describe observe-and-replan, which no
#: stage has built -- so a journal entry for either would record something
#: that did not happen.
@pytest.mark.parametrize("event_type", [
    # `observation_recorded` became writable in Stage 6G, when the runner
    # began recording what a monitoring check observed.
    TaskEventType.REPLANNED,
])
async def test_an_execution_event_cannot_be_recorded(
    event_type, service, db_session
) -> None:
    """A journal entry for something that did not happen is the worst kind."""
    created = await service.create_for_user("Do the thing")
    with pytest.raises(journal.TaskEventRefused):
        await journal.record(db_session, created.task_id, event_type)


async def test_nothing_increments_spent(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(created.task_id, a_plan(3))
    await service.transition(created.task_id, TaskState.BLOCKED)

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.spent == {k: 0 for k in DEFAULT_BUDGET}
    assert all(v == 0 for v in row.spent.values())


# --- Ownership ---------------------------------------------------------------


async def test_a_task_always_has_an_owner(service, db_session) -> None:
    await service.create_for_user("Do the thing")
    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.owner_id is not None
    assert row.owner_id == LOCAL_OWNER_ID


async def test_another_owner_cannot_read_a_task(db_session, settings) -> None:
    mine = TaskService(db_session, settings=settings)
    created = await mine.create_for_user("My private task")

    theirs = TaskService(
        db_session, settings=settings,
        owner_id=uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb"),
    )
    assert await theirs.get(created.task_id) is None
    assert await theirs.get_detail(created.task_id) is None
    assert await theirs.steps_for(created.task_id) == []
    assert await theirs.events_for(created.task_id) == []
    assert (await theirs.list_tasks())[0] == []


async def test_another_owner_cannot_change_a_task(db_session, settings) -> None:
    mine = TaskService(db_session, settings=settings)
    created = await mine.create_for_user("My private task")

    theirs = TaskService(
        db_session, settings=settings,
        owner_id=uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb"),
    )
    assert (await theirs.cancel(created.task_id)).outcome is TaskOutcome.NOT_FOUND
    assert (
        await theirs.attach_plan(created.task_id, a_plan(1))
    ).outcome is TaskOutcome.NOT_FOUND

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.PROPOSED


# --- The journal -------------------------------------------------------------


async def test_events_are_sequenced_monotonically(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    await service.attach_plan(created.task_id, a_plan(2, assumptions=["one", "two"]))
    await service.cancel(created.task_id)

    events = (await db_session.execute(
        select(TaskEvent).order_by(TaskEvent.sequence)
    )).scalars().all()
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert [e.event_type.value for e in events] == [
        "task_created", "plan_attached", "assumption_recorded",
        "assumption_recorded", "state_changed", "task_cancelled",
    ]


async def test_each_task_has_its_own_sequence(service, db_session) -> None:
    """Sequences are per task, not global.

    Mutation testing found this: every earlier event test used a single
    task, and with one task a per-task `max(sequence)` and a global one are
    the same number. Two tasks tell them apart.
    """
    first = await service.create_for_user("First task")
    second = await service.create_for_user("Second task")
    await service.attach_plan(first.task_id, a_plan(1))
    await service.attach_plan(second.task_id, a_plan(1))
    await service.cancel(second.task_id)

    for task_id, expected in (
        (first.task_id, [1, 2, 3]),
        (second.task_id, [1, 2, 3, 4]),
    ):
        rows = (await db_session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.sequence)
        )).scalars().all()
        assert [e.sequence for e in rows] == expected, task_id
        # Every task's journal starts at one, whatever else exists.
        assert rows[0].sequence == 1


async def test_an_unknown_actor_is_refused(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    for actor in ("model", "assistant", "gmail", ""):
        with pytest.raises(journal.TaskEventRefused):
            await journal.record(
                db_session, created.task_id, TaskEventType.STATE_CHANGED,
                actor=actor,
            )


async def test_event_metadata_is_redacted(service, db_session) -> None:
    created = await service.create_for_user("Do the thing")
    await journal.record(
        db_session, created.task_id, TaskEventType.STATE_CHANGED,
        metadata={
            "api_key": "sk-secret", "password": "hunter2",
            "authorization": "Bearer xyz", "safe_count": 3,
        },
    )
    event = (await db_session.execute(
        select(TaskEvent).order_by(TaskEvent.sequence.desc())
    )).scalars().first()

    assert event.event_metadata == {"safe_count": 3}


# --- Activity ----------------------------------------------------------------


async def test_each_activity_question_is_answerable(service) -> None:
    for key in ("did", "doing", "will_do", "failed", "waiting_for", "needs_approval"):
        question, states, tasks, total = await service.activity(key)
        assert question.endswith("?")
        assert total == 0
        assert tasks == []


async def test_activity_reflects_what_is_recorded(service) -> None:
    planned = await service.create_for_user("A planned task")
    await service.attach_plan(planned.task_id, a_plan(1))
    blocked = await service.create_for_user("A blocked task")
    await service.transition(blocked.task_id, TaskState.BLOCKED)
    failed = await service.create_for_user("A failed task")
    await service.transition(failed.task_id, TaskState.FAILED, reason="nope")
    await service.create_for_user("A fresh task")

    _, _, will_do, will_total = await service.activity("will_do")
    assert will_total == 2  # proposed + planned
    assert {t.objective for t in will_do} == {"A planned task", "A fresh task"}

    _, _, waiting, waiting_total = await service.activity("waiting_for")
    assert waiting_total == 1
    assert waiting[0].objective == "A blocked task"

    _, _, broken, failed_total = await service.activity("failed")
    assert failed_total == 1
    assert broken[0].error_code == "nope"

    # Nothing has run, so two questions are honestly empty.
    assert (await service.activity("doing"))[3] == 0
    assert (await service.activity("did"))[3] == 0


async def test_listing_is_bounded(service) -> None:
    for n in range(4):
        await service.create_for_user(f"Task {n}")

    tasks, total = await service.list_tasks(limit=2)
    assert len(tasks) == 2
    assert total == 4
    # Literal: the service's own ceiling, not a value read back from it.
    assert MAX_LISTED == 50
    assert len(await service.list_tasks(limit=9999)) == 2


async def test_tasks_survive_and_read_back_whole(service, db_session) -> None:
    created = await service.create_for_user("Persisted work")
    await service.attach_plan(created.task_id, a_plan(2, assumptions=["assumed"]))
    await db_session.commit()

    fresh = TaskService(db_session, settings=service._settings)
    task = await fresh.get_detail(created.task_id)
    assert task is not None
    assert task.objective == "Persisted work"
    assert len(task.steps) == 2
    assert len(task.events) == 4
    assert task.plan["tasks"][0]["id"] == "step-1"
