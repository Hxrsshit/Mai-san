"""Task and step lifecycle, and the only transitions that exist.

The same shape as `app.execution.states` and `app.workflows.states`, and for
the same reason: a state machine written as a table can be read, tested and
argued with, where one written as scattered `if` statements can only be
traced.

### Why the whole vocabulary is declared in Stage 6A

Most of these states are unreachable in this stage -- nothing executes, so
nothing runs, completes or exhausts a budget. They are declared anyway
because the set is a **PostgreSQL enum type**: adding a member later is a
migration against a live column, while declaring the vocabulary once and
leaving parts of it unreachable costs nothing.

What stops an unreachable state being reached by accident is not its absence
from this table but `app.tasks.service`, which offers no operation that
performs an execution transition -- and a test that pins exactly which
transitions Stage 6A can perform.
"""

import enum
from typing import Dict, FrozenSet


class TaskState(str, enum.Enum):
    """Where a task is in its life."""

    #: Created from a user turn. Nothing has been planned.
    PROPOSED = "proposed"
    #: A validated plan is attached.
    PLANNED = "planned"
    #: Waiting for a person to approve the plan or a step in it.
    AWAITING_APPROVAL = "awaiting_approval"
    #: Approved and waiting for a runner. No runner exists in Stage 6A.
    QUEUED = "queued"
    RUNNING = "running"
    #: Stopped by the user, resumable.
    PAUSED = "paused"
    #: Stopped by circumstance -- a missing connector, an unanswered
    #: question. Distinct from `paused`, which is a choice, and from
    #: `failed`, which is over.
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStepState(str, enum.Enum):
    """Where one step of a plan is."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Deliberately not run -- a dependency failed, or a replan dropped it.
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


#: Every legal task move. Anything not listed here is refused.
#:
#: Note what is absent. There is no edge from `proposed` to `running`: a task
#: cannot run before it is planned. There is no edge into `completed` from
#: anywhere but `running`, so a task cannot be declared done without having
#: run -- which is the structural reason Stage 6A cannot fabricate a
#: completed task. And every terminal state has no outgoing edge at all.
ALLOWED_TRANSITIONS: Dict[TaskState, FrozenSet[TaskState]] = {
    TaskState.PROPOSED: frozenset({
        TaskState.PLANNED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    }),
    TaskState.PLANNED: frozenset({
        TaskState.AWAITING_APPROVAL,
        TaskState.QUEUED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    }),
    TaskState.AWAITING_APPROVAL: frozenset({
        TaskState.QUEUED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    }),
    TaskState.QUEUED: frozenset({
        TaskState.RUNNING,
        TaskState.PAUSED,
        TaskState.BLOCKED,
        TaskState.CANCELLED,
    }),
    TaskState.RUNNING: frozenset({
        TaskState.AWAITING_APPROVAL,
        TaskState.PAUSED,
        TaskState.BLOCKED,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }),
    TaskState.PAUSED: frozenset({
        TaskState.QUEUED,
        TaskState.CANCELLED,
    }),
    TaskState.BLOCKED: frozenset({
        TaskState.QUEUED,
        TaskState.AWAITING_APPROVAL,
        TaskState.CANCELLED,
        TaskState.FAILED,
    }),
    # Terminal.
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

#: Every legal step move.
ALLOWED_STEP_TRANSITIONS: Dict[TaskStepState, FrozenSet[TaskStepState]] = {
    TaskStepState.PENDING: frozenset({
        TaskStepState.RUNNING,
        TaskStepState.SKIPPED,
        TaskStepState.CANCELLED,
    }),
    TaskStepState.RUNNING: frozenset({
        TaskStepState.COMPLETED,
        TaskStepState.FAILED,
        TaskStepState.CANCELLED,
    }),
    # Terminal.
    TaskStepState.COMPLETED: frozenset(),
    TaskStepState.FAILED: frozenset(),
    TaskStepState.SKIPPED: frozenset(),
    TaskStepState.CANCELLED: frozenset(),
}

TERMINAL_STATES: FrozenSet[TaskState] = frozenset(
    state for state, onward in ALLOWED_TRANSITIONS.items() if not onward
)

TERMINAL_STEP_STATES: FrozenSet[TaskStepState] = frozenset(
    state for state, onward in ALLOWED_STEP_TRANSITIONS.items() if not onward
)

#: The states that mean "a person or a condition is being waited on".
#:
#: Named because `app.tasks.service` answers "what are you waiting for?" from
#: it, and a query written as a literal list in a route would drift from the
#: state machine the first time a state was added.
WAITING_STATES: FrozenSet[TaskState] = frozenset({
    TaskState.AWAITING_APPROVAL,
    TaskState.BLOCKED,
    TaskState.PAUSED,
})

#: The states Stage 6A's service is permitted to move a task into.
#:
#: This is the no-execution boundary, written down. Every other transition in
#: the table above is declared for later stages and unreachable today; a test
#: pins this set against what the service can actually do, so a stage that
#: starts executing has to change this line deliberately.
STAGE_6A_REACHABLE: FrozenSet[TaskState] = frozenset({
    TaskState.PROPOSED,
    TaskState.PLANNED,
    TaskState.BLOCKED,
    TaskState.CANCELLED,
    TaskState.FAILED,
})


def can_transition(current: TaskState, target: TaskState) -> bool:
    """True when `current -> target` is a declared edge."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def can_step_transition(current: TaskStepState, target: TaskStepState) -> bool:
    return target in ALLOWED_STEP_TRANSITIONS.get(current, frozenset())


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


def is_step_terminal(state: TaskStepState) -> bool:
    return state in TERMINAL_STEP_STATES


__all__ = [
    "ALLOWED_STEP_TRANSITIONS",
    "ALLOWED_TRANSITIONS",
    "STAGE_6A_REACHABLE",
    "TERMINAL_STATES",
    "TERMINAL_STEP_STATES",
    "WAITING_STATES",
    "TaskState",
    "TaskStepState",
    "can_step_transition",
    "can_transition",
    "is_step_terminal",
    "is_terminal",
]
