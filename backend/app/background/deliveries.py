"""Stage 6M.3: automatic delivery of new task notifications.

The runtime's third work source, after reminders and tasks, in the same tick:

    tick
     |-- reminders
     |-- tasks         (may record a notification -- 6H, same transaction)
     '-- deliveries    <- this module, only if NOTIFICATION_AUTO_DELIVER_ADAPTERS
           for each configured, registered channel, while budget remains:
             candidates = notifications of the delivery owner,
                          created at or after the runtime's boundary,
                          never attempted through this channel     (read only)
             for each:   6K notification_delivery_service(...).deliver()
                         -> 6I owner-scoped read, durable claim, adapter

### Policy (owner decisions, 2026-10-06)

- **Opt-in.** Empty setting, nothing happens: no query, no composition.
- **Only new notifications.** The boundary is the moment this process's
  runtime was created with the setting on. Notifications are written by the
  same runtime, earlier in the same tick, so this skips only history -- and,
  after a crash mid-tick, a notification written but not yet delivered, which
  stays deliverable by a person (6L).
- **At most once, never retried.** A candidate has no delivery record of any
  status for that channel. A failure is recorded by 6I as `failed` and is
  never a candidate again; neither is a `sending` claim a crash left behind.
  6M.1's lease takeover is for explicit deliveries only.
- **Bounded.** At most `HARD_MAX_DELIVERIES_PER_TICK` attempts per tick, one
  at a time, each bounded by 6I's adapter timeout. Nothing is spawned.

### What decides a send

Not this module. The candidate query is only an optimisation: every send goes
through 6I's committed durable claim, so a restart, a second process or a
concurrent tick cannot send twice -- the loser is told `duplicate` or
`delivery_in_progress` and the adapter is not called.

### The owner

Background work has no request, so the request-scoped owner seam
(`app.api.deps.get_owner_id`) does not apply. This module delivers only for the
owner the 6K composition already serves -- the owner of the configured
channels, `LOCAL_OWNER_ID` today -- and never for anyone else: another owner's
notification would otherwise reach this owner's chat. Stage 6M.4 must make the
configured owner explicit.

It constructs no adapter or sender, writes no delivery record itself, and
decides nothing about whether a notification should exist.
"""

from datetime import datetime
from typing import NamedTuple

from app.composition.notification_delivery import (
    get_delivery_registry,
    notification_delivery_service,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.delivery.contract import DeliveryOutcome
from app.delivery.status import DeliveryStatusReader
from app.tasks.models import LOCAL_OWNER_ID

logger = get_logger(__name__)

#: Most automatic delivery attempts in one tick. With 6I's 10-second adapter
#: timeout, a tick is delayed by at most about 30 seconds even if every channel
#: hangs. A code constant, deliberately not a setting.
HARD_MAX_DELIVERIES_PER_TICK = 3

#: Whose notifications are delivered automatically: the owner of the channels
#: the composition serves. See "The owner" above.
DELIVERY_OWNER_ID = LOCAL_OWNER_ID


class DeliveryPass(NamedTuple):
    """What one automatic pass did. Counts only."""

    attempted: int = 0
    delivered: int = 0
    failed: int = 0


def usable_channels(settings: Settings) -> list:
    """The configured channels that are registered, in configured order."""
    configured = list(settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS or [])
    if not configured:
        return []
    registered = set(get_delivery_registry().names())
    return [name for name in configured if name in registered]


async def run_automatic_deliveries(
    session_factory, settings: Settings, since: datetime
) -> DeliveryPass:
    """One bounded pass. Never retries; every send through 6I's claim."""
    if not settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS:
        return DeliveryPass()
    budget = HARD_MAX_DELIVERIES_PER_TICK
    attempted = delivered = failed = 0
    for channel in usable_channels(settings):
        if budget <= 0:
            break
        async with session_factory() as session:
            candidates = await DeliveryStatusReader(
                session, DELIVERY_OWNER_ID
            ).awaiting_first_attempt(channel, since, budget)
        for notification_id in candidates:
            # Its own session: 6I commits the session it is given.
            async with session_factory() as session:
                result = await notification_delivery_service(
                    session, DELIVERY_OWNER_ID
                ).deliver(notification_id, channel)
            budget -= 1
            attempted += 1
            if result.outcome is DeliveryOutcome.DELIVERED:
                delivered += 1
            elif result.outcome is DeliveryOutcome.FAILED:
                failed += 1
    if attempted:
        logger.info(
            "Automatic notification delivery pass",
            extra={"attempted": attempted, "delivered": delivered, "failed": failed},
        )
    return DeliveryPass(attempted, delivered, failed)


__all__ = [
    "DELIVERY_OWNER_ID",
    "DeliveryPass",
    "HARD_MAX_DELIVERIES_PER_TICK",
    "run_automatic_deliveries",
    "usable_channels",
]
