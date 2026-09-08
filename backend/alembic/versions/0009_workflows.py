"""Stage 4F-E: workflows, and the link from an execution to its step.

One new table plus two columns on `executions`. No existing row is modified.

Steps are not stored here. A step is an `Execution`, so the approval
fingerprint, the atomic claim and the append-only journal already cover it --
duplicating that into a `workflow_steps` table would mean two implementations
of the same guarantee, and eventually two behaviours.

Portability notes, unchanged from earlier migrations:

- `plan` is `jsonb` on PostgreSQL and `json` on SQLite, via a dialect variant.
- `batch_alter_table` for the `executions` change, so one migration serves
  both dialects: SQLite cannot ALTER a foreign key in and rebuilds the table.
  The existing CHECK constraints are passed through `table_args` rather than
  re-created, and the difference matters. A SQLite rebuild preserves only what
  it is told to, so they must be described; PostgreSQL performs no rebuild, so
  issuing CREATE for constraints that already exist fails outright -- which is
  precisely what a first run against real PostgreSQL showed. `table_args` is
  consulted only when a rebuild happens, so one spelling is correct on both.
- The enum type is created natively on PostgreSQL and dropped on downgrade,
  since PostgreSQL keeps it after its table is gone.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORKFLOW_STATES = (
    "pending", "awaiting_approval", "approved", "running",
    "succeeded", "failed", "cancelled", "expired",
)

JSON_COLUMN = sa.JSON().with_variant(JSONB(), "postgresql")

#: Described for the SQLite rebuild, ignored by PostgreSQL. See the docstring.
def _execution_checks():
    return (
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


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*WORKFLOW_STATES, name="workflow_state"),
            nullable=False,
        ),
        sa.Column("plan", JSON_COLUMN, nullable=False),
        sa.Column("approved_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflows"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL",
            name="fk_workflows_conversation_id_conversations",
        ),
        sa.CheckConstraint(
            "state <> 'approved' OR "
            "(approved_fingerprint IS NOT NULL AND approved_at IS NOT NULL "
            " AND approval_expires_at IS NOT NULL)",
            name="ck_workflows_approved_requires_fingerprint_and_expiry",
        ),
        sa.CheckConstraint(
            "state NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
            name="ck_workflows_completed_requires_timestamp",
        ),
    )
    op.create_index("ix_workflows_conversation_id", "workflows", ["conversation_id"])
    op.create_index("ix_workflows_state", "workflows", ["state"])
    op.create_index("ix_workflows_created_at", "workflows", ["created_at"])

    with op.batch_alter_table(
        "executions", schema=None, table_args=_execution_checks()
    ) as batch:
        batch.add_column(sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=True))
        batch.add_column(sa.Column("step_index", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_executions_workflow_id_workflows",
            "workflows", ["workflow_id"], ["id"], ondelete="SET NULL",
        )

    op.create_index("ix_executions_workflow_id", "executions", ["workflow_id"])


def downgrade() -> None:
    """Drop the link, then the table.

    Order matters: the foreign key must go before the table it references.
    """
    op.drop_index("ix_executions_workflow_id", table_name="executions")

    with op.batch_alter_table(
        "executions", schema=None, table_args=_execution_checks()
    ) as batch:
        batch.drop_constraint("fk_executions_workflow_id_workflows", type_="foreignkey")
        batch.drop_column("step_index")
        batch.drop_column("workflow_id")

    op.drop_index("ix_workflows_created_at", table_name="workflows")
    op.drop_index("ix_workflows_state", table_name="workflows")
    op.drop_index("ix_workflows_conversation_id", table_name="workflows")
    op.drop_table("workflows")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="workflow_state").drop(bind, checkfirst=True)
