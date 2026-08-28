"""Message request/response schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models.message import MessageRole


class MessageCreate(BaseModel):
    """Body of POST /api/conversations/{id}/messages."""

    content: str = Field(
        ...,
        min_length=1,
        max_length=32000,
        description="The user's message text.",
    )

    @field_validator("content")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """min_length=1 still admits "   ", which is not a real message."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("Message content cannot be blank.")
        return stripped

    model_config = ConfigDict(
        json_schema_extra={"example": {"content": "Hello Mai, what can you do?"}}
    )


class MessageRead(BaseModel):
    """A stored message."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatResponse(BaseModel):
    """Result of sending a message: what was stored, and what Mai replied."""

    conversation_id: uuid.UUID
    user_message: MessageRead
    assistant_message: MessageRead
