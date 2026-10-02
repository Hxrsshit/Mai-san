"""Hand one existing notification to one registered adapter.

    deliver(notification_id, adapter_name)
      -> adapter registered?            else REFUSED unknown_adapter
      -> notification exists for owner? else REFUSED notification_not_found
      -> payload validates?             else REFUSED malformed_notification
      -> durable claim (Stage 6M.1)     already delivered -> DUPLICATE, unsent
                                        another attempt live -> REFUSED
                                                         delivery_in_progress
      -> adapter.deliver(payload), bounded by a timeout
      -> durable outcome recorded
      -> DELIVERED | DUPLICATE | FAILED

### Durable delivery (Stage 6M.1)

Whether a notification was delivered through an adapter is recorded in
`notification_deliveries`, through `app.delivery.records` -- the one writer of
that table. The claim is committed before the adapter is called, and the
outcome after, so a restart, a second process or a concurrent request sees it:
a delivered notification is answered `DUPLICATE` without reaching the adapter
again, and two attempts can never both send. A failed attempt is recorded as
`failed` and stays retryable. Both steps commit the session this service was
given (see `records`), so a caller hands it a session with no other pending
work.

A crash after the adapter accepted but before the outcome commits leaves a
`sending` row; once its lease passes, the next attempt may send again. That
window is at-least-once, chosen deliberately: Telegram has no idempotency key.

### What this does not do

It creates no notification and decides nothing about whether one should
exist -- `app.tasks.notifications.record_outcome` is the one writer. It never
writes the notification (delivery is not reading; `read_at` belongs to the
owner) or the task; its only writes are delivery records, through `records`.
It retries nothing and runs no loop: one call, at most one attempt. It asks no
authorization question, runs no tool and calls no model.

### Owner isolation

The notification is loaded through `NotificationService`, constructed with
this service's owner, so another owner's notification is indistinguishable
from a missing one. The caller passes an id, never a notification object, so
nothing outside the owner-scoped read can reach an adapter.

### Failure containment

A delivery failure is a result, never an exception. Whatever an adapter
raises is caught and reported as `failed` with a fixed reason code; the
exception's own text is never logged or returned, because a channel's error
can carry a token or an endpoint. The notification is untouched either way:
it stays the durable record whether or not any channel accepted it.
"""

import asyncio
import uuid
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.delivery.contract import (
    DeliveryOutcome,
    DeliveryPayload,
    DeliveryResult,
    DeliveryStatus,
    delivery_key,
)
from app.delivery import records
from app.delivery.registry import AdapterRegistry
from app.tasks.notifications import NotificationService

logger = get_logger(__name__)

#: The longest one adapter call may take, in seconds. A channel that hangs
#: must not hang its caller.
ADAPTER_TIMEOUT_SECONDS = 10.0


class NotificationDeliveryService:
    """Deliver an owner's existing notification through one adapter."""

    def __init__(
        self,
        session: AsyncSession,
        owner_id: uuid.UUID,
        registry: AdapterRegistry,
        timeout_seconds: float = ADAPTER_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session
        self._notifications = NotificationService(session, owner_id)
        self._registry = registry
        self._timeout = timeout_seconds

    async def deliver(self, notification_id, adapter_name) -> DeliveryResult:
        """At most one attempt. An adapter's failure is a result, never an
        exception; a database failure propagates, as it always has."""
        adapter = self._registry.get(adapter_name)
        if adapter is None:
            return self._refused("unknown_adapter")
        name = self._registry.canonical(adapter_name)

        if not isinstance(notification_id, uuid.UUID):
            return self._refused("malformed_notification", adapter=name)
        notification = await self._notifications.get(notification_id)
        if notification is None:
            # Missing and another owner's are the same answer on purpose.
            return self._refused("notification_not_found", adapter=name)

        key = delivery_key(notification.id, name)
        try:
            payload = DeliveryPayload(
                notification_id=notification.id,
                task_id=notification.task_id,
                kind=notification.kind,
                check_number=notification.check_number,
                created_at=notification.created_at,
                delivery_key=key,
            )
        except (ValidationError, AttributeError, TypeError):
            return self._refused(
                "malformed_notification", adapter=name, notification_id=notification_id
            )

        held = await records.claim(
            self._session, notification_id=notification.id,
            owner_id=notification.owner_id, adapter=name,
        )
        if held == records.ALREADY_DELIVERED:
            # Durably delivered before -- by this process or any other, before
            # or after a restart. The adapter is not asked again.
            logger.info(
                "Notification already delivered",
                extra={"notification_id": str(payload.notification_id), "adapter": name},
            )
            return DeliveryResult(
                outcome=DeliveryOutcome.DUPLICATE, adapter=name,
                notification_id=payload.notification_id, delivery_key=key,
            )
        if held == records.IN_PROGRESS:
            return self._refused(
                "delivery_in_progress", adapter=name, notification_id=payload.notification_id
            )

        try:
            status = await asyncio.wait_for(adapter.deliver(payload), self._timeout)
        except asyncio.TimeoutError:
            await records.finish(self._session, held, delivered=False)
            return self._failed("adapter_timeout", name, payload)
        except Exception:  # noqa: BLE001 - an adapter failure is a result
            await records.finish(self._session, held, delivered=False)
            return self._failed("adapter_error", name, payload)

        if not isinstance(status, DeliveryStatus):
            await records.finish(self._session, held, delivered=False)
            return self._failed("adapter_invalid_status", name, payload)

        # DUPLICATE from the adapter means it had already sent this key: that
        # is a delivery, so it is recorded as one.
        await records.finish(
            self._session, held, delivered=status is not DeliveryStatus.FAILED
        )

        logger.info(
            "Notification delivery attempted",
            extra={
                "notification_id": str(payload.notification_id),
                "adapter": name,
                "outcome": status.value,
            },
        )
        return DeliveryResult(
            outcome=DeliveryOutcome(status.value),
            adapter=name,
            notification_id=payload.notification_id,
            delivery_key=key,
        )

    @staticmethod
    def _refused(
        reason: str,
        adapter: Optional[str] = None,
        notification_id: Optional[uuid.UUID] = None,
    ) -> DeliveryResult:
        logger.info(
            "Notification delivery refused",
            extra={"reason": reason, "adapter": adapter or ""},
        )
        return DeliveryResult(
            outcome=DeliveryOutcome.REFUSED, reason=reason, adapter=adapter,
            notification_id=notification_id,
        )

    @staticmethod
    def _failed(reason: str, adapter: str, payload: DeliveryPayload) -> DeliveryResult:
        logger.warning(
            "Notification delivery failed",
            extra={
                "notification_id": str(payload.notification_id),
                "adapter": adapter,
                "reason": reason,
            },
        )
        return DeliveryResult(
            outcome=DeliveryOutcome.FAILED, reason=reason, adapter=adapter,
            notification_id=payload.notification_id,
            delivery_key=payload.delivery_key,
        )


__all__ = ["ADAPTER_TIMEOUT_SECONDS", "NotificationDeliveryService"]
