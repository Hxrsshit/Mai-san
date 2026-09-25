"""Stage 6B: plan validation, persistence and preview.

Stage 6A attached plans without checking them. Measured before this stage was
written, against the real service: a dependency cycle, a dangling dependency
and a self-dependency were all accepted and materialised into task steps, and
a duplicate sequence surfaced only as a database integrity error reported as
`persistence_failed`. Each of those is a test below.

Nothing here executes. Several tests exist to prove it: `spent` stays zero,
`execution_id` stays NULL, `current_step` stays NULL, and no plan moves a task
into a running state.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.planning.schemas import (
    Goal,
    IntentType,
    Plan,
    PlanProposal,
    PlanTask,
    ProposedTask,
)
from app.tasks.models import Task, TaskEvent, TaskStep
from app.tasks.plans import FORBIDDEN_KEYS, PlanCheck, validate_for_task
from app.tasks.schemas import TaskOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState

pytestmark = pytest.mark.anyio


@pytest.fixture
def service(db_session, settings) -> TaskService:
    return TaskService(db_session, settings=settings)


def goal(summary: str = "Do the thing") -> Goal:
    return Goal(summary=summary, source_intent=IntentType.ACTION)


def plan_of(*steps: PlanTask, **kwargs) -> Plan:
    return Plan(goal=goal(), tasks=list(steps), **kwargs)


def step(key: str, order: int, deps=(), **kwargs) -> PlanTask:
    return PlanTask(
        id=key, title=f"Step {key}", dependencies=list(deps),
        order=order, depth=len(deps), **kwargs,
    )


VALID = (step("a", 1), step("b", 2, ["a"]), step("c", 3, ["a", "b"]))


# ============================================================================
# A. A valid plan persists
# ============================================================================


async def test_a_valid_plan_is_persisted_whole(service, db_session) -> None:
    created = await service.create_for_user("Plan the offsite")
    result = await service.attach_plan(created.task_id, plan_of(*VALID))

    assert result.outcome is TaskOutcome.UPDATED
    assert result.state is TaskState.PLANNED

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.plan is not None
    assert [t["id"] for t in row.plan["tasks"]] == ["a", "b", "c"]
    assert row.plan["goal"]["summary"] == "Do the thing"


async def test_steps_are_materialised_in_order(service, db_session) -> None:
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(*VALID))

    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    assert [s.step_key for s in steps] == ["a", "b", "c"]
    assert [s.sequence for s in steps] == [1, 2, 3]
    assert [s.title for s in steps] == ["Step a", "Step b", "Step c"]


async def test_dependencies_are_preserved(service, db_session) -> None:
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(*VALID))

    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    assert [s.depends_on for s in steps] == [[], ["a"], ["a", "b"]]


async def test_assumptions_are_preserved_without_a_column(
    service, db_session
) -> None:
    """They live in the plan and in the journal. There is no third copy."""
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(
        created.task_id,
        plan_of(*VALID, assumptions=["by 'offsite' I mean the team offsite"]),
    )

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.plan["assumptions"] == ["by 'offsite' I mean the team offsite"]
    assert not hasattr(row, "assumptions")

    recorded = (await db_session.execute(
        select(TaskEvent).where(
            TaskEvent.event_type == "assumption_recorded"
        )
    )).scalars().all()
    assert len(recorded) == 1


async def test_the_budget_is_untouched_by_attaching_a_plan(
    service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite", budget={"max_steps": 5})
    before = (await db_session.execute(select(Task))).scalars().one().budget
    await service.attach_plan(created.task_id, plan_of(*VALID))

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.budget == before
    assert row.budget["max_steps"] == 5


async def test_attaching_a_plan_writes_the_expected_journal(
    service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(*VALID, assumptions=["one"]))

    events = (await db_session.execute(
        select(TaskEvent).order_by(TaskEvent.sequence)
    )).scalars().all()
    assert [e.event_type.value for e in events] == [
        "task_created", "plan_attached", "assumption_recorded", "state_changed",
    ]
    attached = events[1]
    assert attached.event_metadata == {"step_count": 3, "dependency_count": 3}


# ============================================================================
# B. Invalid plans are refused, by name
# ============================================================================


#: Every fault the validation contract names, and the reason it must produce.
#:
#: The first three were measured to be *accepted* by Stage 6A and
#: materialised into steps. They are the reason this stage exists.
REJECTIONS = [
    (
        "dependency_cycle",
        (step("a", 1, ["b"]), step("b", 2, ["a"])),
    ),
    (
        "unknown_dependency",
        (step("a", 1, ["nowhere"]),),
    ),
    (
        "self_dependency",
        (step("a", 1, ["a"]),),
    ),
    (
        "duplicate_step_sequence",
        (step("a", 1), step("b", 1)),
    ),
    (
        "invalid_step_sequence",
        (PlanTask(id="a", title="A", order=0, depth=0),),
    ),
]


@pytest.mark.parametrize("reason, steps", REJECTIONS)
async def test_an_unsound_plan_is_refused_by_name(
    reason, steps, service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite")
    result = await service.attach_plan(created.task_id, plan_of(*steps))

    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == reason

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.plan is None, "a refused plan was persisted"
    assert row.state is TaskState.PROPOSED, "a refused plan changed the state"
    assert (await db_session.execute(select(TaskStep))).scalars().all() == []


async def test_a_duplicate_step_key_is_refused(service, db_session) -> None:
    """The schema catches this first; the graph layer catches it too."""
    created = await service.create_for_user("Plan the offsite")

    class Duplicated:
        tasks = (step("a", 1), step("a", 2))
        assumptions: list = []

        def model_dump(self, mode=None):
            return {"tasks": [{"id": "a"}, {"id": "a"}]}

    result = await service.attach_plan(created.task_id, Duplicated())
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "duplicate_task_id"
    assert (await db_session.execute(select(TaskStep))).scalars().all() == []


async def test_a_step_missing_a_required_field_is_refused(
    service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite")

    class Bare:
        tasks = [type("S", (), {"id": "a", "title": "  ", "dependencies": [],
                                "order": 1})()]
        assumptions: list = []

    result = await service.attach_plan(created.task_id, Bare())
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "step_missing_field"


async def test_an_oversized_plan_is_refused_by_the_planner(service) -> None:
    created = await service.create_for_user("Plan the offsite")
    # 21 steps, one past the planner's bound. Literal, not derived.
    oversized = tuple(step(f"s{n}", n) for n in range(1, 22))
    result = await service.attach_plan(created.task_id, plan_of(*oversized))

    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "too_many_tasks"


async def test_a_refused_plan_leaves_the_journal_clean(service, db_session) -> None:
    """No `plan_attached` event for a plan that was not attached."""
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(step("a", 1, ["a"])))

    events = (await db_session.execute(select(TaskEvent))).scalars().all()
    assert [e.event_type.value for e in events] == ["task_created"]


# ============================================================================
# C. The proposal path -- model output, validated by application code
# ============================================================================


def proposal_of(*tasks: ProposedTask, **kwargs) -> PlanProposal:
    return PlanProposal(goal_summary="Do the thing", tasks=list(tasks), **kwargs)


def proposed(key: str, deps=()) -> ProposedTask:
    return ProposedTask(id=key, title=f"Step {key}", dependencies=list(deps))


async def test_a_valid_proposal_becomes_a_persisted_plan(
    service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite")
    result = await service.attach_proposal(
        created.task_id,
        proposal_of(proposed("a"), proposed("b", ["a"])),
        goal(),
    )

    assert result.outcome is TaskOutcome.UPDATED
    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    # The order is the validator's topological one, not the declaration order.
    assert [s.step_key for s in steps] == ["a", "b"]
    assert [s.sequence for s in steps] == [1, 2]


async def test_a_proposal_is_ordered_topologically_not_as_written(
    service, db_session
) -> None:
    """Model output arrives in whatever order; the application decides."""
    created = await service.create_for_user("Plan the offsite")
    await service.attach_proposal(
        created.task_id,
        # Declared last-first.
        proposal_of(proposed("c", ["b"]), proposed("b", ["a"]), proposed("a")),
        goal(),
    )

    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    assert [s.step_key for s in steps] == ["a", "b", "c"]


@pytest.mark.parametrize("reason, tasks", [
    ("dependency_cycle", (proposed("a", ["b"]), proposed("b", ["a"]))),
    ("unknown_dependency", (proposed("a", ["nowhere"]),)),
    ("self_dependency", (proposed("a", ["a"]),)),
])
async def test_an_unsound_proposal_never_reaches_the_database(
    reason, tasks, service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite")
    result = await service.attach_proposal(created.task_id, proposal_of(*tasks), goal())

    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == reason
    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.plan is None
    assert row.state is TaskState.PROPOSED


async def test_malformed_model_output_does_not_raise(service) -> None:
    created = await service.create_for_user("Plan the offsite")
    for rubbish in (None, "a plan", 42, object()):
        result = await service.attach_proposal(created.task_id, rubbish, goal())
        assert result.outcome is TaskOutcome.REFUSED, rubbish


# ============================================================================
# D. Immutability
# ============================================================================


async def test_a_plan_is_immutable_once_attached(service, db_session) -> None:
    """There is no revision concept, so there is nothing to revise.

    Stage 6A already refused a second plan. This pins that the first survives
    the attempt unchanged rather than being partially overwritten.
    """
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(*VALID))
    before = (await db_session.execute(select(Task))).scalars().one().plan

    again = await service.attach_plan(created.task_id, plan_of(step("z", 1)))
    assert again.outcome is TaskOutcome.REFUSED
    assert again.reason == "plan_already_attached"

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.plan == before
    steps = (await db_session.execute(select(TaskStep))).scalars().all()
    assert sorted(s.step_key for s in steps) == ["a", "b", "c"]


async def test_a_terminal_task_refuses_a_plan(service, db_session) -> None:
    created = await service.create_for_user("Plan the offsite")
    await service.cancel(created.task_id)

    result = await service.attach_plan(created.task_id, plan_of(*VALID))
    assert result.outcome is TaskOutcome.TERMINAL
    assert (await db_session.execute(select(TaskStep))).scalars().all() == []


# ============================================================================
# E. Nothing executes
# ============================================================================


async def test_a_persisted_plan_leaves_execution_untouched(
    service, db_session
) -> None:
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(*VALID))

    row = (await db_session.execute(select(Task))).scalars().one()
    assert row.state is TaskState.PLANNED
    assert row.current_step is None
    assert row.spent == {
        "max_steps": 0, "max_tool_calls": 0, "max_model_calls": 0,
        "max_seconds": 0,
    }
    assert row.completed_at is None
    assert row.failure_count == 0

    steps = (await db_session.execute(select(TaskStep))).scalars().all()
    assert all(s.execution_id is None for s in steps)
    assert all(s.state is TaskStepState.PENDING for s in steps)
    assert all(s.started_at is None and s.completed_at is None for s in steps)


async def test_a_plan_cannot_move_a_task_into_running(service) -> None:
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(created.task_id, plan_of(*VALID))

    # `queued` became reachable in 6C, through `authorize_plan` and only
    # through it. Attaching a plan still reaches neither of these.
    for state in (TaskState.RUNNING, TaskState.COMPLETED):
        result = await service.transition(created.task_id, state)
        assert result.outcome is TaskOutcome.REFUSED
        assert result.reason == "state_not_reachable_in_this_stage"

    assert (await service.get(created.task_id)).state is TaskState.PLANNED


async def test_a_plan_survives_a_restart(service, db_session) -> None:
    created = await service.create_for_user("Persisted plan")
    await service.attach_plan(created.task_id, plan_of(*VALID, assumptions=["kept"]))
    await db_session.commit()

    fresh = TaskService(db_session, settings=service._settings)
    task = await fresh.get_detail(created.task_id)
    assert task.plan["assumptions"] == ["kept"]
    assert [s.step_key for s in sorted(task.steps, key=lambda s: s.sequence)] == [
        "a", "b", "c"
    ]
    assert all(s.execution_id is None for s in task.steps)


# ============================================================================
# F. Plan preview, over the real API
# ============================================================================


async def seed_planned_task(db_session, settings, objective="Plan the offsite"):
    service = TaskService(db_session, settings=settings)
    created = await service.create_for_user(objective)
    await service.attach_plan(
        created.task_id,
        Plan(
            goal=goal("Run a good offsite"),
            tasks=[
                PlanTask(id="a", title="Pick dates", description="Check calendars",
                         order=1, depth=0, expected_outcome="A date",
                         completion_criteria=["everyone can attend"]),
                PlanTask(id="b", title="Book a venue", dependencies=["a"],
                         order=2, depth=1),
            ],
            assumptions=["by 'offsite' I mean the team offsite"],
            risks=["the venue may be booked"],
            success_criteria=["everyone attends"],
        ),
    )
    await db_session.commit()
    return created.task_id


async def test_the_preview_shows_what_mai_intends_to_do(
    client, db_session, settings
) -> None:
    task_id = await seed_planned_task(db_session, settings)
    body = (await client.get(f"/api/tasks/{task_id}/plan")).json()

    assert body["objective"] == "Plan the offsite"
    assert body["task_state"] == "planned"
    assert body["goal_summary"] == "Run a good offsite"
    assert body["plan_id"] is not None
    assert body["assumptions"] == ["by 'offsite' I mean the team offsite"]
    assert body["risks"] == ["the venue may be booked"]
    assert body["success_criteria"] == ["everyone attends"]
    assert body["budget"]["max_steps"] == 20

    assert [s["step_key"] for s in body["steps"]] == ["a", "b"]
    assert [s["sequence"] for s in body["steps"]] == [1, 2]
    assert body["steps"][0]["description"] == "Check calendars"
    assert body["steps"][0]["completion_criteria"] == ["everyone can attend"]
    assert body["steps"][1]["depends_on"] == ["a"]


async def test_the_preview_says_plainly_that_nothing_has_run(
    client, db_session, settings
) -> None:
    task_id = await seed_planned_task(db_session, settings)
    body = (await client.get(f"/api/tasks/{task_id}/plan")).json()

    assert body["current_step"] is None
    assert body["step_count"] == 2
    assert body["executed_step_count"] == 0
    assert all(s["state"] == "pending" for s in body["steps"])
    assert all(s["execution_id"] is None for s in body["steps"])
    assert body["spent"] == {
        "max_steps": 0, "max_tool_calls": 0, "max_model_calls": 0,
        "max_seconds": 0,
    }


async def test_a_task_with_no_plan_has_no_preview(
    client, db_session, settings
) -> None:
    service = TaskService(db_session, settings=settings)
    created = await service.create_for_user("No plan yet")
    await db_session.commit()

    response = await client.get(f"/api/tasks/{created.task_id}/plan")
    assert response.status_code == 404
    # The application's error envelope, not FastAPI's bare `detail`.
    assert response.json()["error"]["message"] == "no_plan_attached"


async def test_an_unknown_task_has_no_preview(client) -> None:
    response = await client.get(f"/api/tasks/{uuid.uuid4()}/plan")
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "task_not_found"


async def test_the_preview_exposes_no_sensitive_field(
    client, db_session, settings
) -> None:
    """A preview is for a person deciding whether to approve."""
    task_id = await seed_planned_task(db_session, settings)
    body = (await client.get(f"/api/tasks/{task_id}/plan")).json()

    assert sorted(body) == [
        "assumptions", "authorized_at", "budget", "current_step",
        "executed_step_count", "goal_summary", "objective", "plan_id",
        "risks", "spent", "step_count", "steps", "success_criteria",
        "task_id", "task_state",
    ]
    blob = str(body).lower()
    for leak in ("token", "secret", "credential", "authorization", "api_key",
                 "password", "endpoint", "owner_id"):
        assert leak not in blob, leak


async def test_another_owner_gets_no_preview(
    client, db_session, settings
) -> None:
    other = TaskService(
        db_session, settings=settings,
        owner_id=uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb"),
    )
    created = await other.create_for_user("Someone else's task")
    await other.attach_plan(created.task_id, plan_of(*VALID))
    await db_session.commit()

    assert (await client.get(f"/api/tasks/{created.task_id}/plan")).status_code == 404


# ============================================================================
# G. Gaps found by mutation testing
# ============================================================================


async def test_a_step_may_not_exceed_the_dependency_bound(
    service, db_session
) -> None:
    """B7: no test previously gave one step more than five dependencies."""
    created = await service.create_for_user("Plan the offsite")
    # Six dependencies on one step; the bound is five.
    roots = [step(f"r{n}", n) for n in range(1, 7)]
    dependent = step("x", 7, [f"r{n}" for n in range(1, 7)])

    result = await service.attach_plan(created.task_id, plan_of(*roots, dependent))
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason == "too_many_dependencies"
    assert (await db_session.execute(select(TaskStep))).scalars().all() == []


def test_the_dependency_bounds_are_what_they_say() -> None:
    """Literal-pinned, so widening either has to be argued for."""
    from app.planning import limits

    assert limits.MAX_DEPENDENCIES_PER_TASK == 5
    assert limits.MAX_TOTAL_DEPENDENCIES == 60
    assert limits.MAX_TASKS == 20


async def test_a_plan_may_not_exceed_the_total_edge_bound(
    service, db_session
) -> None:
    """B8: the per-step bound can hold while the whole graph is too dense.

    Sixteen steps depending on five earlier ones each is 61 edges, one past
    the total bound, with no step over the per-step bound.
    """
    created = await service.create_for_user("Plan the offsite")
    roots = [step(f"r{n}", n) for n in range(1, 5)]
    built = list(roots)
    sequence = 5
    edges = 0
    while edges <= 60:
        deps = [f"r{n}" for n in range(1, 5)] + [f"d{sequence - 1}"] \
            if sequence > 5 else [f"r{n}" for n in range(1, 5)]
        deps = deps[:5]
        built.append(step(f"d{sequence}", sequence, deps))
        edges += len(deps)
        sequence += 1

    result = await service.attach_plan(created.task_id, plan_of(*built))
    assert result.outcome is TaskOutcome.REFUSED
    assert result.reason in {"too_many_total_dependencies", "too_many_tasks"}
    assert (await db_session.execute(select(TaskStep))).scalars().all() == []


async def test_the_sequence_comes_from_the_plan_not_the_list_position(
    service, db_session
) -> None:
    """B15: every earlier test had declaration order equal to plan order.

    A plan whose `tasks` list is written out of order still materialises in
    the order the validator computed, because the sequence is read from each
    step's own `order` rather than from its position in the list.
    """
    created = await service.create_for_user("Plan the offsite")
    # Declared c, a, b -- but ordered a(1), b(2), c(3).
    out_of_order = plan_of(
        step("c", 3, ["b"]), step("a", 1), step("b", 2, ["a"]),
    )
    assert (await service.attach_plan(created.task_id, out_of_order)).ok

    steps = (await db_session.execute(
        select(TaskStep).order_by(TaskStep.sequence)
    )).scalars().all()
    assert [(s.step_key, s.sequence) for s in steps] == [
        ("a", 1), ("b", 2), ("c", 3)
    ]


async def test_steps_are_loaded_in_sequence_order(service, db_session) -> None:
    """B36: the preview's sort is redundant only while this holds.

    `Task.steps` declares `order_by="TaskStep.sequence"`, which is why
    removing the explicit sort in the preview changed nothing. Pinning the
    relationship keeps that true.
    """
    created = await service.create_for_user("Plan the offsite")
    await service.attach_plan(
        created.task_id, plan_of(step("c", 3, ["b"]), step("a", 1), step("b", 2, ["a"]))
    )
    await db_session.commit()

    task = await service.get_detail(created.task_id)
    assert [s.step_key for s in task.steps] == ["a", "b", "c"]
