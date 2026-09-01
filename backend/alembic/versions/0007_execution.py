"""Stage 4E: execution records and the append-only audit journal.

Two new tables. No existing table is altered and no row is modified.

Portability notes, learned from the PostgreSQL runtime fix:

- No aggregate over a UUID appears here. That was the fault in migration 0003,
  invisible on SQLite and fatal on PostgreSQL.
- `metadata` is `jsonb` on PostgreSQL and `json` on SQLite, chosen through a
  dialect variant rather than a blanket type, so the PostgreSQL column is the
  one that can be indexed and queried later.
- Enum types are created natively on PostgreSQL and dropped on downgrade,
  since PostgreSQL keeps them after their table is gone.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EXECUTION_STATES = (
    "proposed", "approved", "revoked", "expired", "rejected", "cancelled",
    "executing", "succeeded", "failed",
)
AUTHORIZATION_STATUSES = (
    "unknown_tool", "forbidden", "approval_required", "allowed",
)
RISK_LEVELS = ("low", "medium", "high", "critical")
EVENT_TYPES = (
    "proposed", "authorized", "approval_requested", "approved", "revoked",
    "expired", "execution_started", "execution_succeeded", "execution_failed",
    "refused", "rejected", "cancelled",
)

JSON_COLUMN = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("arguments", JSON_COLUMN, nullable=False),
        sa.Column(
            "authorization_status",
            sa.Enum(*AUTHORIZATION_STATUSES, name="execution_authorization_status"),
            nullable=False,
        ),
        sa.Column(
            "risk_level",
            sa.Enum(*RISK_LEVELS, name="execution_risk_level"),
            nullable=True,
        ),
        sa.Column(
            "state",
            sa.Enum(*EXECUTION_STATES, name="execution_state"),
            nullable=False,
        ),
        sa.Column("approved_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("result_summary", sa.String(length=500), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_executions"),
        # An approved execution must carry what it was approved against and
        # when that lapses. Without both, "is this still valid?" has no answer.
        sa.CheckConstraint(
            "state <> 'approved' OR "
            "(approved_fingerprint IS NOT NULL AND approved_at IS NOT NULL "
            " AND approval_expires_at IS NOT NULL)",
            name="ck_executions_approved_requires_fingerprint_and_expiry",
        ),
        sa.CheckConstraint(
            "state NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
            name="ck_executions_completed_requires_timestamp",
        ),
    )

    # The idempotency guarantee. A UNIQUE index is the only race-safe form:
    # two concurrent creates cannot see each other's uncommitted row, so an
    # application-level check would lose.
    op.create_index(
        "uq_executions_idempotency_key", "executions", ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_executions_state", "executions", ["state"])
    op.create_index("ix_executions_tool_name", "executions", ["tool_name"])
    op.create_index("ix_executions_created_at", "executions", ["created_at"])

    op.create_table(
        "execution_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("execution_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(*EVENT_TYPES, name="execution_event_type"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("metadata", JSON_COLUMN, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_events"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE",
            name="fk_execution_events_execution_id_executions",
        ),
    )
    op.create_index(
        "ix_execution_events_execution_id", "execution_events", ["execution_id"]
    )
    op.create_index(
        "ix_execution_events_occurred_at", "execution_events", ["occurred_at"]
    )
    # One sequence number per execution: the journal cannot be reordered by
    # inserting a duplicate position.
    op.create_index(
        "uq_execution_events_sequence", "execution_events",
        ["execution_id", "sequence"], unique=True,
    )


def downgrade() -> None:
    """Drop both tables.

    Destructive by nature: an execution journal is history, and dropping it
    loses that history. There is no way to preserve it while removing the
    tables that hold it, so the downgrade says so plainly rather than
    pretending otherwise.
    """
    op.drop_index("uq_execution_events_sequence", table_name="execution_events")
    op.drop_index("ix_execution_events_occurred_at", table_name="execution_events")
    op.drop_index("ix_execution_events_execution_id", table_name="execution_events")
    op.drop_table("execution_events")

    op.drop_index("ix_executions_created_at", table_name="executions")
    op.drop_index("ix_executions_tool_name", table_name="executions")
    op.drop_index("ix_executions_state", table_name="executions")
    op.drop_index("uq_executions_idempotency_key", table_name="executions")
    op.drop_table("executions")

    # PostgreSQL keeps enum types after the tables using them are dropped.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in (
            "execution_event_type", "execution_state",
            "execution_risk_level", "execution_authorization_status",
        ):
            sa.Enum(name=name).drop(bind, checkfirst=True)
