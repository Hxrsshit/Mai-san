"""Action orchestration inspection endpoint.

Shows what Mai would identify as a possible action in a message, and what the
authorization layer decides about it.

**Propose, authorize, return.** There is no execute step, no execute route, and
nothing that could reach one. Every proposal in the response carries
`executed: false`, and so does the result -- not as a claim about this request,
but because there is no state in which it could be true.

It writes nothing and costs no model call: identification is a deterministic
phrase lookup.
"""

from fastapi import APIRouter

from app.api.deps import Intent, Orchestration
from app.orchestration.schemas import (
    OrchestrationDebugRequest,
    OrchestrationRead,
)

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])


@router.post(
    "/debug",
    response_model=OrchestrationRead,
    summary="Show what action a message implies, and whether it would be permitted",
)
async def debug_orchestration(
    payload: OrchestrationDebugRequest, intent: Intent, orchestration: Orchestration
) -> OrchestrationRead:
    """Classify a message, then orchestrate it if the intent is action-capable.

    The same two steps the chat path takes, in the same order, with the same
    deterministic gate between them -- so what is shown is what a turn would
    produce.

    `not_eligible` and `no_action` are normal results, not errors: almost every
    message is one or the other. Costs one model call for classification and
    none for orchestration.
    """
    understanding = await intent.understand(message=payload.message)
    result = orchestration.orchestrate(payload.message, understanding)
    return OrchestrationRead.from_result(result)
