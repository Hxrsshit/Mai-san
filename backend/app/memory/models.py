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


class MemoryOrigin(str, enum.Enum):
    """Where a memory came from. Stage 5C.

    This is an *authority* distinction, not a bookkeeping one. A LIVE memory
    was derived from something the user said to Mai. An IMPORTED memory was
    derived from a historical archive that Mai did not witness, whose contents
    are untrusted data.

    The difference has one hard consequence, enforced in
    `app.knowledge.conflicts`: an IMPORTED memory may never supersede a LIVE
    one. History explains the present; it does not overrule it.
    """

    LIVE = "live"
    IMPORTED = "imported"


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

    origin: Mapped[MemoryOrigin] = mapped_column(
        Enum(MemoryOrigin, name="memory_origin", values_callable=_enum_values),
        nullable=False,
        default=MemoryOrigin.LIVE,
        server_default=MemoryOrigin.LIVE.value,
    )

    # When the statement was *made*, as distinct from when this row was
    # written. For a live memory the two are the same, and the migration
    # backfills it to `created_at` so nothing about existing behaviour moves.
    #
    # For an imported memory they are years apart, and the difference is the
    # whole point: recency in `app.knowledge.conflicts` is judged on this
    # column, so importing a 2023 archive today cannot make a 2023 opinion
    # look newer than something the user said last week.
    stated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    # Provenance. Exactly one root, enforced by a CHECK below.
    #
    # The conversation is the root for live memories: deleting it removes the
    # memories derived from it, so a stored memory can always answer
    # "where did you learn this?". Nullable since Stage 5C, because an
    # imported memory's root is an archived message instead -- imported
    # history deliberately does not live in `conversations`.
    source_conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The root for imported memories: the exact archived message the statement
    # was read from, so provenance survives even after extraction.
    source_imported_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("imported_messages.id", ondelete="CASCADE"),
        nullable=True,
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
        # Exactly one provenance root, matching the origin. Without this a
        # bug could write a memory with no traceable source at all, or an
        # "imported" memory pointing at a live conversation -- and the
        # conflict rules would then be reasoning about a lie.
        CheckConstraint(
            "(origin = 'live'"
            " AND source_conversation_id IS NOT NULL"
            " AND source_imported_message_id IS NULL)"
            " OR (origin = 'imported'"
            " AND source_imported_message_id IS NOT NULL"
            " AND source_conversation_id IS NULL)",
            name="provenance_matches_origin",
        ),
        Index("ix_memories_source_conversation_id", "source_conversation_id"),
        Index(
            "ix_memories_source_imported_message_id", "source_imported_message_id"
        ),
        # Recency queries in conflict detection order by this.
        Index("ix_memories_stated_at", "stated_at"),
        Index("ix_memories_origin", "origin"),
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
