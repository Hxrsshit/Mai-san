"""Stage 6F: task scheduling state for the one background runtime.

One nullable column and one index on `tasks`, and three values on the
`task_event_type` enum. No table is created, and no existing column changes.

`tasks.next_run_at` is the whole of a task's scheduling state -- the same idea
as `reminders.next_run_at`. NULL for every existing row is correct: nothing
was scheduled for background work before this stage existed.

Enum values follow the Stage 6D/6E pattern: `ALTER TYPE ... ADD VALUE IF NOT
EXISTS` on PostgreSQL, nothing on SQLite (which stores the column as VARCHAR),
and no removal on downgrade -- dropping an enum value means rewriting the type
and every column using it, and that column is an append-only audit journal.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_EVENT_VALUES = (
    "background_scheduled", "background_claimed", "background_unscheduled",
)


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The runtime's discovery query: due, advanceable, ordered by due time.
    op.create_index(
        "ix_tasks_state_next_run_at", "tasks", ["state", "next_run_at"]
    )

    if op.get_bind().dialect.name == "postgresql":
        for value in NEW_EVENT_VALUES:
            op.execute(
                f"ALTER TYPE task_event_type ADD VALUE IF NOT EXISTS '{value}'"
            )


def downgrade() -> None:
    op.drop_index("ix_tasks_state_next_run_at", table_name="tasks")
    op.drop_column("tasks", "next_run_at")
    # The three enum values are not removed: see the module docstring.
