"""Stage 6M.1: the durable record of delivering one notification through one adapter.

One row per (notification, adapter), made unique by the database. It is what
makes "delivered once" survive a restart and hold across processes: 6J's
duplicate memory is per process and forgotten when the process ends; this
row is not.

It is not a notification and does not change one. `task_notifications` (6H)
stays the source of truth for *what happened*; this table records only *what
was sent where*. It holds no content, recipient, credential or error text --
an adapter name, a status, counters and timestamps.

    sending    -- claimed by one attempt, under a lease
    delivered  -- the adapter accepted it; terminal
    failed     -- the last attempt did not succeed; may be claimed again

A `sending` row whose lease has passed is claimable again: the attempt that
held it crashed or stalled. That window is at-least-once by design -- Telegram
has no idempotency key, so a crash after it accepted a message but before the
commit cannot be told apart from one before it.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow

#: Adapter names are lowercase identifiers of at most 32 characters (6I).
MAX_ADAPTER_CHARS = 32


class DeliveryRecordStatus(str, enum.Enum):
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"


def _enum_values(enum_cls) -> list:
    return [member.value for member in enum_cls]


class NotificationDelivery(Base):
    """Stage 6M.1. One notification, one adapter, one row."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("task_notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The notification's own owner, copied by the one writer. Never a parameter.
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    adapter: Mapped[str] = mapped_column(String(MAX_ADAPTER_CHARS), nullable=False)
    status: Mapped[DeliveryRecordStatus] = mapped_column(
        Enum(
            DeliveryRecordStatus, name="notification_delivery_status",
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    #: How many attempts have claimed this row. Also the fencing token: an
    #: attempt may only finish the row while the count is still its own.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Set while `sending`; past it, the claim is abandoned and may be taken.
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        #: The durable once-per-adapter guarantee.
        Index(
            "uq_notification_deliveries_notification_adapter",
            "notification_id", "adapter",
            unique=True,
        ),
        CheckConstraint("attempts >= 1", name="attempts_positive"),
        CheckConstraint(
            "(status = 'delivered') = (delivered_at IS NOT NULL)",
            name="delivered_iff_delivered_at",
        ),
        CheckConstraint(
            "status <> 'sending' OR lease_expires_at IS NOT NULL",
            name="sending_has_lease",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NotificationDelivery {self.adapter} {self.status.value}>"


__all__ = [
    "DeliveryRecordStatus",
    "MAX_ADAPTER_CHARS",
    "NotificationDelivery",
]
