"""Stage 6A: tasks, their steps and their journal.

Three new tables, no change to any existing one. Nothing here executes:
this migration introduces the state a later stage's runner will read.

Portability notes, unchanged from earlier migrations:

* `sa.Uuid(as_uuid=True)` is native `uuid` on PostgreSQL and `CHAR(32)`
  elsewhere, so the schema round-trips on SQLite for the test suite.
* `sa.Enum(...)` creates a real enum type on PostgreSQL and a
  `VARCHAR + CHECK` on SQLite. The types are dropped explicitly on
  downgrade, because PostgreSQL does not remove them with the table.
* Constraint names are passed bare to `op.f()`, which applies the naming
  convention once. Pre-formatting them double-prefixes the result -- the
  mistake Stage 5C's downgrade made.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TASK_STATES = (
    "proposed", "planned", "awaiting_approval", "queued", "running",
    "paused", "blocked", "completed", "failed", "cancelled",
)
TASK_STEP_STATES = (
    "pending", "running", "completed", "failed", "skipped", "cancelled",
)
TASK_ORIGINS = ("user",)
TASK_PRIORITIES = ("low", "medium", "high")
TASK_EVENT_TYPES = (
    "task_created", "plan_attached", "assumption_recorded", "state_changed",
    "task_blocked", "task_cancelled", "task_failed", "approval_requested",
    "approval_granted", "step_started", "step_completed", "step_failed",
    "observation_recorded", "replanned", "budget_exceeded", "task_completed",
)

#: JSON on SQLite, JSONB on PostgreSQL -- the same declaration the models use.
JSONColumn = sa.JSON().with_variant(
    sa.dialects.postgresql.JSONB(), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "origin", sa.Enum(*TASK_ORIGINS, name="task_origin"), nullable=False
        ),
        sa.Column("objective", sa.String(length=2000), nullable=False),
        sa.Column(
            "state", sa.Enum(*TASK_STATES, name="task_state"), nullable=False
        ),
        sa.Column(
            "priority",
            sa.Enum(*TASK_PRIORITIES, name="task_priority"),
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan", JSONColumn, nullable=True),
        sa.Column("current_step", sa.String(length=40), nullable=True),
        sa.Column("budget", JSONColumn, nullable=False),
        sa.Column("spent", JSONColumn, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=4000), nullable=True),
        sa.Column(
            "failure_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL",
            name=op.f("fk_tasks_conversation_id_conversations"),
        ),
        sa.CheckConstraint(
            "(state = 'cancelled' AND cancelled_at IS NOT NULL)"
            " OR (state <> 'cancelled' AND cancelled_at IS NULL)",
            name=op.f("ck_tasks_cancelled_requires_timestamp"),
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL)"
            " OR (state <> 'completed' AND completed_at IS NULL)",
            name=op.f("ck_tasks_completed_requires_timestamp"),
        ),
        sa.CheckConstraint(
            "failure_count >= 0", name=op.f("ck_tasks_failure_count_non_negative")
        ),
    )
    # Every activity question is a query over (owner, state).
    op.create_index("ix_tasks_owner_id_state", "tasks", ["owner_id", "state"])
    op.create_index("ix_tasks_conversation_id", "tasks", ["conversation_id"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])

    op.create_table(
        "task_steps",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("step_key", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*TASK_STEP_STATES, name="task_step_state"),
            nullable=False,
        ),
        sa.Column("depends_on", JSONColumn, nullable=False),
        sa.Column("execution_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_steps")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE",
            name=op.f("fk_task_steps_task_id_tasks"),
        ),
        # A reference to the authoritative execution record, never a copy.
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="SET NULL",
            name=op.f("fk_task_steps_execution_id_executions"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_task_steps_sequence_is_positive")
        ),
    )
    op.create_index(
        "uq_task_steps_task_id_step_key", "task_steps",
        ["task_id", "step_key"], unique=True,
    )
    op.create_index(
        "uq_task_steps_task_id_sequence", "task_steps",
        ["task_id", "sequence"], unique=True,
    )

    op.create_table(
        "task_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(*TASK_EVENT_TYPES, name="task_event_type"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("metadata", JSONColumn, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_events")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE",
            name=op.f("fk_task_events_task_id_tasks"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_task_events_event_sequence_is_positive")
        ),
    )
    # The journal's total-order guarantee: two writers cannot both claim a
    # position. One loses the insert, which is the correct outcome.
    op.create_index(
        "uq_task_events_task_id_sequence", "task_events",
        ["task_id", "sequence"], unique=True,
    )
    op.create_index(
        "ix_task_events_task_id_occurred_at", "task_events",
        ["task_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_events_task_id_occurred_at", table_name="task_events")
    op.drop_index("uq_task_events_task_id_sequence", table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("uq_task_steps_task_id_sequence", table_name="task_steps")
    op.drop_index("uq_task_steps_task_id_step_key", table_name="task_steps")
    op.drop_table("task_steps")

    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_index("ix_tasks_conversation_id", table_name="tasks")
    op.drop_index("ix_tasks_owner_id_state", table_name="tasks")
    op.drop_table("tasks")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="task_event_type").drop(bind, checkfirst=True)
        sa.Enum(name="task_step_state").drop(bind, checkfirst=True)
        sa.Enum(name="task_priority").drop(bind, checkfirst=True)
        sa.Enum(name="task_state").drop(bind, checkfirst=True)
        sa.Enum(name="task_origin").drop(bind, checkfirst=True)
