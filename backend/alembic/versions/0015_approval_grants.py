"""Stage 6E: standing approval grants.

One new table and one new event value. No existing table is altered.

`approval_grants` reuses the `execution_risk_level` enum rather than
declaring a second one -- there is one definition of risk in Mai and a
parallel type would be a second place for it to drift. On PostgreSQL that
means reaching for `postgresql.ENUM(..., create_type=False)` explicitly,
because the same argument on a generic `sa.Enum` is accepted and ignored; on
SQLite the variant is a `VARCHAR` with a CHECK and there is no shared type to
clash with.

`standing_grant_used` joins `task_event_type` the same way Stage 6D's three
did: `ALTER TYPE ... ADD VALUE IF NOT EXISTS` on PostgreSQL, a no-op
elsewhere, and no removal on downgrade -- dropping an enum value means
rewriting the type and every column using it, and that column is an
append-only audit journal.

Revocation is a timestamp, never a delete, so nothing here needs a cascade:
the row that records what was permitted is the audit.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RISK_LEVELS = ("low", "medium", "high", "critical")
NEW_EVENT_VALUE = "standing_grant_used"


def upgrade() -> None:
    bind = op.get_bind()

    # The risk type is declared by migration 0007 and must not be created a
    # second time. `create_type=False` is a *PostgreSQL* ENUM parameter: on a
    # generic `sa.Enum` it is accepted and silently ignored, so the first
    # version of this migration emitted `CREATE TYPE` anyway and failed on a
    # live database with `DuplicateObjectError`. SQLite has no shared type,
    # so the round-trip there passed and hid it -- found by live
    # verification, which is what live verification is for.
    if bind.dialect.name == "postgresql":
        risk = postgresql.ENUM(
            *RISK_LEVELS, name="execution_risk_level", create_type=False
        )
    else:
        risk = sa.Enum(*RISK_LEVELS, name="execution_risk_level")

    op.create_table(
        "approval_grants",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("risk_level", risk, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_grants")),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_approval_grants_expires_after_creation"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_reason IS NULL)"
            " OR (revoked_at IS NOT NULL)",
            name=op.f("ck_approval_grants_revoked_reason_needs_revocation"),
        ),
    )
    # Every lookup is "this owner, this capability, still valid".
    op.create_index(
        "ix_approval_grants_owner_capability",
        "approval_grants", ["owner_id", "capability"],
    )
    op.create_index(
        "ix_approval_grants_expires_at", "approval_grants", ["expires_at"]
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE task_event_type ADD VALUE IF NOT EXISTS "
            f"'{NEW_EVENT_VALUE}'"
        )


def downgrade() -> None:
    op.drop_index("ix_approval_grants_expires_at", table_name="approval_grants")
    op.drop_index(
        "ix_approval_grants_owner_capability", table_name="approval_grants"
    )
    op.drop_table("approval_grants")
    # `execution_risk_level` is not dropped: `executions` still uses it.
    # `standing_grant_used` is not removed from `task_event_type`: see the
    # module docstring.
