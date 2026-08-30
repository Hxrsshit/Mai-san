"""Stage 4B: schema validation and graph validation, as two distinct layers.

Layer 1 proves the shape is right. Layer 2 proves the graph is sound. They are
tested separately because they fail differently: valid JSON is not a valid
plan, and a plan whose dependencies form a cycle has a perfect shape.
"""

import pytest
from pydantic import ValidationError

from app.intent.schemas import IntentType
from app.planning import limits
from app.planning.schemas import Goal, PlanProposal, Priority, ProposedTask
from app.planning.validator import (
    PlanValidationError,
    build_plan,
    validate_graph,
)


def task(task_id: str, **overrides) -> dict:
    payload = {"id": task_id, "title": f"Task {task_id}", "dependencies": []}
    payload.update(overrides)
    return payload


def proposal(*tasks, **overrides) -> PlanProposal:
    payload = {"goal_summary": "Launch a SaaS product", "tasks": list(tasks)}
    payload.update(overrides)
    return PlanProposal(**payload)


def goal() -> Goal:
    return Goal(summary="Launch a SaaS product", source_intent=IntentType.PLANNING)


#: The specification's worked example.
SAAS_TASKS = [
    task("research-market"),
    task("analyze-competitors", dependencies=["research-market"]),
    task("define-positioning", dependencies=["research-market", "analyze-competitors"]),
    task("launch-strategy", dependencies=["define-positioning"]),
]


# --- Layer 1: schema --------------------------------------------------------


def test_a_well_formed_proposal_validates() -> None:
    result = proposal(*SAAS_TASKS)
    assert len(result.tasks) == 4
    assert result.tasks[0].priority is Priority.MEDIUM


@pytest.mark.parametrize("priority", ["low", "medium", "high"])
def test_valid_priorities_are_accepted(priority) -> None:
    result = proposal(task("a", priority=priority))
    assert result.tasks[0].priority.value == priority


@pytest.mark.parametrize(
    "priority", ["urgent", "P0", "critical", "LOW ", "", None, 1, "highest"]
)
def test_an_invalid_priority_is_rejected(priority) -> None:
    with pytest.raises(ValidationError):
        proposal(task("a", priority=priority))


@pytest.mark.parametrize(
    "task_id",
    [
        "Has Spaces", "UPPER!", "sym#bol", "-" * 5 + "!", "",
        "a" * (limits.MAX_TASK_ID_LENGTH + 1),
        "../../etc/passwd", "<script>", "id;drop", "id\nnewline",
    ],
)
def test_an_invalid_task_id_is_rejected(task_id) -> None:
    with pytest.raises(ValidationError):
        proposal(task(task_id))


@pytest.mark.parametrize("task_id", ["a", "step-one", "step_1", "a1b2c3"])
def test_a_valid_slug_id_is_accepted(task_id) -> None:
    assert proposal(task(task_id)).tasks[0].id == task_id


def test_ids_are_lowercased() -> None:
    assert proposal(task("Step-One"))  .tasks[0].id == "step-one"


def test_duplicate_task_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        proposal(task("a"), task("a", title="Different"))


def test_an_empty_task_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        proposal()


def test_invented_fields_are_dropped() -> None:
    """A model must not be able to widen the shape it answers in."""
    result = PlanProposal(
        goal_summary="A goal",
        tasks=[
            {
                "id": "a",
                "title": "Do it",
                "approved": True,
                "execute": True,
                "tool": "shell",
                "auto_run": True,
            }
        ],
        approved=True,
        execution_policy="autonomous",
    )
    assert not hasattr(result, "approved")
    assert not hasattr(result.tasks[0], "execute")
    assert not hasattr(result.tasks[0], "tool")
    assert "tool" not in result.tasks[0].model_dump()


def test_text_fields_are_whitespace_normalised() -> None:
    result = proposal(task("a", title="  Research   the\n\nmarket  "))
    assert result.tasks[0].title == "Research the market"


# --- Layer 1: size limits ---------------------------------------------------


def test_too_many_tasks_are_rejected_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        proposal(*[task(f"t{index}") for index in range(limits.MAX_TASKS + 1)])


def test_the_maximum_task_count_is_accepted() -> None:
    result = proposal(*[task(f"t{index}") for index in range(limits.MAX_TASKS)])
    assert len(result.tasks) == limits.MAX_TASKS


def test_too_many_dependencies_on_one_task_are_rejected() -> None:
    others = [task(f"t{index}") for index in range(limits.MAX_DEPENDENCIES_PER_TASK + 1)]
    with pytest.raises(ValidationError):
        proposal(
            *others,
            task("last", dependencies=[item["id"] for item in others]),
        )


@pytest.mark.parametrize(
    "field,limit",
    [
        ("title", limits.MAX_TASK_TITLE_CHARS),
        ("description", limits.MAX_TASK_DESCRIPTION_CHARS),
        ("expected_outcome", limits.MAX_EXPECTED_OUTCOME_CHARS),
    ],
)
def test_oversized_task_fields_are_rejected(field, limit) -> None:
    with pytest.raises(ValidationError):
        proposal(task("a", **{field: "A" * (limit + 1)}))


@pytest.mark.parametrize(
    "field,limit",
    [
        ("assumptions", limits.MAX_ASSUMPTIONS),
        ("risks", limits.MAX_RISKS),
        ("success_criteria", limits.MAX_SUCCESS_CRITERIA),
    ],
)
def test_oversized_plan_lists_are_rejected(field, limit) -> None:
    with pytest.raises(ValidationError):
        proposal(task("a"), **{field: [f"item {n}" for n in range(limit + 1)]})


def test_too_many_completion_criteria_are_rejected() -> None:
    with pytest.raises(ValidationError):
        proposal(
            task(
                "a",
                completion_criteria=[
                    f"c{n}" for n in range(limits.MAX_COMPLETION_CRITERIA + 1)
                ],
            )
        )


# --- Layer 2: dependency existence ------------------------------------------


def test_a_sound_graph_validates() -> None:
    report = validate_graph(proposal(*SAAS_TASKS).tasks)
    assert report.task_count == 4
    assert report.dependency_count == 4
    assert report.roots == ["research-market"]


def test_a_dependency_on_a_missing_task_is_rejected() -> None:
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(proposal(task("a", dependencies=["ghost"])).tasks)
    assert caught.value.reason == "unknown_dependency"


def test_a_self_dependency_is_rejected() -> None:
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(proposal(task("a", dependencies=["a"])).tasks)
    assert caught.value.reason == "self_dependency"


def test_a_self_dependency_is_rejected_even_among_valid_tasks() -> None:
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(
            proposal(task("a"), task("b", dependencies=["a", "b"])).tasks
        )
    assert caught.value.reason == "self_dependency"


def test_duplicate_dependencies_are_normalised_not_rejected() -> None:
    """A model listing a prerequisite twice describes the graph correctly.

    Rejecting the whole plan for that would trade a real plan for a formatting
    complaint. A dependency that does not *exist* is a different matter.
    """
    result = proposal(task("a"), task("b", dependencies=["a", "a", "a"]))
    assert result.tasks[1].dependencies == ["a"]
    assert validate_graph(result.tasks).dependency_count == 1


def test_dependency_case_is_normalised() -> None:
    result = proposal(task("a"), task("b", dependencies=["A"]))
    assert result.tasks[1].dependencies == ["a"]
    validate_graph(result.tasks)


# --- Layer 2: cycles --------------------------------------------------------


def test_a_two_task_cycle_is_rejected() -> None:
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(
            proposal(
                task("a", dependencies=["b"]), task("b", dependencies=["a"])
            ).tasks
        )
    assert caught.value.reason == "dependency_cycle"


def test_a_three_task_cycle_is_rejected() -> None:
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(
            proposal(
                task("a", dependencies=["c"]),
                task("b", dependencies=["a"]),
                task("c", dependencies=["b"]),
            ).tasks
        )
    assert caught.value.reason == "dependency_cycle"


def test_a_cycle_hidden_behind_valid_tasks_is_rejected() -> None:
    """A sound prefix must not mask an unsound remainder."""
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(
            proposal(
                task("start"),
                task("a", dependencies=["start", "c"]),
                task("b", dependencies=["a"]),
                task("c", dependencies=["b"]),
            ).tasks
        )
    assert caught.value.reason == "dependency_cycle"


def test_a_long_chain_is_not_mistaken_for_a_cycle() -> None:
    chain = [task("t0")]
    for index in range(1, limits.MAX_TASKS):
        chain.append(task(f"t{index}", dependencies=[f"t{index - 1}"]))

    report = validate_graph(proposal(*chain).tasks)

    assert report.order == [f"t{index}" for index in range(limits.MAX_TASKS)]
    assert report.max_depth == limits.MAX_TASKS - 1


def test_a_diamond_is_valid() -> None:
    report = validate_graph(
        proposal(
            task("a"),
            task("b", dependencies=["a"]),
            task("c", dependencies=["a"]),
            task("d", dependencies=["b", "c"]),
        ).tasks
    )
    assert report.order == ["a", "b", "c", "d"]
    assert report.depths["d"] == 2


# --- Layer 2: resource limits -----------------------------------------------


def test_the_graph_layer_rejects_too_many_tasks_on_its_own() -> None:
    """The layer must be correct independently of the schema above it."""
    tasks = [ProposedTask(id=f"t{n}", title="x") for n in range(limits.MAX_TASKS + 1)]
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(tasks)
    assert caught.value.reason == "too_many_tasks"


def test_the_graph_layer_rejects_duplicates_on_its_own() -> None:
    tasks = [ProposedTask(id="a", title="x"), ProposedTask(id="a", title="y")]
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(tasks)
    assert caught.value.reason == "duplicate_task_id"


def test_an_empty_task_list_is_rejected_by_the_graph_layer() -> None:
    with pytest.raises(PlanValidationError) as caught:
        validate_graph([])
    assert caught.value.reason == "empty_plan"


def test_the_total_edge_budget_is_enforced() -> None:
    """Dense graphs are rejected even when every task is within its own limit."""
    tasks = [ProposedTask(id=f"t{n}", title="x") for n in range(limits.MAX_TASKS)]
    edges = 0
    for index in range(1, limits.MAX_TASKS):
        wanted = min(limits.MAX_DEPENDENCIES_PER_TASK, index)
        tasks[index] = ProposedTask(
            id=f"t{index}",
            title="x",
            dependencies=[f"t{n}" for n in range(index - wanted, index)],
        )
        edges += wanted

    assert edges > limits.MAX_TOTAL_DEPENDENCIES, "the fixture is not dense enough"
    with pytest.raises(PlanValidationError) as caught:
        validate_graph(tasks)
    assert caught.value.reason == "too_many_total_dependencies"


# --- Ordering ---------------------------------------------------------------


def test_the_order_respects_every_dependency() -> None:
    report = validate_graph(proposal(*SAAS_TASKS).tasks)
    position = {task_id: index for index, task_id in enumerate(report.order)}

    for entry in proposal(*SAAS_TASKS).tasks:
        for dependency in entry.dependencies:
            assert position[dependency] < position[entry.id]


def test_ordering_is_deterministic_across_runs() -> None:
    """Two runs over the same plan must produce the same order."""
    orders = {tuple(validate_graph(proposal(*SAAS_TASKS).tasks).order) for _ in range(20)}
    assert len(orders) == 1


def test_independent_tasks_keep_their_declared_order() -> None:
    """When the graph does not constrain two tasks, the model's order stands.

    It is the only signal about sequence that exists, and discarding it would
    scramble a plan the model laid out sensibly.
    """
    report = validate_graph(
        proposal(task("zebra"), task("alpha"), task("middle")).tasks
    )
    assert report.order == ["zebra", "alpha", "middle"]


def test_depth_is_the_longest_path_not_the_shortest() -> None:
    report = validate_graph(
        proposal(
            task("a"),
            task("b", dependencies=["a"]),
            task("c", dependencies=["a", "b"]),
        ).tasks
    )
    assert report.depths == {"a": 0, "b": 1, "c": 2}


# --- Building a plan --------------------------------------------------------


def test_a_built_plan_is_ordered_and_annotated() -> None:
    plan = build_plan(proposal(*SAAS_TASKS), goal())

    assert [entry.id for entry in plan.tasks] == [
        "research-market", "analyze-competitors", "define-positioning",
        "launch-strategy",
    ]
    assert [entry.order for entry in plan.tasks] == [1, 2, 3, 4]
    assert [entry.depth for entry in plan.tasks] == [0, 1, 2, 3]
    assert plan.task_count == 4
    assert plan.dependency_count == 4


def test_building_an_unsound_plan_raises_rather_than_returning_one() -> None:
    """Holding a `Plan` is itself proof that both layers passed."""
    with pytest.raises(PlanValidationError):
        build_plan(
            proposal(task("a", dependencies=["b"]), task("b", dependencies=["a"])),
            goal(),
        )


def test_a_plan_is_immutable() -> None:
    plan = build_plan(proposal(*SAAS_TASKS), goal())
    with pytest.raises(ValidationError):
        plan.tasks = []


def test_assumptions_risks_and_criteria_are_carried_through_verbatim() -> None:
    plan = build_plan(
        proposal(
            task("a"),
            assumptions=["The user wants to target small businesses."],
            risks=["Competitor information may be incomplete."],
            success_criteria=["Target market identified."],
        ),
        goal(),
    )
    assert plan.assumptions == ["The user wants to target small businesses."]
    assert plan.risks == ["Competitor information may be incomplete."]
    assert plan.success_criteria == ["Target market identified."]


def test_the_plan_records_the_intent_that_justified_it() -> None:
    plan = build_plan(proposal(task("a")), goal())
    assert plan.goal.source_intent is IntentType.PLANNING
