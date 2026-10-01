"""Stage 6H: notifications for monitoring outcomes.

One new table, `task_notifications`, one new enum type for its kind, and one
value on the `task_event_type` enum. No existing column, constraint or table
changes.

* `task_notifications` holds one row per monitoring outcome worth telling
  the owner about. It carries references and timestamps only -- no text.
* `uq_task_notifications_outcome` on `(task_id, kind, check_number)` is the
  load-bearing constraint: it makes "an outcome is notified at most once" a
  property of the database rather than of the code's care.
* `task_event_type` gains `notification_created`, added the Stage 6D-6G
  way: `ALTER TYPE ... ADD VALUE IF NOT EXISTS` on PostgreSQL, nothing on
  SQLite, and not removed on downgrade (the column is an append-only
  journal).

**Downgrade** drops the table, its indexes and the `task_notification_kind`
type. PostgreSQL keeps a type after its table is gone, so it is dropped
explicitly, as Stage 5F.1 does. Notification rows are lost on downgrade;
the task journal's `notification_created` entries remain as history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOTIFICATION_KINDS = ("condition_met", "monitoring_failed")


def upgrade() -> None:
    op.create_table(
        "task_notifications",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*NOTIFICATION_KINDS, name="task_notification_kind"),
            nullable=False,
        ),
        sa.Column("check_number", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_notifications")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="CASCADE",
            name=op.f("fk_task_notifications_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="SET NULL",
            name=op.f("fk_task_notifications_execution_id_executions"),
        ),
        sa.CheckConstraint(
            "check_number >= 0",
            name=op.f("ck_task_notifications_check_number_non_negative"),
        ),
        sa.CheckConstraint(
            "read_at IS NULL OR read_at >= created_at",
            name=op.f("ck_task_notifications_read_after_created"),
        ),
    )
    # The exactly-once guarantee.
    op.create_index(
        "uq_task_notifications_outcome",
        "task_notifications", ["task_id", "kind", "check_number"], unique=True,
    )
    # The owner's unread inbox.
    op.create_index(
        "ix_task_notifications_owner_id_read_at_created_at",
        "task_notifications", ["owner_id", "read_at", "created_at"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE task_event_type ADD VALUE IF NOT EXISTS 'notification_created'"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_task_notifications_owner_id_read_at_created_at",
        table_name="task_notifications",
    )
    op.drop_index("uq_task_notifications_outcome", table_name="task_notifications")
    op.drop_table("task_notifications")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="task_notification_kind").drop(bind, checkfirst=True)
    # `notification_created` stays on `task_event_type`: see the docstring.
