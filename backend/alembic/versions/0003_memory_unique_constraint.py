"""Stage 2A audit: enforce memory uniqueness in the database.

Deduplication ran entirely in application code, so two extractions running
concurrently could each query before either committed, see no duplicate, and
both insert the same memory. Observed failing roughly half the time under
concurrent turns in one conversation.

The database now enforces the invariant the application already asserts:
an active memory is unique by (memory_type, normalized_content).

`normalized_content` is widened to the full content length first, so the
unique index cannot collide two genuinely different long memories that happen
to share a truncated prefix.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove any duplicates an earlier race already produced, keeping the
    # oldest of each group, so the unique index can be created.
    op.execute(
        """
        DELETE FROM memories
        WHERE id NOT IN (
            SELECT MIN(id) FROM (SELECT id, memory_type, normalized_content
                                 FROM memories) AS m
            GROUP BY memory_type, normalized_content
        )
        """
    )

    with op.batch_alter_table("memories") as batch:
        batch.alter_column(
            "normalized_content",
            existing_type=sa.String(length=500),
            type_=sa.String(length=1000),
            existing_nullable=False,
        )

    op.drop_index("ix_memories_normalized_content", table_name="memories")
    op.create_index(
        "uq_memories_type_normalized_content",
        "memories",
        ["memory_type", "normalized_content"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_memories_type_normalized_content", table_name="memories")
    op.create_index(
        "ix_memories_normalized_content", "memories", ["normalized_content"]
    )
    with op.batch_alter_table("memories") as batch:
        batch.alter_column(
            "normalized_content",
            existing_type=sa.String(length=1000),
            type_=sa.String(length=500),
            existing_nullable=False,
        )
