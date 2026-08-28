"""ORM models.

Imported as a package so Alembic's autogenerate sees every table on Base.metadata.
"""

from app.database.models.base import Base, TimestampMixin
from app.database.models.conversation import (
    DEFAULT_CONVERSATION_TITLE,
    Conversation,
)
from app.database.models.message import Message, MessageRole

__all__ = [
    "Base",
    "TimestampMixin",
    "Conversation",
    "DEFAULT_CONVERSATION_TITLE",
    "Message",
    "MessageRole",
]
