"""Stage 4F-D: link an execution to the conversation that proposed it.

One nullable column and one index. No existing row is modified, and no
existing behaviour depends on the column being set -- executions created
through the execution API leave it NULL, exactly as before.

`ON DELETE SET NULL`, not CASCADE. Deleting a conversation must not delete
the record that something was executed: the journal outlives the chat that
started it, and an audit trail that disappears when a user tidies up their
history is not an audit trail.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The CHECK constraints batch mode must carry across a table rebuild.
#:
#: SQLite cannot ALTER a constraint into an existing table, so batch mode
#: copies the table and moves it -- and a copy is only faithful if it is told
#: what to copy. Reflection does not reliably recover named CHECKs, and losing
#: one would silently drop the guarantee that an approved execution carries a
#: fingerprint. Restated here so the rebuilt table is the same table.
_CHECKS = (
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
    # Batch mode so one migration serves both dialects. On PostgreSQL it
    # emits ordinary ALTERs; on SQLite it rebuilds the table, which is the
    # only way to add a foreign key there. Writing two dialect-specific
    # branches instead would mean the schema the tests exercise is not the
    # schema production runs.
    with op.batch_alter_table("executions", table_kwargs={}) as batch:
        batch.add_column(
            sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_executions_conversation_id_conversations",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index(
        "ix_executions_conversation_id", "executions", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_executions_conversation_id", table_name="executions")
    with op.batch_alter_table("executions") as batch:
        batch.drop_constraint(
            "fk_executions_conversation_id_conversations", type_="foreignkey"
        )
        batch.drop_column("conversation_id")
