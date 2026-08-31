"""Deterministic action eligibility.

Which turns are even looked at for an action. Application policy, decided
before anything else runs and without a model.

**The allowlist is exactly {ACTION}, and that is forced by the architecture
rather than chosen for caution.**

Stage 4A sets `requires_execution=True` only for the ACTION intent, and clamps
it false for everything else. Stage 4C's `_intent_rule` then refuses *every*
tool when that flag is false. So a proposal raised from a RESEARCH, TASK,
PLANNING, QUESTION or CONVERSATION turn is already deterministically
`FORBIDDEN` before its tool is examined.

Making those intents action-capable would therefore buy nothing and cost
twice: work on turns whose outcome is known in advance, and a `forbidden`
reported to the user whose real cause is "Mai was never going to act here" --
which reads as a refusal rather than as an absence.

The narrower allowlist is not a weakening. It is the only value consistent
with the two boundaries already in place.

Where a research or planning request *should* eventually lead to an action,
the route is the one Stage 4B already describes: the request produces a plan,
and a later stage maps a plan task to a proposal that is authorized on its own
terms. A task is not an action, and this module is not the place that changes.
"""

from typing import FrozenSet, Optional, Tuple

from app.intent.schemas import IntentResult, IntentType
from app.orchestration.schemas import IneligibilityReason

#: Intents that may be examined for an action.
#:
#: Exactly the set Stage 4A grants execution capability to. If Stage 4A ever
#: widens that set, this follows automatically -- and the test below asserts
#: the two stay in step, so they cannot drift apart silently.
ACTION_CAPABLE_INTENTS: FrozenSet[IntentType] = frozenset({IntentType.ACTION})

#: Asked for when a request names a capability but is too vague to act on.
CLARIFICATION_PROMPT = (
    "What exactly would you like done, and with what?"
)


def decide(
    intent: Optional[IntentResult],
    message: str,
    enabled: bool = True,
) -> Tuple[bool, str]:
    """Should this turn be examined for an action?

    Returns `(eligible, reason)`. Total and deterministic; the same input
    always produces the same answer, and no model is consulted.

    Checks run cheapest-first so the common case -- an ordinary message that
    warrants no action -- costs a set membership test and stops.
    """
    if not enabled:
        return False, IneligibilityReason.DISABLED

    if not message or not message.strip():
        return False, IneligibilityReason.EMPTY_MESSAGE

    if intent is None or not intent.classified:
        # Classification failed, so Mai does not know what was asked. Guessing
        # at an action from an unclassified message is the one place where a
        # wrong guess is expensive.
        return False, IneligibilityReason.NO_INTENT

    if intent.intent_type not in ACTION_CAPABLE_INTENTS:
        return False, IneligibilityReason.INTENT_NOT_ACTION_CAPABLE

    return True, ""


def is_action_capable(intent_type: IntentType) -> bool:
    return intent_type in ACTION_CAPABLE_INTENTS


__all__ = [
    "ACTION_CAPABLE_INTENTS",
    "CLARIFICATION_PROMPT",
    "decide",
    "is_action_capable",
]
