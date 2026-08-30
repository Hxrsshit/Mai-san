"""Knowledge lifecycle ORM model.

`Memory.status` and `Relationship.status` already carry ACTIVE / SUPERSEDED /
ARCHIVED from Stages 2A and 2C, and Stage 2D already retrieves only ACTIVE
rows. What was missing was not a state machine but an *explanation*: nothing
recorded why a row became historical, or what replaced it.

`KnowledgeConflict` is that record. One row per detected conflict, linking the
older item to the newer one, with the resolution and the rule that produced it.

Why a link table rather than a fourth status value
--------------------------------------------------

The obvious alternative is a CONFLICTED status on both enums. It was rejected:

- It answers "is this contested?" but not "by what?", so traceability would
  need a second table anyway.
- Adding a value to a PostgreSQL enum needs ALTER TYPE ... ADD VALUE, which
  has transactional restrictions -- and the PostgreSQL runtime is not verified
  on this machine, so an untestable migration hazard is a poor trade.
- An unresolved conflict must leave *both* items retrievable. A status marking
  one of them as special already implies a winner, which is exactly the
  invented certainty the design is meant to avoid.

So an unresolved conflict is a link between two rows that both stay ACTIVE,
and a resolved one is a link plus a status change on the older row.

Polymorphism is avoided: memories and relationships get their own column
pairs with real foreign keys, because a conflict never crosses the two kinds.
That buys ON DELETE CASCADE, which is what keeps lifecycle links from being
orphaned when the knowledge they describe is deleted.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow


def _enum_values(enum_cls) -> list:
    return [member.value for member in enum_cls]


class ConflictResolution(str, enum.Enum):
    """What was decided about a detected conflict."""

    #: Deterministic evidence named a replacement. The older item's status is
    #: moved to SUPERSEDED; its content is never touched.
    SUPERSEDED = "superseded"

    #: Two items look incompatible but nothing deterministic picks a winner.
    #: **Both stay ACTIVE.** The link exists so the uncertainty is visible
    #: rather than silently resolved in one direction.
    UNRESOLVED = "unresolved"


class ConflictReason(str, enum.Enum):
    """Which rule fired. Recorded so a decision can be explained later."""

    #: The newer memory named both sides: "switched from X to Y".
    EXPLICIT_REPLACEMENT = "explicit_replacement"
    #: The newer memory named only what was dropped: "no longer uses X".
    EXPLICIT_ABANDONMENT = "explicit_abandonment"
    #: An exclusive relationship type gained a second target, and the newer
    #: memory stated the present ("now prefers hybrid").
    EXCLUSIVE_REPLACEMENT = "exclusive_replacement"
    #: An exclusive relationship type gained a second target with nothing to
    #: separate them. Always paired with UNRESOLVED.
    EXCLUSIVE_AMBIGUOUS = "exclusive_ambiguous"


class KnowledgeConflict(Base):
    """One detected conflict between two pieces of stored knowledge.

    The kind is decided by which `older_*` column is populated; the other
    kind's two columns must both be NULL. The matching `newer_*` column is
    optional, because knowledge can be retired without a named successor --
    "no longer uses OpenRouter" says what stopped being true and nothing about
    what replaced it. `triggering_memory_id` always records what caused the
    decision, so a successorless retirement is still traceable.
    """

    __tablename__ = "knowledge_conflicts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The item that lost, or is contested. Never modified in content.
    older_memory_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=True,
    )
    # What replaced or contests it.
    newer_memory_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=True,
    )
    older_relationship_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        nullable=True,
    )
    newer_relationship_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        nullable=True,
    )

    resolution: Mapped[ConflictResolution] = mapped_column(
        Enum(
            ConflictResolution,
            name="conflict_resolution",
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    reason: Mapped[ConflictReason] = mapped_column(
        Enum(ConflictReason, name="conflict_reason", values_callable=_enum_values),
        nullable=False,
    )

    #: The memory whose text triggered detection. Kept so the evidence for a
    #: lifecycle decision is as traceable as the knowledge itself.
    triggering_memory_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("memories.id", ondelete="SET NULL"),
        nullable=True,
    )

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        # The populated `older_*` column decides the kind, and a row may
        # only ever describe one kind. The matching `newer_*` column stays
        # nullable: "no longer uses X" retires knowledge without naming a
        # successor, and that is still a lifecycle event worth recording.
        CheckConstraint(
            "("
            " (older_memory_id IS NOT NULL"
            "  AND older_relationship_id IS NULL AND newer_relationship_id IS NULL)"
            " OR "
            " (older_relationship_id IS NOT NULL"
            "  AND older_memory_id IS NULL AND newer_memory_id IS NULL)"
            ")",
            name="exactly_one_conflict_kind",
        ),
        # Nothing supersedes itself. NULL comparisons yield UNKNOWN, which a
        # CHECK accepts, so this only constrains the populated pair.
        CheckConstraint(
            "older_memory_id IS NULL OR older_memory_id <> newer_memory_id",
            name="no_self_memory_conflict",
        ),
        CheckConstraint(
            "older_relationship_id IS NULL"
            " OR older_relationship_id <> newer_relationship_id",
            name="no_self_relationship_conflict",
        ),
        # One link per ordered pair. UNIQUE is the only race-safe way to say
        # this: two concurrent background evaluations cannot see each other's
        # uncommitted insert, so an application-level check loses the race.
        # NULLs compare as distinct, so relationship rows do not collide with
        # each other on the memory columns.
        Index(
            "uq_knowledge_conflicts_memory_pair",
            "older_memory_id",
            "newer_memory_id",
            unique=True,
        ),
        Index(
            "uq_knowledge_conflicts_relationship_pair",
            "older_relationship_id",
            "newer_relationship_id",
            unique=True,
        ),
        # Lookups are "what happened to this item?", in both directions.
        Index("ix_knowledge_conflicts_older_memory_id", "older_memory_id"),
        Index("ix_knowledge_conflicts_newer_memory_id", "newer_memory_id"),
        Index(
            "ix_knowledge_conflicts_older_relationship_id", "older_relationship_id"
        ),
        Index(
            "ix_knowledge_conflicts_newer_relationship_id", "newer_relationship_id"
        ),
        Index("ix_knowledge_conflicts_resolution", "resolution"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        if self.older_memory_id is not None:
            pair = f"memory {self.older_memory_id} -> {self.newer_memory_id}"
        else:
            pair = (
                f"relationship {self.older_relationship_id} "
                f"-> {self.newer_relationship_id}"
            )
        return f"<KnowledgeConflict {self.resolution.value} {pair}>"
