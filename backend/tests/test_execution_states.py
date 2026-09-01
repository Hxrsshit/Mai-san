"""Stage 4E: the state machine and what the application says about it.

Two small modules, both of them tables. The value of a table is that it can be
checked exhaustively -- every state has a statement, every transition is
declared, and nothing is decided by a conditional somewhere else.
"""

import pytest

from app.execution import truthfulness
from app.execution.states import (
    ALLOWED_TRANSITIONS,
    RUNNABLE_FROM,
    TERMINAL_STATES,
    ExecutionState,
    can_transition,
    is_terminal,
)


# --- Transitions ------------------------------------------------------------


def test_only_approved_can_begin_executing() -> None:
    """The single most important edge in the machine.

    Everything else in Stage 4E -- approval binding, expiry, revocation --
    depends on there being no other way into EXECUTING.
    """
    for state in ExecutionState:
        expected = state is ExecutionState.APPROVED
        assert can_transition(state, ExecutionState.EXECUTING) is expected, state

    assert RUNNABLE_FROM is ExecutionState.APPROVED


def test_a_proposal_cannot_jump_straight_to_running() -> None:
    assert can_transition(ExecutionState.PROPOSED, ExecutionState.EXECUTING) is False
    assert can_transition(ExecutionState.PROPOSED, ExecutionState.SUCCEEDED) is False


def test_terminal_states_have_no_way_out() -> None:
    """Terminal means terminal: no edge, to anything, ever."""
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS.get(state, frozenset()) == frozenset(), state
        for target in ExecutionState:
            assert can_transition(state, target) is False, (state, target)


def test_a_completed_execution_cannot_run_again() -> None:
    for state in (ExecutionState.SUCCEEDED, ExecutionState.FAILED):
        assert is_terminal(state)
        assert can_transition(state, ExecutionState.EXECUTING) is False


def test_no_state_can_transition_to_itself() -> None:
    """A self-edge would let a record be re-approved or re-run in place."""
    for state in ExecutionState:
        assert can_transition(state, state) is False, state


def test_every_declared_transition_names_a_real_state() -> None:
    """A typo in the table would otherwise be an unreachable state."""
    for source, targets in ALLOWED_TRANSITIONS.items():
        assert isinstance(source, ExecutionState)
        for target in targets:
            assert isinstance(target, ExecutionState), (source, target)


def test_every_state_appears_in_the_table() -> None:
    """Exhaustive, so a new state must be given edges deliberately."""
    for state in ExecutionState:
        assert state in ALLOWED_TRANSITIONS, state


# --- Truthfulness -----------------------------------------------------------


def test_every_state_has_a_statement() -> None:
    for state in ExecutionState:
        assert state in truthfulness.STATEMENTS, state
        assert truthfulness.STATEMENTS[state].strip()


def test_only_succeeded_claims_the_action_happened() -> None:
    """The rule the whole stage turns on, checked against every state."""
    for state in ExecutionState:
        assert truthfulness.succeeded(state) is (state is ExecutionState.SUCCEEDED)


def test_no_unsuccessful_statement_implies_the_action_happened(
) -> None:
    """Read the sentences, not just the boolean.

    A response is text a person reads. `succeeded=False` beside "the file was
    created" would still mislead, so the statements themselves are checked for
    language that asserts completion.
    """
    for state in ExecutionState:
        if state is ExecutionState.SUCCEEDED:
            continue
        sentence = truthfulness.statement_for(state).lower()
        assert "was performed successfully" not in sentence, state
        assert "completed successfully" not in sentence, state
        if state is not ExecutionState.EXECUTING:
            # EXECUTING is the one honest "we do not know yet".
            assert (
                "nothing has been done" in sentence
                or "not performed" in sentence
                or "did not complete" in sentence
            ), state


def test_an_unrecognised_state_gets_the_most_conservative_statement() -> None:
    """Total by design: never a blank, never an exception, never a claim."""
    statement = truthfulness.statement_for("something-nobody-declared")

    assert "unknown" in statement.lower()
    assert "do not assume it was performed" in statement.lower()
    assert truthfulness.succeeded("something-nobody-declared") is False
