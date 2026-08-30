"""Deterministic planning eligibility.

The application decides whether a message gets a plan. The model is never
asked, and never told -- it is handed a goal and produces a plan, having had no
say in whether one was warranted.

This is the same authority separation Stage 4A established, applied one level
up: Stage 4A's policy decides what a classification *implies*, and this decides
what an implication *warrants*. Both are pure functions over typed data, and
neither reads model output.

Nothing here calls a model, touches the database, or has any I/O.
"""

from typing import FrozenSet, Optional, Tuple

from app.intent.schemas import Ambiguity, IntentResult, IntentType
from app.planning.schemas import PlanStatus

#: Intents that warrant a plan.
#:
#: ACTION is included on purpose. A request to do something is exactly the kind
#: of thing worth laying out before anyone does it -- and planning it is the
#: safe half of handling it. The plan is inert either way; Stage 4B has no
#: executor, so an ACTION plan is a description of work, identical in power to
#: every other plan.
PLANNABLE_INTENTS: FrozenSet[IntentType] = frozenset(
    {
        IntentType.PLANNING,
        IntentType.TASK,
        IntentType.RESEARCH,
        IntentType.ACTION,
    }
)

#: Intents that do not warrant a plan. A question wants an answer, and
#: answering it with a four-step project plan is a worse response, not a
#: richer one. UNKNOWN is here because a failed classification means Mai does
#: not know what was asked -- and planning against a guess is how invented
#: constraints get in.
NON_PLANNABLE_INTENTS: FrozenSet[IntentType] = frozenset(
    set(IntentType) - PLANNABLE_INTENTS
)

#: What the user is asked for when a goal is too vague to plan.
CLARIFICATION_PROMPT = (
    "What specifically would you like to achieve, and what does a good "
    "outcome look like?"
)


class Eligibility:
    """Reason codes. Application constants; none derive from model output."""

    ELIGIBLE = "eligible"
    DISABLED = "planning_disabled"
    NOT_PLANNABLE_INTENT = "intent_does_not_call_for_a_plan"
    INTENT_UNAVAILABLE = "intent_not_classified"
    GOAL_TOO_VAGUE = "goal_too_vague_to_plan"
    EMPTY_MESSAGE = "empty_message"


def decide(
    intent: Optional[IntentResult],
    message: str,
    enabled: bool = True,
) -> Tuple[bool, PlanStatus, str]:
    """Should this message be planned?

    Returns `(eligible, status, reason)`. Total and deterministic: every input
    produces an answer, and the same input always produces the same one.

    The checks run cheapest-first, so the common case -- an ordinary message
    that warrants no plan -- costs nothing and, crucially, makes no model call.
    """
    if not enabled:
        return False, PlanStatus.DISABLED, Eligibility.DISABLED

    if not message or not message.strip():
        return False, PlanStatus.NOT_ELIGIBLE, Eligibility.EMPTY_MESSAGE

    if intent is None:
        return False, PlanStatus.NOT_ELIGIBLE, Eligibility.INTENT_UNAVAILABLE

    if not intent.classified:
        # Classification degraded. Mai does not know what was asked, and
        # planning against a guess is precisely how invented constraints
        # enter a plan.
        return False, PlanStatus.NOT_ELIGIBLE, Eligibility.INTENT_UNAVAILABLE

    if intent.intent_type not in PLANNABLE_INTENTS:
        return False, PlanStatus.NOT_ELIGIBLE, Eligibility.NOT_PLANNABLE_INTENT

    if intent.ambiguity is Ambiguity.HIGH:
        # "Do something about this." A plan here would be invention: the model
        # would have to supply the subject, the outcome and the constraints,
        # and every one of those would arrive labelled as a plan rather than
        # as a guess.
        #
        # Asking is also cheaper than guessing -- this branch makes no model
        # call at all.
        return False, PlanStatus.NEEDS_CLARIFICATION, Eligibility.GOAL_TOO_VAGUE

    return True, PlanStatus.READY, Eligibility.ELIGIBLE


def is_plannable(intent_type: IntentType) -> bool:
    return intent_type in PLANNABLE_INTENTS


__all__ = [
    "CLARIFICATION_PROMPT",
    "Eligibility",
    "NON_PLANNABLE_INTENTS",
    "PLANNABLE_INTENTS",
    "decide",
    "is_plannable",
]
