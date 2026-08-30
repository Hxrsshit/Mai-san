"""Plan inspection endpoint.

Shows the plan Mai would draw up for a request, without answering it, storing
it, or acting on it.

Like the intent endpoint it makes model calls -- at most two: one to classify
the request, one to plan it, and only if the classification warrants planning.
An ineligible request costs one call and returns why.

It writes nothing, and it cannot act. A plan whose first task is "Send the
outreach email" is a sentence: Stage 4B contains no executor for it to reach.
"""

from fastapi import APIRouter

from app.api.deps import Intent, Planning
from app.planning.schemas import PlanDebugRequest, PlanningRead

router = APIRouter(prefix="/api/planning", tags=["planning"])


@router.post(
    "/debug",
    response_model=PlanningRead,
    summary="Show the plan for a request, without acting on it",
)
async def debug_planning(
    payload: PlanDebugRequest, intent: Intent, planning: Planning
) -> PlanningRead:
    """Classify a request, then plan it if the intent warrants a plan.

    The same two services the chat path uses, in the same order, with the same
    deterministic eligibility check between them -- so what is shown is what a
    turn would produce, not a reconstruction of it.

    A `not_eligible` or `needs_clarification` status is a normal result, not an
    error: most messages do not warrant a plan, and a vague goal is better
    answered with a question than with an invented one.
    """
    understanding = await intent.understand(
        message=payload.message, conversation_id=payload.conversation_id
    )
    result = await planning.plan_for(payload.message, understanding)
    return PlanningRead.from_result(result)
