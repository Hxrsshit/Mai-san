"""Stage 4D: eligibility, matching, proposals and authorization integration."""

import json

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.intent.policy import derive, fallback
from app.intent.schemas import IntentClassification, MODEL_SELECTABLE_INTENTS, IntentType
from app.orchestration import eligibility, matching
from app.orchestration.schemas import (
    MAX_PROPOSALS,
    ActionCandidate,
    ActionOutcome,
    IneligibilityReason,
    OrchestrationResult,
    outcome_rank,
)
from app.orchestration.service import OrchestrationService
from app.tools.authorization import AuthorizationService
from app.tools.catalog import build_catalog
from app.tools.registry import ToolRegistry
from app.tools.schemas import ActionSource, AuthorizationStatus

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})
ACTION_INTENT = json.dumps(
    {"intent_type": "action", "confidence": 0.95, "ambiguity": "none"}
)


def intent_for(kind, **overrides):
    payload = {"intent_type": kind, "confidence": 0.9}
    payload.update(overrides)
    return derive(IntentClassification(**payload))


@pytest.fixture
def registry() -> ToolRegistry:
    return build_catalog(ToolRegistry())


@pytest.fixture
def service(registry, settings) -> OrchestrationService:
    return OrchestrationService(
        authorization=AuthorizationService(registry=registry), settings=settings
    )


# --- Eligibility ------------------------------------------------------------


def test_the_allowlist_matches_stage_4a_execution_capability() -> None:
    """The two must not drift apart.

    Eligibility exists to follow Stage 4A's capability boundary, not to restate
    it. If 4A ever grants execution capability to another intent, this fails
    until eligibility is revisited deliberately.
    """
    grants_execution = {
        intent
        for intent in MODEL_SELECTABLE_INTENTS
        if derive(
            IntentClassification(intent_type=intent, confidence=1.0)
        ).requires_execution
    }
    assert eligibility.ACTION_CAPABLE_INTENTS == grants_execution
    assert eligibility.ACTION_CAPABLE_INTENTS == {IntentType.ACTION}


@pytest.mark.parametrize(
    "kind", ["conversation", "question", "planning", "research", "task"]
)
def test_a_non_action_intent_does_no_work(service, kind) -> None:
    """The gate. No matching, no proposal, no authorization call."""
    result = service.orchestrate("Please send an email to Gautam.", intent_for(kind))

    assert result.outcome is ActionOutcome.NOT_ELIGIBLE
    assert result.reason == IneligibilityReason.INTENT_NOT_ACTION_CAPABLE
    assert result.proposals == []
    assert result.model_calls == 0


def test_an_action_intent_enters_the_path(service) -> None:
    result = service.orchestrate(
        "Please send an email to Gautam.", intent_for("action")
    )

    assert result.outcome is ActionOutcome.ACTION_REQUIRES_APPROVAL
    assert len(result.proposals) == 1
    assert result.proposals[0].tool_name == "future_send_email"


def test_a_degraded_intent_does_no_work(service) -> None:
    result = service.orchestrate("Send an email.", fallback("provider_error"))

    assert result.outcome is ActionOutcome.NOT_ELIGIBLE
    assert result.reason == IneligibilityReason.NO_INTENT


def test_a_missing_intent_does_no_work(service) -> None:
    result = service.orchestrate("Send an email.", None)
    assert result.outcome is ActionOutcome.NOT_ELIGIBLE


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_an_empty_message_does_no_work(service, message) -> None:
    result = service.orchestrate(message, intent_for("action"))
    assert result.outcome is ActionOutcome.NOT_ELIGIBLE
    assert result.reason == IneligibilityReason.EMPTY_MESSAGE


def test_disabling_orchestration_does_no_work(registry, settings) -> None:
    settings.ORCHESTRATION_ENABLED = False
    service = OrchestrationService(
        authorization=AuthorizationService(registry=registry), settings=settings
    )

    result = service.orchestrate("Send an email.", intent_for("action"))

    assert result.outcome is ActionOutcome.NOT_ELIGIBLE
    assert result.reason == IneligibilityReason.DISABLED


# --- Matching ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Please send an email to Gautam", "future_send_email"),
        ("Search the web for competitors", "future_web_search"),
        ("Generate a document from this", "future_generate_document"),
        ("Delete the file called notes.txt", "future_delete_file"),
        ("Echo this back to me", "echo"),
    ],
)
def test_a_known_phrase_produces_a_candidate(message, expected) -> None:
    candidates = matching.find_candidates(message)
    assert [candidate.tool_name for candidate in candidates] == [expected]


@pytest.mark.parametrize(
    "message",
    [
        "How are you?",
        "What is Redis?",
        "I was thinking about email marketing",
        "The file system is interesting",
        "Tell me about web search engines",
        "I searched for it yesterday",
        "",
    ],
)
def test_an_ordinary_message_matches_nothing(message) -> None:
    """Missing an action is a conversation. Inventing one is an incident."""
    assert matching.find_candidates(message) == []


def test_matching_is_case_and_punctuation_insensitive() -> None:
    for phrasing in (
        "SEND AN EMAIL to him",
        "send an email, to him",
        "  Send   an   email  ",
    ):
        assert [c.tool_name for c in matching.find_candidates(phrasing)] == [
            "future_send_email"
        ]


def test_matching_is_deterministic() -> None:
    message = "Search the web and then send an email"
    runs = {
        tuple(c.tool_name for c in matching.find_candidates(message))
        for _ in range(20)
    }
    assert len(runs) == 1


def test_candidates_are_ordered_by_position() -> None:
    """A message naming two actions usually means them in that order."""
    first = matching.find_candidates("Search the web and then send an email")
    second = matching.find_candidates("Send an email after you search the web")

    assert [c.tool_name for c in first] == ["future_web_search", "future_send_email"]
    assert [c.tool_name for c in second] == ["future_send_email", "future_web_search"]


def test_every_mapped_tool_exists_in_the_registry(registry) -> None:
    """The table cannot name a capability the registry does not have."""
    matching.validate_table(registry)
    for name in matching.known_trigger_phrases():
        assert registry.contains(name), name


def test_the_matcher_cannot_emit_an_unregistered_name(registry) -> None:
    """Structural: the output set is exactly the table's keys."""
    mapped = set(matching.known_trigger_phrases())
    assert mapped <= set(registry.names())


def test_a_long_message_is_bounded_before_matching() -> None:
    message = ("padding " * 2000) + "send an email"
    # The trigger sits past the scan bound, so nothing matches -- bounded, and
    # failing toward no action.
    assert matching.find_candidates(message) == []


# --- Proposals --------------------------------------------------------------


def test_every_candidate_reaches_authorization(service) -> None:
    """One decision per proposal. Authorization is never skipped."""
    result = service.orchestrate(
        "Search the web and send an email and delete the file",
        intent_for("action"),
    )

    assert len(result.proposals) == 3
    for proposal in result.proposals:
        assert proposal.status in set(AuthorizationStatus)
        assert proposal.reason


def test_the_overall_outcome_is_the_most_restrictive(service) -> None:
    """One refused action is never hidden behind another that passed."""
    result = service.orchestrate(
        "Echo this back and delete the file", intent_for("action")
    )

    statuses = {proposal.status for proposal in result.proposals}
    assert AuthorizationStatus.ALLOWED in statuses
    assert AuthorizationStatus.FORBIDDEN in statuses
    assert result.outcome is ActionOutcome.ACTION_FORBIDDEN


def test_one_proposal_never_authorizes_another(service) -> None:
    result = service.orchestrate(
        "Echo this back and delete the file", intent_for("action")
    )

    by_tool = {proposal.tool_name: proposal for proposal in result.proposals}
    assert by_tool["echo"].status is AuthorizationStatus.ALLOWED
    assert by_tool["future_delete_file"].status is AuthorizationStatus.FORBIDDEN
    assert by_tool["future_delete_file"].requires_approval is True


def test_the_proposal_count_is_bounded(service, monkeypatch) -> None:
    many = [
        ActionCandidate(tool_name="echo", arguments={"text": f"item {index}"},
                        matched_at=index)
        for index in range(12)
    ]
    monkeypatch.setattr(matching, "find_candidates", lambda message: many)

    result = service.orchestrate("Echo this back", intent_for("action"))

    assert len(result.proposals) == MAX_PROPOSALS
    assert result.candidates_discarded == 12 - MAX_PROPOSALS


def test_exact_duplicates_are_removed(service, monkeypatch) -> None:
    duplicates = [
        ActionCandidate(tool_name="echo", arguments={"text": "same"}),
        ActionCandidate(tool_name="echo", arguments={"text": "same"}),
        ActionCandidate(tool_name="echo", arguments={"text": "different"}),
    ]
    monkeypatch.setattr(matching, "find_candidates", lambda message: duplicates)

    result = service.orchestrate("Echo this back", intent_for("action"))

    assert len(result.proposals) == 2
    assert result.duplicates_removed == 1


def test_different_arguments_are_two_actions(service, monkeypatch) -> None:
    """No fuzzy merging: one decision must not stand in for two actions."""
    candidates = [
        ActionCandidate(tool_name="echo", arguments={"text": "alpha"}),
        ActionCandidate(tool_name="echo", arguments={"text": "alphb"}),
    ]
    monkeypatch.setattr(matching, "find_candidates", lambda message: candidates)

    result = service.orchestrate("Echo this back", intent_for("action"))
    assert len(result.proposals) == 2


def test_an_unknown_candidate_does_not_poison_a_valid_one(
    service, monkeypatch
) -> None:
    candidates = [
        ActionCandidate(tool_name="echo", arguments={"text": "hello"}),
        ActionCandidate(tool_name="delete_everything", arguments={}),
    ]
    monkeypatch.setattr(matching, "find_candidates", lambda message: candidates)

    result = service.orchestrate("anything", intent_for("action"))

    by_tool = {proposal.tool_name: proposal for proposal in result.proposals}
    assert by_tool["echo"].status is AuthorizationStatus.ALLOWED
    assert by_tool["delete_everything"].status is AuthorizationStatus.UNKNOWN_TOOL
    assert result.outcome is ActionOutcome.ACTION_UNKNOWN


def test_invalid_arguments_forbid_only_their_own_proposal(
    service, monkeypatch
) -> None:
    candidates = [
        ActionCandidate(tool_name="echo", arguments={"text": "fine"}),
        ActionCandidate(tool_name="echo", arguments={"text": "", "bogus": 1}),
    ]
    monkeypatch.setattr(matching, "find_candidates", lambda message: candidates)

    result = service.orchestrate("anything", intent_for("action"))

    statuses = [proposal.status for proposal in result.proposals]
    assert AuthorizationStatus.ALLOWED in statuses
    assert AuthorizationStatus.FORBIDDEN in statuses


# --- Outcomes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message,outcome",
    [
        ("Echo this back to me", ActionOutcome.ACTION_ALLOWED_NOT_EXECUTED),
        ("Send an email to Gautam", ActionOutcome.ACTION_REQUIRES_APPROVAL),
        ("Delete the file notes.txt", ActionOutcome.ACTION_FORBIDDEN),
        ("Tell me a joke", ActionOutcome.NO_ACTION),
    ],
)
def test_every_outcome_is_reachable(service, message, outcome) -> None:
    """All five action outcomes occur through the real catalogue."""
    result = service.orchestrate(message, intent_for("action"))
    assert result.outcome is outcome


def test_the_unknown_outcome_is_reachable(service, monkeypatch) -> None:
    monkeypatch.setattr(
        matching, "find_candidates",
        lambda message: [ActionCandidate(tool_name="nope", arguments={})],
    )
    result = service.orchestrate("anything", intent_for("action"))
    assert result.outcome is ActionOutcome.ACTION_UNKNOWN


def test_the_outcome_ordering_is_total() -> None:
    ranks = [outcome_rank(o) for o in
             (ActionOutcome.ACTION_ALLOWED_NOT_EXECUTED,
              ActionOutcome.ACTION_REQUIRES_APPROVAL,
              ActionOutcome.ACTION_FORBIDDEN,
              ActionOutcome.ACTION_UNKNOWN)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4


# --- Cost and determinism ---------------------------------------------------


def test_orchestration_makes_no_model_call(service) -> None:
    for message in ("Send an email", "Echo this back", "Tell me a joke"):
        result = service.orchestrate(message, intent_for("action"))
        assert result.model_calls == 0


def test_a_result_cannot_claim_more_than_zero_calls() -> None:
    with pytest.raises(ValidationError):
        OrchestrationResult(outcome=ActionOutcome.NO_ACTION, model_calls=1)


def test_repeated_passes_are_identical(service) -> None:
    message = "Search the web and send an email"
    runs = {
        (
            service.orchestrate(message, intent_for("action")).outcome,
            tuple(
                p.tool_name
                for p in service.orchestrate(message, intent_for("action")).proposals
            ),
        )
        for _ in range(10)
    }
    assert len(runs) == 1


def test_a_result_is_frozen(service) -> None:
    result = service.orchestrate("Echo this back", intent_for("action"))
    with pytest.raises(ValidationError):
        result.outcome = ActionOutcome.NO_ACTION


# --- Failure handling -------------------------------------------------------


def test_a_matcher_failure_degrades_to_no_action(service, monkeypatch) -> None:
    def boom(message):
        raise RuntimeError("matcher is broken")

    monkeypatch.setattr(matching, "find_candidates", boom)

    result = service.orchestrate("Send an email", intent_for("action"))

    assert result.outcome is ActionOutcome.NO_ACTION
    assert result.proposals == []
    assert result.acted is False


def test_a_malformed_candidate_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        ActionCandidate(tool_name=None)


# --- Chat integration -------------------------------------------------------


async def test_an_action_turn_carries_an_orchestration_result(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.intent_reply = ACTION_INTENT
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Please send an email to Gautam about the proposal."},
    )

    assert response.status_code == 201
    body = response.json()["orchestration"]
    assert body["outcome"] == "action_requires_approval"
    assert body["executed"] is False
    assert body["proposals"][0]["tool_name"] == "future_send_email"
    assert body["proposals"][0]["executed"] is False


async def test_an_ordinary_turn_is_unchanged(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "How are you today?"},
    )

    body = response.json()
    assert body["orchestration"]["outcome"] == "not_eligible"
    assert body["orchestration"]["proposals"] == []
    assert body["assistant_message"]["content"] == fake_provider.reply


async def test_the_message_is_classified_once(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.intent_reply = ACTION_INTENT
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Send an email to Gautam."},
    )

    assert len(fake_provider.intent_calls) == 1


async def test_the_debug_endpoint_shows_the_same_flow(
    client: AsyncClient, fake_provider
) -> None:
    fake_provider.intent_reply = ACTION_INTENT

    response = await client.post(
        "/api/orchestration/debug",
        json={"message": "Please send an email to Gautam."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "action_requires_approval"
    assert body["executed"] is False


async def test_the_debug_endpoint_reports_ineligibility_not_an_error(
    client: AsyncClient, fake_provider
) -> None:
    fake_provider.intent_reply = json.dumps(
        {"intent_type": "question", "confidence": 0.9, "ambiguity": "none"}
    )

    response = await client.post(
        "/api/orchestration/debug",
        json={"message": "What is Redis? Also send an email."},
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "not_eligible"
