"""Memory inspection endpoints.

Primarily a development and debugging surface: memory quality is impossible
to judge without being able to read what was stored and why.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Query, Response, status

from app.api.deps import Memories
from app.memory.models import MemoryStatus, MemoryType
from app.memory.schemas import MemoryList, MemoryRead
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/memories", tags=["memories"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Memory not found"}}


@router.get("", response_model=MemoryList, summary="List stored memories")
async def list_memories(
    memories: Memories,
    memory_type: Optional[MemoryType] = Query(
        default=None, description="Filter by memory type."
    ),
    status_filter: Optional[MemoryStatus] = Query(
        default=None, alias="status", description="Filter by lifecycle status."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MemoryList:
    """Most recently created memories first."""
    items = await memories.list_memories(
        memory_type=memory_type, status=status_filter, limit=limit, offset=offset
    )
    total = await memories.count_memories(
        memory_type=memory_type, status=status_filter
    )
    return MemoryList(
        items=[MemoryRead.model_validate(item) for item in items], total=total
    )


@router.get(
    "/{memory_id}",
    response_model=MemoryRead,
    responses=NOT_FOUND,
    summary="Retrieve one memory",
)
async def get_memory(memory_id: uuid.UUID, memories: Memories) -> MemoryRead:
    memory = await memories.get_memory(memory_id)
    return MemoryRead.model_validate(memory)


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    summary="Delete a memory",
)
async def delete_memory(memory_id: uuid.UUID, memories: Memories) -> Response:
    """Permanent deletion. Soft deletion is a later-stage concern."""
    await memories.delete_memory(memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
