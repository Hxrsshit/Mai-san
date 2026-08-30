"""Stage 2A: memories table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MEMORY_TYPES = ("semantic", "preference", "goal", "decision", "episodic")
MEMORY_STATUSES = ("active", "superseded", "archived")


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # Normalised copy of `content`, indexed for exact-duplicate lookup.
        sa.Column("normalized_content", sa.String(length=500), nullable=False),
        sa.Column(
            "memory_type",
            sa.Enum(*MEMORY_TYPES, name="memory_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*MEMORY_STATUSES, name="memory_status"),
            nullable=False,
        ),
        sa.Column("importance_score", sa.Integer(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("source_conversation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_message_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Last line of defence against out-of-range scores reaching storage.
        sa.CheckConstraint(
            "importance_score >= 1 AND importance_score <= 10",
            name=op.f("ck_memories_importance_score_range"),
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name=op.f("ck_memories_confidence_score_range"),
        ),
        # Deleting a conversation removes the memories derived from it, so a
        # stored memory can always be traced to its source.
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["conversations.id"],
            name=op.f("fk_memories_source_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            name=op.f("fk_memories_source_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
    )

    # Listing: active memories, newest first.
    op.create_index(
        "ix_memories_status_created_at", "memories", ["status", "created_at"]
    )
    # Filtering by type, and the per-type deduplication window.
    op.create_index("ix_memories_memory_type", "memories", ["memory_type"])
    op.create_index(
        "ix_memories_source_conversation_id", "memories", ["source_conversation_id"]
    )
    # Exact-duplicate lookup.
    op.create_index(
        "ix_memories_normalized_content", "memories", ["normalized_content"]
    )


def downgrade() -> None:
    op.drop_index("ix_memories_normalized_content", table_name="memories")
    op.drop_index("ix_memories_source_conversation_id", table_name="memories")
    op.drop_index("ix_memories_memory_type", table_name="memories")
    op.drop_index("ix_memories_status_created_at", table_name="memories")
    op.drop_table("memories")

    # PostgreSQL keeps enum types after their table is dropped.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="memory_status").drop(bind, checkfirst=True)
        sa.Enum(name="memory_type").drop(bind, checkfirst=True)
