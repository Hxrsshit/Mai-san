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


#: Removes duplicates an earlier race produced, keeping the oldest row of each
#: (memory_type, normalized_content) group, so the unique index can be built.
#:
#: This replaced a `MIN(id)` formulation that had two faults:
#:
#: 1. **It did not run on PostgreSQL.** `id` is a UUID, and PostgreSQL has no
#:    `min(uuid)` aggregate -- `function min(uuid) does not exist`. SQLite is
#:    dynamically typed and applies its own ordering to whatever the value is
#:    stored as, so the same statement worked there and the fault stayed
#:    invisible until the first real PostgreSQL run.
#:
#: 2. **It did not keep the oldest row**, which is what the comment claimed.
#:    Memory ids are random UUIDv4, so the minimum id is an arbitrary member of
#:    the group, not its earliest. Ordering by `created_at` is what actually
#:    delivers the documented behaviour.
#:
#: `ROW_NUMBER()` is used rather than a `min()` over some other column because
#: it expresses the intent directly -- rank the rows, keep rank 1 -- and
#: because it needs no aggregate over a UUID. PostgreSQL *can* order UUIDs
#: (`uuid` has a btree operator class); it is only the `min` aggregate that is
#: missing, so no cast to text is required and none is used.
#:
#: `ORDER BY created_at, id`: `created_at` selects the oldest, and `id` breaks
#: ties so the outcome is deterministic when two rows share a timestamp.
#:
#: Portable as written. PostgreSQL has had window functions since 8.4 and
#: SQLite since 3.25 (2018); the project's floor is well past both, so no
#: dialect branch is needed.
#:
#: `IN` rather than the original `NOT IN`: a `NOT IN` against a subquery that
#: can yield NULL silently matches nothing, and this form has no such edge.
_DEDUPLICATE_MEMORIES = """
    DELETE FROM memories
    WHERE id IN (
        SELECT id
        FROM (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY memory_type, normalized_content
                    ORDER BY created_at ASC, id ASC
                ) AS row_number_in_group
            FROM memories
        ) ranked
        WHERE row_number_in_group > 1
    )
"""


def upgrade() -> None:
    op.execute(_DEDUPLICATE_MEMORIES)

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
