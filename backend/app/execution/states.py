"""The execution lifecycle, as an explicit state machine.

Every transition is declared. Anything undeclared is refused, so an execution
cannot reach a running state by an unanticipated route -- including the one
that matters most: `PROPOSED` has no edge to `EXECUTING`, so there is no path
from "the model suggested this" to "it ran" that does not pass through a human.

Terminal states have no outgoing edges at all. A finished execution is
finished; re-running it means creating a new record, which gets its own
approval.
"""

import enum
from typing import Dict, FrozenSet


class ExecutionState(str, enum.Enum):
    """Where an execution is in its life."""

    #: Created from an authorized action proposal. Nothing has happened.
    PROPOSED = "proposed"
    #: A human approved this exact payload. Still nothing has happened.
    APPROVED = "approved"
    #: Approval withdrawn before execution started.
    REVOKED = "revoked"
    #: Approval aged out before execution started.
    EXPIRED = "expired"
    #: Deterministic policy refused it. Never reachable by approval.
    REJECTED = "rejected"
    #: Abandoned deliberately before completion.
    CANCELLED = "cancelled"
    #: Claimed by exactly one attempt. The tool is running.
    EXECUTING = "executing"
    #: The tool ran and reported success.
    SUCCEEDED = "succeeded"
    #: The tool ran and failed.
    FAILED = "failed"


#: Declared transitions. The whole machine, in one readable place.
#:
#: Two absences are the load-bearing part:
#:
#: - `PROPOSED` cannot reach `EXECUTING`. Approval is not a formality that can
#:   be skipped when convenient; it is the only edge into the running state.
#: - Terminal states have no outgoing edges, so nothing can be resurrected,
#:   re-run in place, or quietly overwritten.
ALLOWED_TRANSITIONS: Dict[ExecutionState, FrozenSet[ExecutionState]] = {
    ExecutionState.PROPOSED: frozenset({
        ExecutionState.APPROVED,
        ExecutionState.REVOKED,
        ExecutionState.REJECTED,
        ExecutionState.EXPIRED,
        ExecutionState.CANCELLED,
    }),
    ExecutionState.APPROVED: frozenset({
        ExecutionState.EXECUTING,
        ExecutionState.REVOKED,
        ExecutionState.EXPIRED,
        ExecutionState.CANCELLED,
        ExecutionState.REJECTED,
    }),
    ExecutionState.EXECUTING: frozenset({
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
    }),
    # Terminal. No recovery mechanism exists, and none is implied: re-running
    # means a new record with its own approval.
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.REVOKED: frozenset(),
    ExecutionState.EXPIRED: frozenset(),
    ExecutionState.REJECTED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
}

#: States from which nothing further can happen.
TERMINAL_STATES: FrozenSet[ExecutionState] = frozenset(
    state for state, onward in ALLOWED_TRANSITIONS.items() if not onward
)

#: The only state a tool may be run from. Named so the dispatcher reads as a
#: statement of the rule rather than a comparison.
RUNNABLE_FROM: ExecutionState = ExecutionState.APPROVED


def can_transition(current: ExecutionState, target: ExecutionState) -> bool:
    """True when `current -> target` is a declared edge."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def is_terminal(state: ExecutionState) -> bool:
    return state in TERMINAL_STATES


def succeeded(state: ExecutionState) -> bool:
    """The one state from which success may be claimed.

    A function rather than a comparison at each call site, so "did this
    actually succeed?" has exactly one implementation.
    """
    return state is ExecutionState.SUCCEEDED


__all__ = [
    "ALLOWED_TRANSITIONS",
    "RUNNABLE_FROM",
    "TERMINAL_STATES",
    "ExecutionState",
    "can_transition",
    "is_terminal",
    "succeeded",
]
