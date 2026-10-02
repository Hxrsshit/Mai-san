"""Stage 6M.1: the one writer of delivery records.

Two operations, each its own short committed transaction, so that every other
process sees the result before anything is sent:

    claim(notification, adapter)
      no row                 -> insert `sending` under a lease      -> Claim
      row `delivered`        ->                                     ALREADY_DELIVERED
      row `failed`, or       -> conditional UPDATE to `sending`     -> Claim
        `sending` past lease    (the winner is whoever changes 1 row)
      otherwise              ->                                     IN_PROGRESS

    finish(claim, delivered)
      UPDATE to `delivered` / `failed`, only while the row is still `sending`
      and its attempt count is still this claim's (a fencing token: an
      attempt whose lease was taken over cannot overwrite the new one).

The database decides every race: the unique index on (notification, adapter)
stops two first claims, and `rowcount == 1` on a conditional UPDATE stops two
re-claims. Nothing here is held in memory.

### Transactions

Both operations commit the session they are given. That is deliberate, as in
`ExecutionService`: a claim that only became visible when the caller's request
finished would let a second process claim the same row and send again. A
caller must therefore hand 6I a session with no other pending work -- the 6L
route's request session holds nothing else.

### What this module never does

It never writes `task_notifications`, never marks anything read, never sends,
retries or schedules, and stores no content, recipient, credential or error
text.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional, Union

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.delivery.models import DeliveryRecordStatus, NotificationDelivery

#: How long one attempt may hold a claim, in seconds. Well beyond 6I's
#: 10-second adapter timeout, so only a crashed or stalled attempt loses it.
CLAIM_LEASE_SECONDS = 60

#: Another attempt already delivered this notification through this adapter.
ALREADY_DELIVERED = "already_delivered"
#: Another attempt holds a live claim; it may still succeed or fail.
IN_PROGRESS = "in_progress"


class Claim(NamedTuple):
    """The right to make one attempt. `attempt` is the fencing token."""

    record_id: uuid.UUID
    attempt: int


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


async def _record(
    session: AsyncSession, notification_id: uuid.UUID, adapter: str
) -> Optional[NotificationDelivery]:
    return (await session.execute(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.notification_id == notification_id,
            NotificationDelivery.adapter == adapter,
        )
        .execution_options(populate_existing=True)
    )).scalars().first()


async def claim(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    owner_id: uuid.UUID,
    adapter: str,
    now: Optional[datetime] = None,
) -> Union[Claim, str]:
    """Claim one attempt, or say why not. Commits before returning a Claim."""
    moment = _now(now)
    lease = moment + timedelta(seconds=CLAIM_LEASE_SECONDS)

    record = await _record(session, notification_id, adapter)
    if record is None:
        fresh = NotificationDelivery(
            notification_id=notification_id, owner_id=owner_id, adapter=adapter,
            status=DeliveryRecordStatus.SENDING, attempts=1,
            lease_expires_at=lease, created_at=moment, updated_at=moment,
        )
        session.add(fresh)
        try:
            await session.commit()
            return Claim(fresh.id, 1)
        except IntegrityError:
            # Another attempt inserted first. Its row decides.
            await session.rollback()
            record = await _record(session, notification_id, adapter)
            if record is None:
                return IN_PROGRESS

    if record.status is DeliveryRecordStatus.DELIVERED:
        return ALREADY_DELIVERED

    taken = await session.execute(
        update(NotificationDelivery)
        .where(and_(
            NotificationDelivery.id == record.id,
            NotificationDelivery.attempts == record.attempts,
            or_(
                NotificationDelivery.status == DeliveryRecordStatus.FAILED,
                and_(
                    NotificationDelivery.status == DeliveryRecordStatus.SENDING,
                    NotificationDelivery.lease_expires_at < moment,
                ),
            ),
        ))
        .values(
            status=DeliveryRecordStatus.SENDING, attempts=record.attempts + 1,
            lease_expires_at=lease, updated_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if taken.rowcount == 1:
        await session.commit()
        return Claim(record.id, record.attempts + 1)

    await session.rollback()
    record = await _record(session, notification_id, adapter)
    if record is not None and record.status is DeliveryRecordStatus.DELIVERED:
        return ALREADY_DELIVERED
    return IN_PROGRESS


async def finish(
    session: AsyncSession, held: Claim, *, delivered: bool, now: Optional[datetime] = None
) -> bool:
    """Record the attempt's outcome. True only if this claim still held the row."""
    moment = _now(now)
    values = {
        "status": DeliveryRecordStatus.DELIVERED if delivered else DeliveryRecordStatus.FAILED,
        "lease_expires_at": None,
        "updated_at": moment,
    }
    if delivered:
        values["delivered_at"] = moment
    result = await session.execute(
        update(NotificationDelivery)
        .where(and_(
            NotificationDelivery.id == held.record_id,
            NotificationDelivery.attempts == held.attempt,
            NotificationDelivery.status == DeliveryRecordStatus.SENDING,
        ))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


__all__ = [
    "ALREADY_DELIVERED",
    "CLAIM_LEASE_SECONDS",
    "Claim",
    "IN_PROGRESS",
    "claim",
    "finish",
]
