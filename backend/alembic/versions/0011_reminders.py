"""Stage 5F.1: reminders and their delivered notifications.

Two new tables, no change to any existing one.

Portability notes, unchanged from earlier migrations:

- Enum types are created natively on PostgreSQL and dropped on downgrade,
  since PostgreSQL keeps a type after its table is gone.
- Timestamps are `DateTime(timezone=True)` throughout. A reminder is a claim
  about an instant, and a naive timestamp is not one.

The unique index on `(reminder_id, due_at)` is the load-bearing constraint:
it is what makes "an occurrence fires at most once" a property of the
database rather than of the scheduler's care.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REMINDER_STATES = ("scheduled", "completed", "cancelled", "failed")
RECURRENCES = ("once", "daily", "weekly")
NOTIFICATION_STATES = ("pending", "read")


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column(
            "state", sa.Enum(*REMINDER_STATES, name="reminder_state"), nullable=False
        ),
        sa.Column(
            "recurrence",
            sa.Enum(*RECURRENCES, name="reminder_recurrence"),
            nullable=False,
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("fire_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL",
            name=op.f("fk_reminders_conversation_id_conversations"),
        ),
        sa.CheckConstraint(
            "fire_count >= 0 AND failure_count >= 0",
            name=op.f("ck_reminders_counters_non_negative"),
        ),
        sa.CheckConstraint(
            "(state = 'cancelled' AND cancelled_at IS NOT NULL)"
            " OR (state <> 'cancelled' AND cancelled_at IS NULL)",
            name=op.f("ck_reminders_cancelled_requires_timestamp"),
        ),
    )
    op.create_index(
        "ix_reminders_state_next_run_at", "reminders", ["state", "next_run_at"]
    )
    op.create_index(
        "ix_reminders_conversation_id", "reminders", ["conversation_id"]
    )

    op.create_table(
        "reminder_notifications",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reminder_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "delivered_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(*NOTIFICATION_STATES, name="reminder_notification_state"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminder_notifications")),
        sa.ForeignKeyConstraint(
            ["reminder_id"], ["reminders.id"], ondelete="CASCADE",
            name=op.f("fk_reminder_notifications_reminder_id_reminders"),
        ),
    )
    # The occurrence-uniqueness guarantee. Everything else about double-firing
    # is defence in depth behind this.
    op.create_index(
        "uq_reminder_notifications_reminder_id_due_at",
        "reminder_notifications", ["reminder_id", "due_at"], unique=True,
    )
    op.create_index(
        "ix_reminder_notifications_state_delivered_at",
        "reminder_notifications", ["state", "delivered_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reminder_notifications_state_delivered_at",
        table_name="reminder_notifications",
    )
    op.drop_index(
        "uq_reminder_notifications_reminder_id_due_at",
        table_name="reminder_notifications",
    )
    op.drop_table("reminder_notifications")

    op.drop_index("ix_reminders_conversation_id", table_name="reminders")
    op.drop_index("ix_reminders_state_next_run_at", table_name="reminders")
    op.drop_table("reminders")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="reminder_notification_state").drop(bind, checkfirst=True)
        sa.Enum(name="reminder_recurrence").drop(bind, checkfirst=True)
        sa.Enum(name="reminder_state").drop(bind, checkfirst=True)
