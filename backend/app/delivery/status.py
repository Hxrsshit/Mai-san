"""Stage 6M.2: reading delivery state, never writing it.

The 6M.1 record (`notification_deliveries`) is written only by
`app.delivery.records`. This module only reads it, for the owner's
notification read API, and reports each attempted channel as:

    status         the stored value: sending | delivered | failed
    attempts       how many attempts have claimed the row
    last_attempt_at, delivered_at
    retryable      derived: `failed`, or `sending` past its lease (an
                   abandoned claim). A live `sending` is in progress, not
                   retryable; `delivered` never is.

It defines no state of its own -- `retryable` restates exactly the condition
under which `records.claim` would grant a new claim -- and it never exposes
the raw lease, the record id or the delivery key. Every query is filtered on
the owner it was constructed with, as well as on the notifications asked
about, so it cannot report another owner's channels.
"""

import uuid
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.delivery.models import DeliveryRecordStatus, NotificationDelivery


class DeliveryState(BaseModel):
    """One channel's delivery state for one notification, as the API reports it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    status: DeliveryRecordStatus
    attempts: int
    last_attempt_at: datetime
    delivered_at: Optional[datetime] = None
    retryable: bool


def _aware(moment: datetime) -> datetime:
    # SQLite returns naive datetimes for timezone-aware columns; they are UTC.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def is_retryable(record: NotificationDelivery, now: datetime) -> bool:
    if record.status is DeliveryRecordStatus.FAILED:
        return True
    if record.status is DeliveryRecordStatus.SENDING:
        return record.lease_expires_at is not None and _aware(record.lease_expires_at) < now
    return False


class DeliveryStatusReader:
    """The owner's delivery states, by notification. Reads only."""

    def __init__(self, session: AsyncSession, owner_id: uuid.UUID) -> None:
        self._session = session
        self._owner_id = owner_id

    async def for_notifications(
        self, notification_ids: Iterable[uuid.UUID], now: Optional[datetime] = None
    ) -> Dict[uuid.UUID, List[DeliveryState]]:
        """Every attempted channel for each id, by adapter name. A notification
        never attempted maps to an empty list."""
        ids = list(notification_ids)
        states: Dict[uuid.UUID, List[DeliveryState]] = {i: [] for i in ids}
        if not ids:
            return states
        moment = now if now is not None else datetime.now(timezone.utc)
        records = (await self._session.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.owner_id == self._owner_id,
                NotificationDelivery.notification_id.in_(ids),
            )
            .order_by(NotificationDelivery.notification_id, NotificationDelivery.adapter)
        )).all()
        for record in records:
            states[record.notification_id].append(DeliveryState(
                adapter=record.adapter,
                status=record.status,
                attempts=record.attempts,
                last_attempt_at=record.updated_at,
                delivered_at=record.delivered_at,
                retryable=is_retryable(record, moment),
            ))
        return states


__all__ = ["DeliveryState", "DeliveryStatusReader", "is_retryable"]
