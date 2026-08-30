"""Relationship ORM models.

Entities are individual things; relationships describe how they connect.
Every relationship is directional and must be traceable to the memories that
support it, which is why evidence lives in its own table rather than as a
single `source_memory_id` column: one relationship is often supported by
several memories.
"""

import enum
import uuid
from datetime import datetime
from typing import List

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, utcnow


def _enum_values(enum_cls) -> list:
    return [member.value for member in enum_cls]


class RelationshipType(str, enum.Enum):
    """A deliberately small controlled vocabulary.

    Unlimited relationship strings make the data unusable; a fixed set keeps
    it consistent. Synonyms are mapped into these by the normalizer.
    """

    USES = "USES"
    BUILDS = "BUILDS"
    WORKS_ON = "WORKS_ON"
    INTERESTED_IN = "INTERESTED_IN"
    PREFERS = "PREFERS"
    OWNS = "OWNS"
    PART_OF = "PART_OF"
    CREATED = "CREATED"
    FOUNDED = "FOUNDED"
    WORKS_WITH = "WORKS_WITH"
    RELATED_TO = "RELATED_TO"
    DEPENDS_ON = "DEPENDS_ON"
    LOCATED_IN = "LOCATED_IN"
    INVOLVED_IN = "INVOLVED_IN"
    HAS_GOAL = "HAS_GOAL"


class RelationshipStatus(str, enum.Enum):
    """Lifecycle state. Stage 2C only ever writes ACTIVE.

    SUPERSEDED and ARCHIVED exist so the later lifecycle stage can express a
    changed or retired relationship without another migration.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class Relationship(Base):
    __tablename__ = "relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Direction matters: source -- type --> target is not the same claim as
    # target -- type --> source.
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[RelationshipType] = mapped_column(
        Enum(RelationshipType, name="relationship_type", values_callable=_enum_values),
        nullable=False,
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )

    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[RelationshipStatus] = mapped_column(
        Enum(
            RelationshipStatus,
            name="relationship_status",
            values_callable=_enum_values,
        ),
        nullable=False,
        default=RelationshipStatus.ACTIVE,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    evidence: Mapped[List["RelationshipEvidence"]] = relationship(
        back_populates="relationship",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        # A relationship is identified by its triple plus its lifecycle state.
        # UNIQUE, so two concurrent extractions cannot both insert it -- an
        # application check cannot see the other transaction's uncommitted row.
        # Status is part of the key so a later stage can archive a claim and
        # record a new active one without colliding.
        Index(
            "uq_relationships_triple",
            "source_entity_id",
            "relationship_type",
            "target_entity_id",
            "status",
            unique=True,
        ),
        Index("ix_relationships_source_entity_id", "source_entity_id"),
        Index("ix_relationships_target_entity_id", "target_entity_id"),
        Index("ix_relationships_type", "relationship_type"),
        Index("ix_relationships_status_created_at", "status", "created_at"),
        # An entity relating to itself carries no information and is almost
        # always an extraction error.
        CheckConstraint(
            "source_entity_id <> target_entity_id", name="no_self_relationship"
        ),
        CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name="confidence_score_range",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Relationship {self.source_entity_id} "
            f"-{self.relationship_type.value}-> {self.target_entity_id}>"
        )


class RelationshipEvidence(Base):
    """A memory that supports a relationship.

    Several memories can support the same claim ("Mai uses PostgreSQL." and
    "I chose PostgreSQL as Mai's database."). They become two evidence rows on
    one relationship, not two relationships.
    """

    __tablename__ = "relationship_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    relationship_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("relationships.id", ondelete="CASCADE"),
        nullable=False,
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    relationship: Mapped["Relationship"] = relationship(back_populates="evidence")

    __table_args__ = (
        # One memory supports a relationship once.
        Index(
            "uq_relationship_evidence_pair",
            "relationship_id",
            "memory_id",
            unique=True,
        ),
        Index("ix_relationship_evidence_memory_id", "memory_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RelationshipEvidence rel={self.relationship_id} mem={self.memory_id}>"
