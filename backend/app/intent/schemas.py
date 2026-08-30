"""Stage 4A intent schemas.

Two types, deliberately kept apart:

- `IntentClassification` is what the **model** is allowed to say. It is parsed
  from untrusted output and carries no authority.
- `IntentResult` is what the **application** concludes. Its capability flags
  are computed by `policy.py`, never parsed.

That separation is the security design of the whole stage. A model cannot
return `requires_execution=True` and have it believed, because the field is not
read from its output at all. See `app/intent/policy.py`.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IntentType(str, Enum):
    """What the user is trying to do.

    Six categories the model may choose from, plus `UNKNOWN`, which only the
    application assigns. A model that returns "unknown" is rejected like any
    other invalid value -- see `MODEL_SELECTABLE_INTENTS`.
    """

    CONVERSATION = "conversation"
    QUESTION = "question"
    PLANNING = "planning"
    RESEARCH = "research"
    TASK = "task"
    ACTION = "action"

    #: Application-only. Means "classification did not produce a usable
    #: answer", never "the user wants something unknown". Assigned on
    #: provider failure, malformed output, or an empty message.
    UNKNOWN = "unknown"


#: The closed set a model may return. `UNKNOWN` is excluded on purpose: it is
#: the application's way of recording its own failure, and letting the model
#: claim it would blur a degraded classification with a confident one.
MODEL_SELECTABLE_INTENTS = frozenset(
    intent for intent in IntentType if intent is not IntentType.UNKNOWN
)

#: Intents that describe talking, not doing. No capability flag may ever be
#: true for these, whatever the model asserts.
CONVERSATIONAL_INTENTS = frozenset(
    {IntentType.CONVERSATION, IntentType.QUESTION, IntentType.UNKNOWN}
)


class Ambiguity(str, Enum):
    """How clear the request is.

    Not a confidence score. A request can be perfectly clear and still be
    classified with low confidence, and a vague one ("do something about
    this") can be confidently identified *as* vague.
    """

    NONE = "none"
    MILD = "mild"
    HIGH = "high"


class IntentClassification(BaseModel):
    """The model's answer, validated. Carries interpretation, not authority.

    `extra="ignore"`: a model that invents fields must not be able to widen
    the shape. Anything not named here is dropped before it is ever read.
    """

    model_config = ConfigDict(extra="ignore")

    intent_type: IntentType
    confidence: float = Field(..., ge=0.0, le=1.0)

    #: What the user is trying to achieve, in their own terms. Free text from
    #: the model, treated as untrusted data everywhere downstream.
    goal: Optional[str] = Field(default=None, max_length=500)
    #: The concrete thing they asked for, when there is one.
    requested_outcome: Optional[str] = Field(default=None, max_length=500)

    ambiguity: Ambiguity = Ambiguity.NONE
    ambiguity_reason: Optional[str] = Field(default=None, max_length=300)

    #: Hints only. `policy.py` decides what the application acts on; these are
    #: inputs to that decision, not the decision.
    suggests_planning: bool = False
    suggests_research: bool = False

    #: Other intents present in a mixed request. Bounded, deduplicated, and
    #: never containing the primary.
    secondary_intents: List[IntentType] = Field(default_factory=list, max_length=3)

    @field_validator("intent_type")
    @classmethod
    def _must_be_model_selectable(cls, value: IntentType) -> IntentType:
        if value not in MODEL_SELECTABLE_INTENTS:
            raise ValueError(
                f"{value.value!r} is not a classification a model may return"
            )
        return value

    @field_validator("secondary_intents")
    @classmethod
    def _clean_secondaries(cls, value: List[IntentType]) -> List[IntentType]:
        seen = []
        for intent in value:
            if intent in MODEL_SELECTABLE_INTENTS and intent not in seen:
                seen.append(intent)
        return seen

    @field_validator("goal", "requested_outcome", "ambiguity_reason")
    @classmethod
    def _blank_is_absent(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None


class IntentResult(BaseModel):
    """What the application concluded. The output of Stage 4A.

    The capability flags below are **derived**, never parsed. They describe
    what a *future* stage would have to arrange before anything could happen --
    they authorise nothing themselves, and Stage 4A contains no code that
    reads them to decide an action, because Stage 4A performs no actions.
    """

    model_config = ConfigDict(frozen=True)

    intent_type: IntentType
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    goal: Optional[str] = None
    requested_outcome: Optional[str] = None

    ambiguity: Ambiguity = Ambiguity.NONE
    ambiguity_reason: Optional[str] = None
    secondary_intents: List[IntentType] = Field(default_factory=list)

    #: Derived by `policy.derive`. A future planner would need to run.
    requires_planning: bool = False
    #: Derived. Information gathering would be needed first.
    requires_research: bool = False
    #: Derived **solely** from `intent_type is ACTION`. Never model-supplied.
    requires_execution: bool = False
    #: Derived. Always true wherever `requires_execution` is true; there is no
    #: combination of inputs that yields execution without approval.
    requires_user_approval: bool = False

    # --- Provenance ---------------------------------------------------------

    #: False when this is a fallback rather than a real classification.
    classified: bool = True
    #: Why classification degraded, when it did. Never contains model output.
    degraded_reason: Optional[str] = None
    #: Model calls made to produce this. Bounded at 1 by construction.
    model_calls: int = Field(default=0, ge=0, le=1)
    duration_ms: float = 0.0

    @property
    def is_actionable(self) -> bool:
        """True when a later stage would have work to do.

        Descriptive, not permissive. Nothing in Stage 4A branches on it.
        """
        return self.requires_planning or self.requires_research or self.requires_execution

    @property
    def is_degraded(self) -> bool:
        return not self.classified


class IntentRead(BaseModel):
    """The API view of an intent result.

    Deliberately narrower than `IntentResult`: timings and call counts are
    operational detail and stay in the logs.
    """

    model_config = ConfigDict(from_attributes=True)

    intent_type: IntentType
    confidence: float
    goal: Optional[str] = None
    requested_outcome: Optional[str] = None
    ambiguity: Ambiguity
    ambiguity_reason: Optional[str] = None
    secondary_intents: List[IntentType] = Field(default_factory=list)
    requires_planning: bool
    requires_research: bool
    requires_execution: bool
    requires_user_approval: bool
    classified: bool
    degraded_reason: Optional[str] = None


class IntentDebugRequest(BaseModel):
    conversation_id: Optional[uuid.UUID] = None
    message: str = Field(..., min_length=1, max_length=8000)


__all__ = [
    "Ambiguity",
    "CONVERSATIONAL_INTENTS",
    "IntentClassification",
    "IntentDebugRequest",
    "IntentRead",
    "IntentResult",
    "IntentType",
    "MODEL_SELECTABLE_INTENTS",
]
