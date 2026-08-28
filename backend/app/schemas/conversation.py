"""Conversation request/response schemas."""

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.message import MessageRead


class ConversationCreate(BaseModel):
    """Body of POST /api/conversations. All fields optional."""

    title: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Optional title; a default is used when omitted.",
    )

    @field_validator("title")
    @classmethod
    def _normalise_title(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Title cannot be blank.")
        return stripped


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Title cannot be blank.")
        return stripped


class ConversationRead(BaseModel):
    """Conversation without its messages (used in list views)."""

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ConversationDetail(ConversationRead):
    """Conversation with its full message history."""

    messages: List[MessageRead] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ConversationList(BaseModel):
    """Paginated list of conversations."""

    items: List[ConversationRead]
    total: int
