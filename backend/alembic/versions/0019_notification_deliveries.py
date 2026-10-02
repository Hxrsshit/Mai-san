"""Stage 6M.1: durable notification delivery records.

One new table, `notification_deliveries`, and one new enum type for its
status. No existing column, constraint or table changes; `task_notifications`
is referenced, not altered.

* One row per (notification, adapter): `uq_notification_deliveries_notification_adapter`
  is the load-bearing constraint. It makes "delivered once per adapter" a
  property of the database, surviving restarts and holding across processes.
* `status` is `sending` (claimed under a lease), `delivered` (terminal) or
  `failed` (retryable). Check constraints tie `delivered` to `delivered_at`
  and `sending` to a lease, and keep `attempts` positive.
* Rows cascade with their notification.

**Downgrade** drops the table, its index and the
`notification_delivery_status` type (PostgreSQL keeps a type after its table
is gone, so it is dropped explicitly, as 6H does). Delivery records are lost
on downgrade; the notifications themselves are untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DELIVERY_STATUSES = ("sending", "delivered", "failed")


def upgrade() -> None:
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("notification_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*DELIVERY_STATUSES, name="notification_delivery_status"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["task_notifications.id"], ondelete="CASCADE",
            name=op.f("fk_notification_deliveries_notification_id_task_notifications"),
        ),
        sa.CheckConstraint(
            "attempts >= 1",
            name=op.f("ck_notification_deliveries_attempts_positive"),
        ),
        sa.CheckConstraint(
            "(status = 'delivered') = (delivered_at IS NOT NULL)",
            name=op.f("ck_notification_deliveries_delivered_iff_delivered_at"),
        ),
        sa.CheckConstraint(
            "status <> 'sending' OR lease_expires_at IS NOT NULL",
            name=op.f("ck_notification_deliveries_sending_has_lease"),
        ),
    )
    # The durable once-per-adapter guarantee.
    op.create_index(
        "uq_notification_deliveries_notification_adapter",
        "notification_deliveries", ["notification_id", "adapter"], unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_notification_deliveries_notification_adapter",
        table_name="notification_deliveries",
    )
    op.drop_table("notification_deliveries")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="notification_delivery_status").drop(bind, checkfirst=True)
