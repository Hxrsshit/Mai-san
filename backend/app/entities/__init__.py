"""Stage 2B entity system.

Entities are the identifiable things referenced inside memories: people,
projects, companies, technologies, places and named concepts. Memories remain
the source statements; entities add structure around what they mention.
"""

from app.entities.models import (
    Entity,
    EntityAlias,
    EntityStatus,
    EntityType,
    MemoryEntity,
)
from app.entities.schemas import (
    EntityCandidate,
    EntityDetail,
    EntityList,
    EntityRead,
)
from app.entities.service import EntityNotFoundError, EntityService

__all__ = [
    "Entity",
    "EntityAlias",
    "EntityStatus",
    "EntityType",
    "MemoryEntity",
    "EntityCandidate",
    "EntityDetail",
    "EntityList",
    "EntityRead",
    "EntityNotFoundError",
    "EntityService",
]
