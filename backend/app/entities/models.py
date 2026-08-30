"""Entity ORM models.

Memories are statements; entities are the identifiable things referenced
inside them. Entities never replace memories -- they add structure around what
the memories mention, joined through `memory_entities`.
"""

import enum
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, utcnow


def _enum_values(enum_cls) -> list:
    return [member.value for member in enum_cls]


class EntityType(str, enum.Enum):
    """Deliberately coarse. A granular taxonomy invites inconsistent labels."""

    PERSON = "person"
    ORGANIZATION = "organization"
    COMPANY = "company"
    PROJECT = "project"
    PRODUCT = "product"
    TECHNOLOGY = "technology"
    PLACE = "place"
    CONCEPT = "concept"
    EVENT = "event"
    OTHER = "other"


class EntityStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Display form, case preserved: "PostgreSQL", not "postgresql".
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Matching form. UNIQUE, so two extractions cannot create the same entity
    # twice even when they run concurrently and neither sees the other's
    # uncommitted insert.
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)

    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type", values_callable=_enum_values),
        nullable=False,
    )
    status: Mapped[EntityStatus] = mapped_column(
        Enum(EntityStatus, name="entity_status", values_callable=_enum_values),
        nullable=False,
        default=EntityStatus.ACTIVE,
    )

    # Optional and deliberately short. Only ever set from what a source memory
    # supports -- never enrichment.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    aliases: Mapped[List["EntityAlias"]] = relationship(
        back_populates="entity",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    __table_args__ = (
        # Resolution is by name, not (name, type): models classify the same
        # thing inconsistently ("Groq" as company or technology), and forking
        # an entity on every reclassification is the common failure. The first
        # classification wins.
        Index("uq_entities_normalized_name", "normalized_name", unique=True),
        Index("ix_entities_entity_type", "entity_type"),
        Index("ix_entities_status_created_at", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Entity {self.entity_type.value} {self.canonical_name!r}>"


class EntityAlias(Base):
    """An alternative surface form that resolves to one entity."""

    __tablename__ = "entity_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    entity: Mapped["Entity"] = relationship(back_populates="aliases")

    __table_args__ = (
        # Globally unique: an alias that points at two entities makes
        # resolution ambiguous, which is worse than having no alias at all.
        Index("uq_entity_aliases_normalized_alias", "normalized_alias", unique=True),
        Index("ix_entity_aliases_entity_id", "entity_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EntityAlias {self.alias!r} -> {self.entity_id}>"


class MemoryEntity(Base):
    """Join table: which entities a memory mentions.

    Many-to-many. The composite primary key makes a duplicate link impossible,
    so re-extracting the same memory cannot pile up links.
    """

    __tablename__ = "memory_entities"

    memory_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # The surface form as it appeared, e.g. "postgres" linking to "PostgreSQL".
    mention_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_memory_entities_entity_id", "entity_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MemoryEntity memory={self.memory_id} entity={self.entity_id}>"
