"""Memory ORM model.

Memories are *derived* knowledge. Conversation messages remain the raw source
of truth; a memory is a structured, standalone statement extracted from them
and is always traceable back to its origin.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow


def _enum_values(enum_cls) -> list:
    """Store the lowercase values, not the Python member names."""
    return [member.value for member in enum_cls]


class MemoryType(str, enum.Enum):
    """The five Stage 2A memory categories. Deliberately closed."""

    SEMANTIC = "semantic"
    PREFERENCE = "preference"
    GOAL = "goal"
    DECISION = "decision"
    EPISODIC = "episodic"


class MemoryStatus(str, enum.Enum):
    """Lifecycle state. Stage 2A only ever writes ACTIVE.

    SUPERSEDED and ARCHIVED exist so the later memory-lifecycle stage has
    somewhere to go without another migration.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Normalised form of `content`, used for exact-duplicate lookup. Sized to
    # hold the full normalised text so the unique index below cannot collide
    # two different memories that share a truncated prefix.
    normalized_content: Mapped[str] = mapped_column(String(1000), nullable=False)

    memory_type: Mapped[MemoryType] = mapped_column(
        Enum(MemoryType, name="memory_type", values_callable=_enum_values),
        nullable=False,
    )
    status: Mapped[MemoryStatus] = mapped_column(
        Enum(MemoryStatus, name="memory_status", values_callable=_enum_values),
        nullable=False,
        default=MemoryStatus.ACTIVE,
    )

    importance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Provenance. The conversation is the root: deleting it removes the
    # memories derived from it, so a stored memory can always answer
    # "where did you learn this?".
    source_conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable: a memory may summarise a turn rather than one exact message.
    source_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        # Scores are validated in Pydantic before insert; these constraints
        # make the database the last line of defence against bad LLM output.
        CheckConstraint(
            "importance_score >= 1 AND importance_score <= 10",
            name="importance_score_range",
        ),
        CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name="confidence_score_range",
        ),
        # Listing is "active memories, newest first", optionally by type.
        Index("ix_memories_status_created_at", "status", "created_at"),
        Index("ix_memories_memory_type", "memory_type"),
        Index("ix_memories_source_conversation_id", "source_conversation_id"),
        # Deduplication looks up exact normalised matches -- and this index is
        # UNIQUE, so the database enforces it even when two extractions run
        # concurrently and neither sees the other's uncommitted insert.
        Index(
            "uq_memories_type_normalized_content",
            "memory_type",
            "normalized_content",
            unique=True,
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Memory {self.memory_type.value} {self.content[:40]!r}>"
