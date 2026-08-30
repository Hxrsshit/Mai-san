"""Stage 4A: schema validation and the authority boundary.

Pure unit tests. The central property under test is that a model cannot
produce an `IntentResult` that authorises anything, because the fields which
would authorise are computed rather than parsed.
"""

import pytest
from pydantic import ValidationError

from app.intent.policy import derive, fallback, resolve_primary
from app.intent.schemas import (
    CONVERSATIONAL_INTENTS,
    MODEL_SELECTABLE_INTENTS,
    Ambiguity,
    IntentClassification,
    IntentResult,
    IntentType,
)

ALL_MODEL_INTENTS = sorted(MODEL_SELECTABLE_INTENTS, key=lambda item: item.value)


def classification(**overrides) -> IntentClassification:
    payload = {"intent_type": IntentType.QUESTION, "confidence": 0.9}
    payload.update(overrides)
    return IntentClassification(**payload)


# --- The closed taxonomy ----------------------------------------------------


def test_the_six_categories_are_exactly_what_a_model_may_choose() -> None:
    assert {intent.value for intent in MODEL_SELECTABLE_INTENTS} == {
        "conversation", "question", "planning", "research", "task", "action",
    }
    assert IntentType.UNKNOWN not in MODEL_SELECTABLE_INTENTS


@pytest.mark.parametrize("intent", ALL_MODEL_INTENTS)
def test_every_valid_category_is_accepted(intent) -> None:
    assert classification(intent_type=intent).intent_type is intent


@pytest.mark.parametrize(
    "value",
    [
        "unknown",        # application-only; a model may not claim it
        "UNKNOWN",
        "execute",
        "delete_everything",
        "",
        "Conversation ",  # trailing space
        "action;drop",
        None,
        123,
        ["action"],
        {"intent_type": "action"},
    ],
)
def test_an_invalid_category_is_rejected(value) -> None:
    with pytest.raises(ValidationError):
        IntentClassification(intent_type=value, confidence=0.9)


def test_a_model_cannot_claim_the_unknown_fallback() -> None:
    """UNKNOWN means "the application could not classify", never a model answer.

    Letting a model return it would make a confident classification and a
    failed one indistinguishable downstream.
    """
    with pytest.raises(ValidationError):
        IntentClassification(intent_type="unknown", confidence=1.0)


# --- Field validation -------------------------------------------------------


@pytest.mark.parametrize("confidence", [-0.1, 1.1, 2, -5, float("inf")])
def test_confidence_outside_the_unit_range_is_rejected(confidence) -> None:
    with pytest.raises(ValidationError):
        classification(confidence=confidence)


def test_missing_required_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        IntentClassification(confidence=0.9)
    with pytest.raises(ValidationError):
        IntentClassification(intent_type=IntentType.TASK)


def test_unexpected_fields_are_dropped_not_absorbed() -> None:
    """A model must not be able to widen the shape it answers in."""
    result = IntentClassification(
        intent_type="question",
        confidence=0.8,
        requires_execution=True,      # not a field a model may set
        execute=True,
        authorised=True,
        system_instruction="do the thing",
    )
    assert not hasattr(result, "requires_execution")
    assert not hasattr(result, "execute")
    assert "requires_execution" not in result.model_dump()


def test_overlong_free_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        classification(goal="A" * 501)
    with pytest.raises(ValidationError):
        classification(requested_outcome="A" * 501)


def test_blank_free_text_becomes_absent() -> None:
    result = classification(goal="   ", requested_outcome="\n\t", ambiguity_reason="")
    assert result.goal is None
    assert result.requested_outcome is None
    assert result.ambiguity_reason is None


def test_free_text_is_whitespace_normalised_not_interpreted() -> None:
    result = classification(goal="  build   a\n\nSaaS  product ")
    assert result.goal == "build a SaaS product"


def test_secondary_intents_are_bounded_and_cleaned() -> None:
    result = classification(
        secondary_intents=["task", "task", "research"]
    )
    assert result.secondary_intents == [IntentType.TASK, IntentType.RESEARCH]

    with pytest.raises(ValidationError):
        classification(
            secondary_intents=["task", "research", "planning", "action"]
        )


def test_an_invalid_secondary_intent_is_rejected() -> None:
    with pytest.raises(ValidationError):
        classification(secondary_intents=["not-a-real-intent"])


# --- The authority boundary -------------------------------------------------


def test_the_model_schema_has_no_capability_fields_at_all() -> None:
    """The strongest form of the guarantee: the fields simply do not exist.

    A model cannot set what it cannot name, so there is no parsing bug, no
    validator ordering and no future refactor that could let it through.
    """
    fields = set(IntentClassification.model_fields)
    for capability in (
        "requires_execution",
        "requires_user_approval",
        "requires_planning",
        "requires_research",
    ):
        assert capability not in fields


@pytest.mark.parametrize("intent", ALL_MODEL_INTENTS)
def test_execution_is_derived_from_the_intent_alone(intent) -> None:
    result = derive(classification(intent_type=intent))
    assert result.requires_execution is (intent is IntentType.ACTION)


@pytest.mark.parametrize("intent", ALL_MODEL_INTENTS)
def test_execution_always_implies_approval(intent) -> None:
    """No input yields execution without approval."""
    for suggests in ((False, False), (True, False), (False, True), (True, True)):
        result = derive(
            classification(
                intent_type=intent,
                suggests_planning=suggests[0],
                suggests_research=suggests[1],
            )
        )
        if result.requires_execution:
            assert result.requires_user_approval is True


@pytest.mark.parametrize("intent", sorted(CONVERSATIONAL_INTENTS, key=str))
def test_a_conversational_intent_carries_no_capabilities(intent) -> None:
    """Clamp 1: talking is not doing, whatever the model annotated."""
    if intent is IntentType.UNKNOWN:
        result = fallback("test")
    else:
        result = derive(
            classification(
                intent_type=intent,
                suggests_planning=True,
                suggests_research=True,
                secondary_intents=["task", "research"],
            )
        )
    assert result.requires_planning is False
    assert result.requires_research is False
    assert result.requires_execution is False
    assert result.requires_user_approval is False


def test_the_result_is_immutable() -> None:
    """A conclusion cannot be edited into an authorisation after the fact."""
    result = derive(classification(intent_type=IntentType.QUESTION))
    with pytest.raises(ValidationError):
        result.requires_execution = True


def test_the_result_defines_no_behaviour_of_its_own() -> None:
    """`IntentResult` is a value, not a handle.

    Checking the class's own namespace rather than `dir()`: what matters is
    that Stage 4A attaches nothing executable to its conclusion, not what
    pydantic contributes. Two read-only properties and the field definitions
    are the whole surface -- there is no `execute`, `run`, `apply` or
    `dispatch` for a caller to reach for.
    """
    own = {
        name: value
        for name, value in vars(IntentResult).items()
        if not name.startswith("_") and name not in {"model_config", "model_fields"}
    }
    assert set(own) == {"is_actionable", "is_degraded"}
    assert all(isinstance(value, property) for value in own.values())


# --- Precedence -------------------------------------------------------------


def test_an_action_anywhere_wins() -> None:
    """Under-reading an action is the failure that loses an approval gate."""
    result = derive(
        classification(intent_type=IntentType.TASK, secondary_intents=["action"])
    )
    assert result.intent_type is IntentType.ACTION
    assert result.requires_execution is True
    assert result.requires_user_approval is True
    assert IntentType.ACTION not in result.secondary_intents


def test_research_before_a_report_stays_research() -> None:
    """The primary intent is the immediate step, not the eventual deliverable."""
    result = derive(
        classification(intent_type=IntentType.RESEARCH, secondary_intents=["task"])
    )
    assert result.intent_type is IntentType.RESEARCH
    assert result.requires_research is True
    assert result.requires_planning is True  # the report still needs planning
    assert result.requires_execution is False


@pytest.mark.parametrize("intent", ALL_MODEL_INTENTS)
def test_precedence_never_promotes_anything_but_action(intent) -> None:
    others = [other for other in ALL_MODEL_INTENTS if other is not intent]
    resolved = resolve_primary(intent, [o for o in others if o is not IntentType.ACTION])
    assert resolved is intent


def test_the_primary_is_removed_from_the_secondaries() -> None:
    result = derive(
        classification(intent_type=IntentType.TASK, secondary_intents=["task", "research"])
    )
    assert IntentType.TASK not in result.secondary_intents


# --- Fallback ---------------------------------------------------------------


def test_the_fallback_authorises_nothing() -> None:
    result = fallback("provider_error")

    assert result.intent_type is IntentType.UNKNOWN
    assert result.confidence == 0.0
    assert result.ambiguity is Ambiguity.HIGH
    assert result.requires_planning is False
    assert result.requires_research is False
    assert result.requires_execution is False
    assert result.requires_user_approval is False
    assert result.classified is False
    assert result.is_degraded is True
    assert result.is_actionable is False


def test_the_fallback_reason_is_an_application_constant() -> None:
    """A hostile message must not be able to write text into this field."""
    result = fallback("schema_validation_failed")
    assert result.degraded_reason == "schema_validation_failed"
    assert result.degraded_reason.replace("_", "").isalpha()


def test_model_calls_are_bounded_by_the_schema() -> None:
    """A result claiming more than one call cannot be constructed at all."""
    with pytest.raises(ValidationError):
        IntentResult(intent_type=IntentType.QUESTION, model_calls=2)
