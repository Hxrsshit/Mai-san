"""Message request/response schemas."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.models.message import MessageRole
from app.intent.schemas import IntentRead
from app.orchestration.schemas import OrchestrationRead
from app.planning.schemas import PlanningRead


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

    #: Stage 4A. What Mai understood the user to be asking for.
    #:
    #: A sibling of the messages, not part of one: intent is application state
    #: about the turn, and keeping it in its own field is what stops it being
    #: mistaken for something the user or the model said. It is `None` when
    #: classification is disabled or unavailable.
    #:
    #: Nothing here authorises anything. `requires_execution` records that a
    #: future stage would have to arrange execution; Stage 4A has no executor.
    intent: Optional[IntentRead] = None

    #: Stage 4B. The plan, when the intent warranted one.
    #:
    #: A sibling of `intent`, for the same reason: application state about the
    #: turn, kept out of the messages so it cannot be mistaken for something
    #: the user or the model said. `None` when planning is disabled.
    #:
    #: Inert. A task reading "Send the outreach email" is a sentence about
    #: future work; nothing in this codebase can act on it.
    planning: Optional[PlanningRead] = None

    #: Stage 4D. What Mai identified as a possible action, and what the
    #: authorization layer decided about it.
    #:
    #: A third sibling of `intent` and `planning`, for the same reason.
    #:
    #: **Nothing here ran.** `executed` is false on the result and on every
    #: proposal, and there is no state in which it could be true: Stage 4D has
    #: no executor. An outcome of `action_allowed_not_executed` is named that
    #: way so a client cannot read permission as completion.
    orchestration: Optional[OrchestrationRead] = None
