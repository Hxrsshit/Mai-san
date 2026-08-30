"""Stage 2C: relationships, evidence, and the seeded User entity.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-30
"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RELATIONSHIP_TYPES = (
    "USES", "BUILDS", "WORKS_ON", "INTERESTED_IN", "PREFERS", "OWNS",
    "PART_OF", "CREATED", "FOUNDED", "WORKS_WITH", "RELATED_TO",
    "DEPENDS_ON", "LOCATED_IN", "INVOLVED_IN", "HAS_GOAL",
)
RELATIONSHIP_STATUSES = ("active", "superseded", "archived")


def upgrade() -> None:
    op.create_table(
        "relationships",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_entity_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "relationship_type",
            sa.Enum(*RELATIONSHIP_TYPES, name="relationship_type"),
            nullable=False,
        ),
        sa.Column("target_entity_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*RELATIONSHIP_STATUSES, name="relationship_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        # An entity related to itself carries no information.
        sa.CheckConstraint(
            "source_entity_id <> target_entity_id",
            name=op.f("ck_relationships_no_self_relationship"),
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name=op.f("ck_relationships_confidence_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["source_entity_id"], ["entities.id"],
            name=op.f("fk_relationships_source_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_entity_id"], ["entities.id"],
            name=op.f("fk_relationships_target_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationships")),
    )
    # UNIQUE on the triple plus status: two concurrent extractions cannot both
    # insert the same claim, and a later stage can still archive one and
    # record a new active one.
    op.create_index(
        "uq_relationships_triple",
        "relationships",
        ["source_entity_id", "relationship_type", "target_entity_id", "status"],
        unique=True,
    )
    op.create_index(
        "ix_relationships_source_entity_id", "relationships", ["source_entity_id"]
    )
    op.create_index(
        "ix_relationships_target_entity_id", "relationships", ["target_entity_id"]
    )
    op.create_index("ix_relationships_type", "relationships", ["relationship_type"])
    op.create_index(
        "ix_relationships_status_created_at", "relationships", ["status", "created_at"]
    )

    op.create_table(
        "relationship_evidence",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("relationship_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("memory_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["relationship_id"], ["relationships.id"],
            name=op.f("fk_relationship_evidence_relationship_id_relationships"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["memories.id"],
            name=op.f("fk_relationship_evidence_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship_evidence")),
    )
    # One memory supports a relationship once.
    op.create_index(
        "uq_relationship_evidence_pair",
        "relationship_evidence",
        ["relationship_id", "memory_id"],
        unique=True,
    )
    op.create_index(
        "ix_relationship_evidence_memory_id", "relationship_evidence", ["memory_id"]
    )

    # --- Seed the implicit subject -----------------------------------------
    # Memories are written in the third person about "User", and Stage 2B
    # deliberately does not extract the user as an entity. Relationship types
    # like INTERESTED_IN, PREFERS and HAS_GOAL are meaningless without a node
    # for the user, so it is seeded here rather than created at runtime --
    # the relationship system must never create entities itself.
    connection = op.get_bind()
    already = connection.execute(
        sa.text("SELECT 1 FROM entities WHERE normalized_name = 'user'")
    ).first()
    if already is None:
        # Timestamps are supplied explicitly rather than left to the column
        # server_default: that default is `now()`, which PostgreSQL provides
        # but SQLite does not, and the ORM normally fills these in Python.
        now = datetime.now(timezone.utc)
        connection.execute(
            sa.text(
                """
                INSERT INTO entities (
                    id, canonical_name, normalized_name, entity_type,
                    status, description, created_at, updated_at
                ) VALUES (
                    :id, :canonical_name, :normalized_name, :entity_type,
                    :status, :description, :created_at, :updated_at
                )
                """
            ),
            {
                # Hex form so the value binds on both native-UUID and
                # CHAR(32) columns.
                "id": uuid.uuid4().hex,
                "canonical_name": "User",
                "normalized_name": "user",
                "entity_type": "person",
                "status": "active",
                "description": "The owner of this Mai instance.",
                "created_at": now,
                "updated_at": now,
            },
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM entities WHERE normalized_name = 'user'"))

    op.drop_index(
        "ix_relationship_evidence_memory_id", table_name="relationship_evidence"
    )
    op.drop_index("uq_relationship_evidence_pair", table_name="relationship_evidence")
    op.drop_table("relationship_evidence")

    op.drop_index("ix_relationships_status_created_at", table_name="relationships")
    op.drop_index("ix_relationships_type", table_name="relationships")
    op.drop_index("ix_relationships_target_entity_id", table_name="relationships")
    op.drop_index("ix_relationships_source_entity_id", table_name="relationships")
    op.drop_index("uq_relationships_triple", table_name="relationships")
    op.drop_table("relationships")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="relationship_status").drop(bind, checkfirst=True)
        sa.Enum(name="relationship_type").drop(bind, checkfirst=True)
