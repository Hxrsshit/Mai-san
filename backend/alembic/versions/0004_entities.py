"""Stage 2B: entities, aliases and memory links.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENTITY_TYPES = (
    "person", "organization", "company", "project", "product",
    "technology", "place", "concept", "event", "other",
)
ENTITY_STATUSES = ("active", "archived")


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        # Display form, case preserved.
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        # Matching form; UNIQUE below.
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column(
            "entity_type", sa.Enum(*ENTITY_TYPES, name="entity_type"), nullable=False
        ),
        sa.Column(
            "status", sa.Enum(*ENTITY_STATUSES, name="entity_status"), nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
    )
    # Resolution is by normalized name. UNIQUE so two concurrent extractions
    # cannot both create the same entity -- an application-level check cannot
    # see the other transaction's uncommitted insert.
    op.create_index(
        "uq_entities_normalized_name", "entities", ["normalized_name"], unique=True
    )
    op.create_index("ix_entities_entity_type", "entities", ["entity_type"])
    op.create_index(
        "ix_entities_status_created_at", "entities", ["status", "created_at"]
    )

    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("entity_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("normalized_alias", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"],
            name=op.f("fk_entity_aliases_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_aliases")),
    )
    # Globally unique: an alias pointing at two entities makes resolution
    # ambiguous, which is worse than having no alias.
    op.create_index(
        "uq_entity_aliases_normalized_alias",
        "entity_aliases", ["normalized_alias"], unique=True,
    )
    op.create_index("ix_entity_aliases_entity_id", "entity_aliases", ["entity_id"])

    op.create_table(
        "memory_entities",
        sa.Column("memory_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("entity_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("mention_text", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["memories.id"],
            name=op.f("fk_memory_entities_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entities.id"],
            name=op.f("fk_memory_entities_entity_id_entities"),
            ondelete="CASCADE",
        ),
        # Composite primary key: a duplicate link is impossible, so
        # re-extracting a memory cannot pile up links.
        sa.PrimaryKeyConstraint(
            "memory_id", "entity_id", name=op.f("pk_memory_entities")
        ),
    )
    op.create_index("ix_memory_entities_entity_id", "memory_entities", ["entity_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_entities_entity_id", table_name="memory_entities")
    op.drop_table("memory_entities")

    op.drop_index("ix_entity_aliases_entity_id", table_name="entity_aliases")
    op.drop_index("uq_entity_aliases_normalized_alias", table_name="entity_aliases")
    op.drop_table("entity_aliases")

    op.drop_index("ix_entities_status_created_at", table_name="entities")
    op.drop_index("ix_entities_entity_type", table_name="entities")
    op.drop_index("uq_entities_normalized_name", table_name="entities")
    op.drop_table("entities")

    # PostgreSQL keeps enum types after their tables are dropped.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="entity_status").drop(bind, checkfirst=True)
        sa.Enum(name="entity_type").drop(bind, checkfirst=True)
