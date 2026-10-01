"""The one adapter this stage ships: local, in-memory, no network.

It records the payloads it accepts, keyed by `delivery_key`, and reports a
second delivery of the same key as `DUPLICATE` -- the idempotency behaviour
every real channel adapter must provide. It sends nothing anywhere, imports
nothing that could, and holds nothing beyond the process.
"""

from typing import Dict, Tuple

from app.delivery.contract import DeliveryPayload, DeliveryStatus, NotificationAdapter


class LocalRecordingAdapter(NotificationAdapter):
    """Records accepted deliveries in memory. For tests and local checks."""

    name = "local"

    def __init__(self) -> None:
        self._delivered: Dict[str, DeliveryPayload] = {}

    async def deliver(self, payload: DeliveryPayload) -> DeliveryStatus:
        if payload.delivery_key in self._delivered:
            return DeliveryStatus.DUPLICATE
        self._delivered[payload.delivery_key] = payload
        return DeliveryStatus.DELIVERED

    @property
    def delivered(self) -> Tuple[DeliveryPayload, ...]:
        """Accepted payloads, in delivery order."""
        return tuple(self._delivered.values())


__all__ = ["LocalRecordingAdapter"]
