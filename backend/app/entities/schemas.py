"""Entity schemas.

`EntityCandidate` is the trust boundary. Nothing the model proposes reaches the
database without passing it: type membership, name sanity, alias sanity and
confidence range are all enforced here.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.entities.models import EntityStatus, EntityType
from app.entities.normalizer import (
    MAX_NAME_LENGTH,
    clean_display_name,
    is_valid_name,
    normalize_name,
)

MAX_DESCRIPTION_LENGTH = 300
MAX_ALIASES_PER_ENTITY = 5


class EntityCandidate(BaseModel):
    """One entity proposed by the model, before resolution and storage."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., max_length=MAX_NAME_LENGTH)
    entity_type: EntityType
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    aliases: List[str] = Field(default_factory=list)
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not is_valid_name(value):
            raise ValueError(f"{value!r} is not a usable entity name.")
        return clean_display_name(value)

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value):
        if value is None:
            return None
        text = " ".join(str(value).split())
        # A placeholder is worse than nothing.
        if not text or text.lower() in {"null", "none", "n/a", "unknown", "..."}:
            return None
        return text[:MAX_DESCRIPTION_LENGTH]

    @field_validator("aliases", mode="before")
    @classmethod
    def _coerce_aliases(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            return []
        return value

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: List[str], info) -> List[str]:
        """Keep only usable aliases that differ from the name itself.

        An alias identical to the canonical name adds nothing and would
        collide with it during resolution.
        """
        name_normalized = normalize_name(info.data.get("name", ""))

        kept: List[str] = []
        seen = {name_normalized}
        for raw in value:
            if not isinstance(raw, str) or not is_valid_name(raw):
                continue
            normalized = normalize_name(raw)
            if normalized in seen:
                continue
            seen.add(normalized)
            kept.append(clean_display_name(raw))
            if len(kept) >= MAX_ALIASES_PER_ENTITY:
                break
        return kept


class EntityExtractionResult(BaseModel):
    """The full structured payload returned by the extraction prompt."""

    model_config = ConfigDict(extra="ignore")

    entities: List[EntityCandidate] = Field(default_factory=list)


# --- API schemas ------------------------------------------------------------


class EntityAliasRead(BaseModel):
    alias: str
    normalized_alias: str

    model_config = ConfigDict(from_attributes=True)


class EntityRead(BaseModel):
    id: uuid.UUID
    canonical_name: str
    normalized_name: str
    entity_type: EntityType
    status: EntityStatus
    description: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EntityDetail(EntityRead):
    """Entity with its aliases and how many memories reference it."""

    aliases: List[EntityAliasRead] = Field(default_factory=list)
    memory_count: int = 0


class EntityList(BaseModel):
    items: List[EntityRead]
    total: int
