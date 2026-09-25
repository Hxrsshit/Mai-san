"""Layer 2: graph validation.

Schema validation (layer 1, in `schemas.py`) proves the *shape* is right: the
fields exist, the types match, the enums are closed, the sizes are bounded.
It cannot prove the graph is sound, because soundness is a property of the
tasks together rather than of any one of them.

That is what this module does, and why it is a separate layer with its own
tests: valid JSON is not automatically a valid plan.

    dependencies exist  →  no self-dependency  →  edge budget
                        →  no cycles  →  deterministic topological order

Pure functions throughout. No database, no model, no clock, no I/O. Same
proposal in, same plan out, every time.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.planning import limits
from app.planning.schemas import (
    Goal,
    Plan,
    PlanProposal,
    PlanTask,
    ProposedTask,
)


class PlanValidationError(Exception):
    """A proposal that cannot become a plan.

    `reason` is one of a fixed set of application constants, so nothing from
    the model or the user reaches a caller through it. `detail` is developer
    context and is logged, never returned to a client.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class GraphReport:
    """What validation found. Useful for debugging a rejection."""

    task_count: int = 0
    dependency_count: int = 0
    order: List[str] = field(default_factory=list)
    depths: Dict[str, int] = field(default_factory=dict)
    #: Duplicate dependency entries removed during normalisation.
    duplicates_normalised: int = 0
    #: Tasks with no prerequisites -- where work could begin.
    roots: List[str] = field(default_factory=list)
    max_depth: int = 0


def validate_graph(tasks: Sequence[ProposedTask]) -> GraphReport:
    """Check the dependency graph. Raises `PlanValidationError` on any fault.

    Order of checks is deliberate: cheap and specific first, so a rejection
    names the actual problem rather than a downstream symptom. A missing
    dependency reported as "cycle detected" would be actively misleading.
    """
    if not tasks:
        raise PlanValidationError("empty_plan")

    if len(tasks) > limits.MAX_TASKS:
        raise PlanValidationError(
            "too_many_tasks", f"{len(tasks)} > {limits.MAX_TASKS}"
        )

    ids = [task.id for task in tasks]
    unique = set(ids)
    if len(unique) != len(ids):
        # The schema also catches this; kept here so the graph layer is
        # correct on its own and can be tested without the schema.
        raise PlanValidationError("duplicate_task_id")

    total_edges = 0
    for task in tasks:
        if len(task.dependencies) > limits.MAX_DEPENDENCIES_PER_TASK:
            raise PlanValidationError(
                "too_many_dependencies",
                f"{task.id} has {len(task.dependencies)}",
            )
        for dependency in task.dependencies:
            if dependency == task.id:
                raise PlanValidationError("self_dependency", task.id)
            if dependency not in unique:
                raise PlanValidationError(
                    "unknown_dependency", f"{task.id} -> {dependency}"
                )
        total_edges += len(task.dependencies)

    if total_edges > limits.MAX_TOTAL_DEPENDENCIES:
        raise PlanValidationError(
            "too_many_total_dependencies",
            f"{total_edges} > {limits.MAX_TOTAL_DEPENDENCIES}",
        )

    order, depths = _topological_order(tasks)

    return GraphReport(
        task_count=len(tasks),
        dependency_count=total_edges,
        order=order,
        depths=depths,
        roots=[task.id for task in tasks if not task.dependencies],
        max_depth=max(depths.values()) if depths else 0,
    )


def _topological_order(
    tasks: Sequence[ProposedTask],
) -> Tuple[List[str], Dict[str, int]]:
    """Kahn's algorithm, with declaration order as the tie-break.

    Determinism matters more than it looks. Two runs over the same plan must
    produce the same order, or a plan is not something a later stage can
    reason about, diff or show twice. A `set`-based frontier would be correct
    and non-deterministic; the position map below makes ties resolve the same
    way every time.

    Declaration order is the tie-break rather than, say, priority: when the
    graph does not constrain two tasks, the order the model wrote them in is
    the only signal about sequence that exists, and discarding it would
    scramble a plan the model laid out sensibly.
    """
    position = {task.id: index for index, task in enumerate(tasks)}
    dependencies = {task.id: list(task.dependencies) for task in tasks}

    dependents: Dict[str, List[str]] = {task.id: [] for task in tasks}
    remaining: Dict[str, int] = {}
    for task in tasks:
        remaining[task.id] = len(dependencies[task.id])
        for dependency in dependencies[task.id]:
            dependents[dependency].append(task.id)

    frontier = sorted(
        (task_id for task_id, count in remaining.items() if count == 0),
        key=lambda task_id: position[task_id],
    )

    order: List[str] = []
    depths: Dict[str, int] = {task_id: 0 for task_id in frontier}

    while frontier:
        current = frontier.pop(0)
        order.append(current)

        newly_ready: List[str] = []
        for dependent in dependents[current]:
            depths[dependent] = max(depths.get(dependent, 0), depths[current] + 1)
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                newly_ready.append(dependent)

        if newly_ready:
            frontier.extend(newly_ready)
            frontier.sort(key=lambda task_id: position[task_id])

    if len(order) != len(tasks):
        # Whatever could not be ordered is part of a cycle, or downstream of
        # one. Naming a member makes the rejection actionable.
        stuck = sorted(
            (task_id for task_id in remaining if task_id not in set(order)),
            key=lambda task_id: position[task_id],
        )
        raise PlanValidationError("dependency_cycle", ", ".join(stuck[:5]))

    return order, depths


def build_plan(proposal: PlanProposal, goal: Goal) -> Plan:
    """Turn a validated proposal into a `Plan`, ordered and annotated.

    Runs graph validation first: a `Plan` object exists only for a proposal
    that passed both layers, so holding one is itself the proof.
    """
    report = validate_graph(proposal.tasks)

    by_id = {task.id: task for task in proposal.tasks}
    ordered: List[PlanTask] = []
    for position, task_id in enumerate(report.order, start=1):
        task = by_id[task_id]
        ordered.append(
            PlanTask(
                id=task.id,
                title=task.title,
                description=task.description,
                priority=task.priority,
                dependencies=list(task.dependencies),
                expected_outcome=task.expected_outcome,
                completion_criteria=list(task.completion_criteria),
                # Stage 6C. Carried through unchanged: the validator's job is
                # the graph, and a capability name means nothing until
                # `app.tasks.capabilities` looks it up.
                capability=task.capability,
                arguments=dict(task.arguments),
                order=position,
                depth=report.depths.get(task_id, 0),
            )
        )

    return Plan(
        goal=goal,
        tasks=ordered,
        assumptions=list(proposal.assumptions),
        risks=list(proposal.risks),
        success_criteria=list(proposal.success_criteria),
    )


__all__ = [
    "GraphReport",
    "PlanValidationError",
    "build_plan",
    "validate_graph",
]
