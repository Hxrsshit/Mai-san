"""Stage 6C: capability binding on task steps, and plan authorization.

Three columns, all additive, no existing column altered:

* `task_steps.capability` -- the registry's canonical name for what a step
  needs, written only when a plan is authorised. NULL means the step is prose.
* `task_steps.arguments` -- the validated arguments that capability would be
  called with.
* `tasks.authorized_at` -- when a plan became permission. NULL until it does.

`arguments` is created NOT NULL with a server default of an empty object, so
the rows Stage 6A and 6B already wrote get a valid value without a backfill
pass. `capability` and `authorized_at` are nullable because NULL is the
correct answer for an existing row: nothing was bound, and nothing was
authorised.

Portability notes, unchanged from earlier migrations: `sa.Uuid(as_uuid=True)`
is native on PostgreSQL and `CHAR(32)` elsewhere, and the JSON variant is
`JSONB` on PostgreSQL and `JSON` on SQLite, so the schema round-trips for the
test suite.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONColumn = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "task_steps", sa.Column("capability", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "task_steps",
        sa.Column(
            "arguments", JSONColumn, nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Every readiness query filters on it, and an authorised plan is the only
    # kind whose steps are ever looked up.
    op.create_index(
        "ix_task_steps_capability", "task_steps", ["capability"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_steps_capability", table_name="task_steps")
    op.drop_column("tasks", "authorized_at")
    op.drop_column("task_steps", "arguments")
    op.drop_column("task_steps", "capability")
