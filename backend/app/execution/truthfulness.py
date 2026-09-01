"""What the application is willing to say about an execution.

The rule Stage 4E turns on: **never claim an action happened unless it did.**

The way that rule is kept is by making the claim a function of the recorded
state and nothing else. No caller composes its own sentence, no model writes
one, and there is no argument for "what I think happened" -- so a response
cannot describe an outcome the database does not record.

`SUCCEEDED` is the only state whose statement says the action was performed,
and `succeeded()` is true in that state alone. Every other state, including
`FAILED`, says plainly that it was not.
"""

from typing import Dict

from app.execution.states import ExecutionState

#: One sentence per state. Exhaustive by construction -- a test asserts every
#: member of `ExecutionState` has an entry, so adding a state without deciding
#: what it means fails rather than falling back to something vague.
STATEMENTS: Dict[ExecutionState, str] = {
    ExecutionState.PROPOSED: (
        "This action has been recorded but not approved. Nothing has been done."
    ),
    ExecutionState.APPROVED: (
        "This action has been approved but not yet run. Nothing has been done "
        "yet."
    ),
    ExecutionState.REVOKED: (
        "The approval for this action was withdrawn. It was not performed."
    ),
    ExecutionState.EXPIRED: (
        "The approval for this action expired before it ran. It was not "
        "performed."
    ),
    ExecutionState.REJECTED: (
        "This action was rejected. It was not performed."
    ),
    ExecutionState.CANCELLED: (
        "This action was cancelled. It was not performed."
    ),
    ExecutionState.EXECUTING: (
        "This action is running now. It has not finished, so its outcome is "
        "not yet known."
    ),
    ExecutionState.SUCCEEDED: (
        "This action was performed successfully."
    ),
    ExecutionState.FAILED: (
        "This action was attempted and failed. It did not complete."
    ),
}


def statement_for(state: ExecutionState) -> str:
    """The sentence for a state.

    Total: an unrecognised state yields the most conservative statement rather
    than raising or returning an empty string. A response that cannot describe
    what happened must not imply that something did.
    """
    return STATEMENTS.get(
        state,
        "The status of this action is unknown. Do not assume it was performed.",
    )


def succeeded(state: ExecutionState) -> bool:
    """True in exactly one state.

    Written as an identity check against a single member rather than as a
    negation of the failure states. The difference matters: a new state added
    later is false here by default, and would have been true under
    `state not in FAILURES`.
    """
    return state is ExecutionState.SUCCEEDED


__all__ = ["STATEMENTS", "statement_for", "succeeded"]
