"""Memory schemas.

`MemoryCandidate` is the trust boundary: it is what the LLM proposes, and
nothing reaches the database without passing it. Enum membership, score
ranges and content sanity are all enforced here rather than downstream.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.memory.models import MemoryStatus, MemoryType

# Content shorter than this is not a usable standalone statement.
MIN_CONTENT_LENGTH = 8
MAX_CONTENT_LENGTH = 1000


class MemoryCandidate(BaseModel):
    """One memory proposed by the model, before validation and storage."""

    # Unknown fields from the model are dropped rather than accepted.
    model_config = ConfigDict(extra="ignore")

    content: str = Field(..., min_length=MIN_CONTENT_LENGTH, max_length=MAX_CONTENT_LENGTH)
    memory_type: MemoryType
    importance_score: int = Field(..., ge=1, le=10)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    source_message_id: Optional[uuid.UUID] = None

    @field_validator("content")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if len(collapsed) < MIN_CONTENT_LENGTH:
            raise ValueError("Memory content is too short to be meaningful.")
        return collapsed

    @field_validator("source_message_id", mode="before")
    @classmethod
    def _blank_uuid_is_none(cls, value):
        """Models often emit "", "null" or a placeholder instead of omitting."""
        if value in (None, "", "null", "none", "None", "..."):
            return None
        return value


class MemoryExtractionResult(BaseModel):
    """The full structured payload returned by the extraction prompt."""

    model_config = ConfigDict(extra="ignore")

    should_store_memory: bool = False
    memories: List[MemoryCandidate] = Field(default_factory=list)

    @field_validator("memories")
    @classmethod
    def _cap_batch(cls, value: List[MemoryCandidate]) -> List[MemoryCandidate]:
        """One turn cannot reasonably justify many memories.

        A long list is a sign the model is over-extracting, which is exactly
        what Stage 2A is meant to avoid.
        """
        return value[:5]


# --- API schemas ------------------------------------------------------------


class MemoryRead(BaseModel):
    """A stored memory, as returned by the inspection API."""

    id: uuid.UUID
    content: str
    memory_type: MemoryType
    status: MemoryStatus
    importance_score: int
    confidence_score: float
    source_conversation_id: uuid.UUID
    source_message_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemoryList(BaseModel):
    items: List[MemoryRead]
    total: int
