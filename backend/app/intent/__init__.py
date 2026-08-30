"""Stage 4A intent and task understanding.

Turns a user message into a typed statement of what they are trying to do:

    message -> IntentClassifier -> IntentClassification -> policy -> IntentResult
               (one model call)    (untrusted, validated)  (authoritative)

Understanding only. Stage 4A contains no planner, no tools, no executor and no
approval flow -- those are 4B through 4E. An `IntentResult` describing an
action records that a future stage would need to arrange one; it arranges
nothing, and there is nothing here for it to arrange.

The authority boundary lives in `policy.py`: capability flags are derived from
the intent, never parsed from the model's answer, so no model output and no
user message can produce a result that authorises anything.
"""

from app.intent.classifier import IntentClassificationError, IntentClassifier
from app.intent.policy import derive, fallback, resolve_primary
from app.intent.schemas import (
    Ambiguity,
    IntentClassification,
    IntentDebugRequest,
    IntentRead,
    IntentResult,
    IntentType,
)
from app.intent.service import IntentService

__all__ = [
    "Ambiguity",
    "IntentClassification",
    "IntentClassificationError",
    "IntentClassifier",
    "IntentDebugRequest",
    "IntentRead",
    "IntentResult",
    "IntentService",
    "IntentType",
    "derive",
    "fallback",
    "resolve_primary",
]
