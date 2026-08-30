"""Context assembly debug endpoint.

Shows exactly what Stage 3A assembled: which recent messages were selected,
which retrieved knowledge survived, what it costs, and what the budget dropped.

This endpoint makes **no model call** and **mutates nothing** -- assembly is a
read-and-transform layer.
"""

from fastapi import APIRouter

from app.api.deps import Context
from app.context.schemas import ContextDebugRequest, ContextPackage

router = APIRouter(prefix="/api/context", tags=["context"])


@router.post(
    "/debug",
    response_model=ContextPackage,
    summary="Show the assembled context package for a message",
)
async def debug_context(
    payload: ContextDebugRequest, context: Context
) -> ContextPackage:
    """Assemble and return the package, without sending it anywhere.

    `conversation_id` is optional: omit it to see long-term knowledge alone,
    which is also how the failure-isolation behaviour can be inspected.

    Returns the package itself rather than a bespoke debug shape, so what is
    displayed is exactly what Stage 3B will consume -- categories separate,
    ranks preserved, budget and dropped items recorded in `metadata`.
    """
    return await context.build(
        current_message=payload.message,
        conversation_id=payload.conversation_id,
    )
