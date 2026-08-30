"""Relationship schemas.

`RelationshipCandidate` is the trust boundary: type membership, direction,
confidence range and non-self-reference are all enforced before anything
reaches resolution or storage.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.relationships.models import RelationshipStatus, RelationshipType
from app.relationships.normalizer import normalize_type

MAX_ENTITY_NAME_LENGTH = 200


class RelationshipCandidate(BaseModel):
    """One relationship proposed by the model, before entity resolution."""

    model_config = ConfigDict(extra="ignore")

    source_entity: str = Field(..., min_length=1, max_length=MAX_ENTITY_NAME_LENGTH)
    relationship_type: RelationshipType
    target_entity: str = Field(..., min_length=1, max_length=MAX_ENTITY_NAME_LENGTH)
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("relationship_type", mode="before")
    @classmethod
    def _normalize_type(cls, value):
        """Map a synonym phrasing onto the controlled vocabulary.

        An unmappable label is rejected rather than guessed at -- missing a
        relationship is preferable to recording the wrong one.
        """
        resolved = normalize_type(value if isinstance(value, str) else None)
        if resolved is None:
            raise ValueError(f"{value!r} is not a supported relationship type.")
        return resolved

    @field_validator("source_entity", "target_entity")
    @classmethod
    def _clean_entity_name(cls, value: str) -> str:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            raise ValueError("Entity name cannot be blank.")
        return cleaned

    @model_validator(mode="after")
    def _reject_self_reference(self) -> "RelationshipCandidate":
        """An entity related to itself carries no information.

        Compared case-insensitively, since the model may vary capitalisation
        between the two ends of the same name.
        """
        if self.source_entity.strip().lower() == self.target_entity.strip().lower():
            raise ValueError("A relationship cannot point an entity at itself.")
        return self


class RelationshipExtractionResult(BaseModel):
    """The full structured payload returned by the extraction prompt."""

    model_config = ConfigDict(extra="ignore")

    relationships: List[RelationshipCandidate] = Field(default_factory=list)


# --- API schemas ------------------------------------------------------------


class EntityRef(BaseModel):
    """Just enough of an entity to read a relationship."""

    id: uuid.UUID
    canonical_name: str
    entity_type: str

    model_config = ConfigDict(from_attributes=True)


class RelationshipRead(BaseModel):
    id: uuid.UUID
    source_entity: EntityRef
    relationship_type: RelationshipType
    target_entity: EntityRef
    confidence_score: float
    status: RelationshipStatus
    evidence_count: int = 0
    created_at: datetime
    updated_at: datetime


class RelationshipList(BaseModel):
    items: List[RelationshipRead]
    total: int


class RelationshipDirection(BaseModel):
    """A relationship as seen from one entity's point of view."""

    relationship: RelationshipRead
    direction: str  # "outgoing" | "incoming"


class EntityRelationships(BaseModel):
    entity: EntityRef
    outgoing: List[RelationshipRead] = Field(default_factory=list)
    incoming: List[RelationshipRead] = Field(default_factory=list)
    total: int = 0


class RelationshipEvidenceRead(BaseModel):
    """A memory supporting a relationship."""

    memory_id: uuid.UUID
    content: str
    memory_type: str
    created_at: datetime
    linked_at: Optional[datetime] = None
