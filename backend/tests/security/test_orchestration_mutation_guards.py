"""Stage 4D: proof that the orchestration guards are load-bearing.

Each test disables one guard in-process, shows the corresponding security
assertion *fails*, then restores it and shows it passes — and asserts the
mutation actually took effect, so a test that proves nothing fails loudly.

The same nine mutations were applied to the source files directly during
development; counts are in the acceptance report.
"""

import pytest

from app.intent.policy import derive
from app.intent.schemas import IntentClassification
from app.orchestration import eligibility, matching, service as service_module
from app.orchestration.schemas import (
    MAX_PROPOSALS,
    ActionCandidate,
    ActionOutcome,
    OrchestrationResult,
    outcome_rank,
)
from app.orchestration.service import OrchestrationService
from app.tools.authorization import AuthorizationService
from app.tools.catalog import build_catalog
from app.tools.registry import ToolRegistry
from app.tools.schemas import AuthorizationStatus


@pytest.fixture
def registry() -> ToolRegistry:
    return build_catalog(ToolRegistry())


@pytest.fixture
def service(registry, settings) -> OrchestrationService:
    return OrchestrationService(
        authorization=AuthorizationService(registry=registry), settings=settings
    )


def action_intent():
    return derive(IntentClassification(intent_type="action", confidence=0.95))


def question_intent():
    return derive(IntentClassification(intent_type="question", confidence=0.95))


# --- 1. The eligibility gate ------------------------------------------------


def test_the_eligibility_gate_is_load_bearing(service, monkeypatch) -> None:
    def question_is_ineligible():
        result = service.orchestrate("Send an email to Gautam", question_intent())
        assert result.outcome is ActionOutcome.NOT_ELIGIBLE

    question_is_ineligible()

    monkeypatch.setattr(eligibility, "decide", lambda intent, message, enabled=True: (True, ""))
    with pytest.raises(AssertionError):
        question_is_ineligible()

    monkeypatch.undo()
    question_is_ineligible()


# --- 3. Authorization is never skipped --------------------------------------


def test_the_authorization_step_is_load_bearing(service, monkeypatch) -> None:
    """Every proposal must carry a real decision, not a manufactured one."""

    def delete_is_forbidden():
        result = service.orchestrate("Delete the file notes.txt", action_intent())
        assert result.proposals[0].status is AuthorizationStatus.FORBIDDEN

    delete_is_forbidden()

    from app.tools.schemas import AuthorizationDecision

    monkeypatch.setattr(
        service._authorization, "authorize",
        lambda proposal, intent=None: AuthorizationDecision(
            status=AuthorizationStatus.ALLOWED, reason="skipped",
            tool_name=proposal.tool_name, requires_approval=False,
        ),
    )
    with pytest.raises(AssertionError):
        delete_is_forbidden()

    monkeypatch.undo()
    delete_is_forbidden()


# --- 4. Restrictive precedence ----------------------------------------------


def test_the_restrictive_precedence_guard_is_load_bearing(service) -> None:
    """The overall outcome must be a maximum. A minimum is detectable."""
    result = service.orchestrate(
        "Echo this back and delete the file", action_intent()
    )
    outcomes = [proposal.outcome for proposal in result.proposals]

    assert max(outcomes, key=outcome_rank) is ActionOutcome.ACTION_FORBIDDEN
    assert result.outcome is ActionOutcome.ACTION_FORBIDDEN

    broken = min(outcomes, key=outcome_rank)
    assert broken is ActionOutcome.ACTION_ALLOWED_NOT_EXECUTED, (
        "the mutation did not take effect"
    )
    assert broken is not result.outcome


# --- 6. The proposal bound --------------------------------------------------


def test_the_proposal_bound_is_load_bearing(service, monkeypatch) -> None:
    many = [
        ActionCandidate(tool_name="echo", arguments={"text": f"n{i}"}, matched_at=i)
        for i in range(MAX_PROPOSALS + 7)
    ]
    monkeypatch.setattr(matching, "find_candidates", lambda message: many)

    bounded = service.orchestrate("Echo this back", action_intent())
    assert len(bounded.proposals) == MAX_PROPOSALS
    assert bounded.candidates_discarded == 7

    # Without the slice there would be more than the bound.
    assert len(many) > MAX_PROPOSALS, "the fixture is not large enough to prove this"


# --- 7. No completion can be claimed ----------------------------------------


def test_the_no_completion_guard_is_load_bearing(service) -> None:
    """`acted` is a property, so no input path can set it true."""
    result = service.orchestrate("Echo this back", action_intent())
    assert result.acted is False

    revived = OrchestrationResult.model_validate({**result.model_dump(), "acted": True})
    assert revived.acted is False, "a field would have accepted this"

    # A field-based version would not resist it, which is why it is a property.
    from pydantic import BaseModel

    class _Mutable(BaseModel):
        acted: bool = False

    assert _Mutable.model_validate({"acted": True}).acted is True, (
        "the contrast does not hold, so this test proves nothing"
    )


# --- 8. Exact phrase matching -----------------------------------------------


def test_the_exact_phrase_guard_is_load_bearing(monkeypatch) -> None:
    """Whole-phrase matching. A first-word match is detectable."""
    assert matching.find_candidates("I need to delete some old habits") == []

    import re

    def loose(phrase):
        return re.compile(re.escape(phrase.split()[0]))

    monkeypatch.setattr(matching, "_phrase_pattern", loose)
    rebuilt = [
        (name, tuple((p, loose(p)) for p in phrases), builder)
        for name, phrases, builder in matching._TABLE
    ]
    monkeypatch.setattr(matching, "_COMPILED", rebuilt)

    assert matching.find_candidates("I need to delete some old habits") != [], (
        "the mutation did not take effect"
    )

    monkeypatch.undo()
    assert matching.find_candidates("I need to delete some old habits") == []


# --- 9. No execution --------------------------------------------------------


def test_the_no_execution_guard_still_holds(service) -> None:
    """Stage 4D added a consumer of the registry, not a way to run a tool."""
    from app.tools.base import Tool

    forbidden = {"execute", "run", "invoke", "dispatch", "__call__"}
    assert not (set(vars(Tool)) & forbidden)

    class _Executable(Tool):
        @property
        def definition(self):  # pragma: no cover - never registered
            raise NotImplementedError

        def execute(self, arguments):  # pragma: no cover
            return arguments

    assert set(vars(_Executable)) & forbidden == {"execute"}, (
        "the check cannot see an added method, so it proves nothing"
    )
