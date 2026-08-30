"""Relationship inspection endpoints.

A development and debugging surface: relationship quality cannot be judged
without reading what was extracted, in which direction, and on what evidence.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Query, Response, status

from app.api.deps import Entities, Relationships
from app.relationships.models import RelationshipStatus, RelationshipType
from app.relationships.schemas import (
    EntityRef,
    EntityRelationships,
    RelationshipEvidenceRead,
    RelationshipList,
    RelationshipRead,
)
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/relationships", tags=["relationships"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Relationship not found"}}


async def _render(relationships, relationship) -> RelationshipRead:
    """Build the read model, resolving both ends and the evidence count."""
    source = await relationships.get_entity_ref(relationship.source_entity_id)
    target = await relationships.get_entity_ref(relationship.target_entity_id)
    return RelationshipRead(
        id=relationship.id,
        source_entity=source,
        relationship_type=relationship.relationship_type,
        target_entity=target,
        confidence_score=relationship.confidence_score,
        status=relationship.status,
        evidence_count=await relationships.count_evidence(relationship.id),
        created_at=relationship.created_at,
        updated_at=relationship.updated_at,
    )


@router.get("", response_model=RelationshipList, summary="List relationships")
async def list_relationships(
    relationships: Relationships,
    relationship_type: Optional[RelationshipType] = Query(
        default=None, description="Filter by relationship type."
    ),
    source_entity_id: Optional[uuid.UUID] = Query(
        default=None, description="Only relationships originating at this entity."
    ),
    target_entity_id: Optional[uuid.UUID] = Query(
        default=None, description="Only relationships pointing at this entity."
    ),
    status_filter: Optional[RelationshipStatus] = Query(
        default=None, alias="status", description="Filter by lifecycle status."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RelationshipList:
    """Most recently created relationships first."""
    items = await relationships.list_relationships(
        relationship_type=relationship_type,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    total = await relationships.count_relationships(
        relationship_type=relationship_type,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        status=status_filter,
    )
    return RelationshipList(
        items=[await _render(relationships, item) for item in items], total=total
    )


@router.get(
    "/{relationship_id}",
    response_model=RelationshipRead,
    responses=NOT_FOUND,
    summary="Retrieve one relationship",
)
async def get_relationship(
    relationship_id: uuid.UUID, relationships: Relationships
) -> RelationshipRead:
    relationship = await relationships.get_relationship(relationship_id)
    return await _render(relationships, relationship)


@router.get(
    "/{relationship_id}/evidence",
    response_model=List[RelationshipEvidenceRead],
    responses=NOT_FOUND,
    summary="Memories supporting this relationship",
)
async def get_relationship_evidence(
    relationship_id: uuid.UUID,
    relationships: Relationships,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> List[RelationshipEvidenceRead]:
    """The memories that justify the claim -- not the conversations."""
    await relationships.get_relationship(relationship_id)
    rows = await relationships.get_evidence_memories(
        relationship_id, limit=limit, offset=offset
    )
    return [
        RelationshipEvidenceRead(
            memory_id=memory.id,
            content=memory.content,
            memory_type=memory.memory_type.value,
            created_at=memory.created_at,
            linked_at=evidence.created_at,
        )
        for memory, evidence in rows
    ]


@router.delete(
    "/{relationship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    summary="Delete a relationship",
)
async def delete_relationship(
    relationship_id: uuid.UUID, relationships: Relationships
) -> Response:
    """Removes the relationship and its evidence rows.

    **Entities and memories are untouched** -- only the claim linking them.
    """
    await relationships.delete_relationship(relationship_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Mounted on the entity router path -------------------------------------

entity_router = APIRouter(prefix="/api/entities", tags=["relationships"])


@entity_router.get(
    "/{entity_id}/relationships",
    response_model=EntityRelationships,
    responses={404: {"model": ErrorResponse, "description": "Entity not found"}},
    summary="Relationships involving an entity",
)
async def get_entity_relationships(
    entity_id: uuid.UUID,
    relationships: Relationships,
    entities: Entities,
    status_filter: Optional[RelationshipStatus] = Query(
        default=None, alias="status"
    ),
) -> EntityRelationships:
    """Incoming and outgoing are returned separately, never merged.

    Direction is the whole point of a relationship, so the response keeps the
    two sides distinct rather than leaving the caller to infer them.
    """
    entity = await entities.get_entity(entity_id)
    outgoing, incoming = await relationships.get_entity_relationships(
        entity_id, status=status_filter
    )
    rendered_out = [await _render(relationships, r) for r in outgoing]
    rendered_in = [await _render(relationships, r) for r in incoming]

    return EntityRelationships(
        entity=EntityRef.model_validate(entity),
        outgoing=rendered_out,
        incoming=rendered_in,
        total=len(rendered_out) + len(rendered_in),
    )
