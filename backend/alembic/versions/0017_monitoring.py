"""Stage 6G: monitoring state on `tasks`.

Two columns on `tasks` and four values on the `task_event_type` enum. No table
is created, and no existing column changes.

* `tasks.monitor` -- the validated monitoring spec (condition and interval),
  JSONB on PostgreSQL, JSON on SQLite. NULL for every existing row is correct:
  no task was a monitor before this stage existed.
* `tasks.check_count` -- the number of the last claimed check. The runner
  claims check `n + 1` with a conditional UPDATE on `check_count = n`, so it
  is the exactly-once guard per check. NOT NULL, default 0, never negative.

The CHECK constraint is written with the column on SQLite, because SQLite
cannot add a constraint to an existing table without rebuilding it; on
PostgreSQL it is added the ordinary way. Either way it carries the name the
model's naming convention gives it, `ck_tasks_check_count_non_negative`.

On PostgreSQL that name is passed through `op.f`. The naming convention
prefixes `ck_<table>_` onto any name it is handed, so without `op.f` the
constraint came out as `ck_tasks_ck_tasks_check_count_non_negative` -- which
live PostgreSQL verification found and SQLite, whose DDL here is literal text,
could not.

Enum values follow the Stage 6D/6E/6F pattern: `ALTER TYPE ... ADD VALUE IF
NOT EXISTS` on PostgreSQL, nothing on SQLite, and no removal on downgrade --
the column is an append-only audit journal.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONColumn = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")

CHECK_NAME = "ck_tasks_check_count_non_negative"

NEW_EVENT_VALUES = (
    "monitoring_configured",
    "monitoring_check_started",
    "monitoring_triggered",
    "monitoring_check_failed",
)


def upgrade() -> None:
    op.add_column("tasks", sa.Column("monitor", JSONColumn, nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.add_column(
            "tasks",
            sa.Column(
                "check_count", sa.Integer(), server_default="0", nullable=False
            ),
        )
        op.create_check_constraint(op.f(CHECK_NAME), "tasks", "check_count >= 0")
        for value in NEW_EVENT_VALUES:
            op.execute(
                f"ALTER TYPE task_event_type ADD VALUE IF NOT EXISTS '{value}'"
            )
    else:
        op.execute(
            "ALTER TABLE tasks ADD COLUMN check_count INTEGER NOT NULL DEFAULT 0"
            f" CONSTRAINT {CHECK_NAME} CHECK (check_count >= 0)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(op.f(CHECK_NAME), "tasks", type_="check")
    op.drop_column("tasks", "check_count")
    op.drop_column("tasks", "monitor")
    # The four enum values are not removed: see the module docstring.
