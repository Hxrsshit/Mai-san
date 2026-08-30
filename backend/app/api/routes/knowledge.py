"""Knowledge lifecycle debug endpoint.

Answers the question the lifecycle exists to make answerable: *why is this
memory historical, and what replaced it?*

Read-only and model-free. It makes no LLM call and mutates nothing -- the
lifecycle is written by the background pipeline alone, and inspecting it must
never be a way to change it.

Exposure is deliberately narrow. It returns lifecycle metadata plus the memory
text already available through `/api/memories`, and nothing else: no scores, no
provider configuration, no credentials, no system prompt.
"""

import uuid

from fastapi import APIRouter

from app.api.deps import Knowledge
from app.knowledge.schemas import MemoryLifecycle
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get(
    "/debug/{memory_id}",
    response_model=MemoryLifecycle,
    responses={404: {"model": ErrorResponse, "description": "Memory not found"}},
    summary="Explain one memory's lifecycle state",
)
async def debug_memory_lifecycle(
    memory_id: uuid.UUID, knowledge: Knowledge
) -> MemoryLifecycle:
    """Lifecycle status and every conflict link touching this memory.

    Three link lists, from the memory's own point of view:

    - `superseded_by` -- what replaced or contests it;
    - `supersedes` -- what it replaced;
    - `triggered` -- decisions its arrival caused about other knowledge.

    An `unresolved` link means two claims looked incompatible and nothing
    deterministic separated them. Both remain ACTIVE; the link records the
    uncertainty rather than resolving it.
    """
    return await knowledge.lifecycle_for_memory(memory_id)
