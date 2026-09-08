"""Workflow state, and the only transitions that exist.

The same shape as `app.execution.states`, and for the same reason: a table of
declared edges is auditable in a way scattered `if` statements are not. Every
transition this system permits is visible in one dictionary, and anything
absent from it is refused.

A workflow's state is **not** derived from its steps. It is stored, and moved
only by `WorkflowService`. Derived state would mean a workflow's status could
change because a step record changed, and a step record is exactly the thing
an attacker with a database foothold would reach for.
"""

import enum
from typing import Dict, FrozenSet


class WorkflowState(str, enum.Enum):
    """Where a workflow is. Explicit states, never a pair of booleans."""

    #: Recognised and planned. Nothing has been shown to the user yet.
    PENDING = "pending"
    #: The plan has been put to the user. Nothing has run.
    AWAITING_APPROVAL = "awaiting_approval"
    #: The user approved this exact plan.
    APPROVED = "approved"
    #: A step is executing.
    RUNNING = "running"
    #: Every step completed.
    SUCCEEDED = "succeeded"
    #: A step failed. Earlier successful steps keep their own state.
    FAILED = "failed"
    #: The user declined, or the turn moved on.
    CANCELLED = "cancelled"
    #: The approval window closed before the workflow ran.
    EXPIRED = "expired"


#: Every legal move. Anything not listed here is refused.
#:
#: Note what is absent: PENDING -> RUNNING (a workflow cannot run without
#: being approved), APPROVED -> SUCCEEDED (it cannot finish without running),
#: and every edge out of a terminal state.
ALLOWED_TRANSITIONS: Dict[WorkflowState, FrozenSet[WorkflowState]] = {
    WorkflowState.PENDING: frozenset({
        WorkflowState.AWAITING_APPROVAL,
        WorkflowState.CANCELLED,
    }),
    WorkflowState.AWAITING_APPROVAL: frozenset({
        WorkflowState.APPROVED,
        WorkflowState.CANCELLED,
        WorkflowState.EXPIRED,
    }),
    WorkflowState.APPROVED: frozenset({
        WorkflowState.RUNNING,
        WorkflowState.CANCELLED,
        WorkflowState.EXPIRED,
    }),
    WorkflowState.RUNNING: frozenset({
        WorkflowState.SUCCEEDED,
        WorkflowState.FAILED,
    }),
    # Terminal.
    WorkflowState.SUCCEEDED: frozenset(),
    WorkflowState.FAILED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
    WorkflowState.EXPIRED: frozenset(),
}

TERMINAL_STATES: FrozenSet[WorkflowState] = frozenset(
    state for state, onward in ALLOWED_TRANSITIONS.items() if not onward
)

#: The only state a workflow may begin executing from.
RUNNABLE_FROM: WorkflowState = WorkflowState.APPROVED


def can_transition(current: WorkflowState, target: WorkflowState) -> bool:
    """True when `current -> target` is a declared edge."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal(state: WorkflowState) -> bool:
    return state in TERMINAL_STATES


__all__ = [
    "ALLOWED_TRANSITIONS",
    "RUNNABLE_FROM",
    "TERMINAL_STATES",
    "WorkflowState",
    "can_transition",
    "is_terminal",
]
