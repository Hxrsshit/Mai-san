"""Entity inspection endpoints.

A development and debugging surface: entity quality cannot be judged without
reading what was extracted, how it was classified, and what it links to.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Query, Response, status

from app.api.deps import Entities
from app.entities.models import EntityStatus, EntityType
from app.entities.schemas import (
    EntityAliasRead,
    EntityDetail,
    EntityList,
    EntityRead,
)
from app.memory.schemas import MemoryRead
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/entities", tags=["entities"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Entity not found"}}


@router.get("", response_model=EntityList, summary="List entities")
async def list_entities(
    entities: Entities,
    entity_type: Optional[EntityType] = Query(
        default=None, description="Filter by entity type."
    ),
    status_filter: Optional[EntityStatus] = Query(
        default=None, alias="status", description="Filter by lifecycle status."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EntityList:
    """Most recently created entities first."""
    items = await entities.list_entities(
        entity_type=entity_type, status=status_filter, limit=limit, offset=offset
    )
    total = await entities.count_entities(
        entity_type=entity_type, status=status_filter
    )
    return EntityList(
        items=[EntityRead.model_validate(item) for item in items], total=total
    )


@router.get(
    "/{entity_id}",
    response_model=EntityDetail,
    responses=NOT_FOUND,
    summary="Retrieve one entity with its aliases",
)
async def get_entity(entity_id: uuid.UUID, entities: Entities) -> EntityDetail:
    """Returns the entity, its aliases, and how many memories reference it.

    The linked memories themselves are not inlined -- use
    `/api/entities/{id}/memories` for those.
    """
    entity = await entities.get_entity(entity_id)
    memory_count = await entities.count_linked_memories(entity_id)

    return EntityDetail(
        **EntityRead.model_validate(entity).model_dump(),
        aliases=[EntityAliasRead.model_validate(a) for a in entity.aliases],
        memory_count=memory_count,
    )


@router.get(
    "/{entity_id}/memories",
    response_model=List[MemoryRead],
    responses=NOT_FOUND,
    summary="List memories that reference this entity",
)
async def get_entity_memories(
    entity_id: uuid.UUID,
    entities: Entities,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> List[MemoryRead]:
    # 404 for an unknown entity rather than an empty list.
    await entities.get_entity(entity_id)
    memories = await entities.get_linked_memories(
        entity_id, limit=limit, offset=offset
    )
    return [MemoryRead.model_validate(memory) for memory in memories]


@router.delete(
    "/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    summary="Delete an entity",
)
async def delete_entity(entity_id: uuid.UUID, entities: Entities) -> Response:
    """Permanent deletion, matching how memories and conversations behave.

    Removes the entity, its aliases and its memory links. **The memories
    themselves are untouched** -- only the links go.
    """
    await entities.delete_entity(entity_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
