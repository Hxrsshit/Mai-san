"""Stage 2C relationship system.

Relationships describe how entities are connected, directionally, with every
claim traceable to the memories that support it.
"""

from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)
from app.relationships.schemas import (
    RelationshipCandidate,
    RelationshipList,
    RelationshipRead,
)
from app.relationships.service import (
    RelationshipNotFoundError,
    RelationshipService,
)

__all__ = [
    "Relationship",
    "RelationshipEvidence",
    "RelationshipStatus",
    "RelationshipType",
    "RelationshipCandidate",
    "RelationshipList",
    "RelationshipRead",
    "RelationshipNotFoundError",
    "RelationshipService",
]
