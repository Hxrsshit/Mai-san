"""Stage 6K: the one process-lifetime notification delivery graph.

    NotificationDeliveryService (6I)      per call: it holds a DB session
            |
    AdapterRegistry (6I)                  ONE per process, sealed
            |
    TelegramNotificationAdapter (6J)      ONE per process, if configured
            |
    BotApiSender (Telegram foundation)    ONE per process, inside the adapter
            |
    api.telegram.org

### Why this exists

6J's duplicate protection is a memory inside the adapter instance: a
notification already delivered is answered with `DUPLICATE`. That only works
if the same adapter instance serves every delivery. An adapter built per call
would start with an empty memory each time and silently deliver everything
again. So the registry, and the adapter inside it, are built once and kept for
the life of the process.

`NotificationDeliveryService` is *not* process-lifetime and cannot be: 6I
gives it a database session and an owner, both per call. It is a thin object
around the shared registry, so building one per call loses nothing. Making it
long-lived would mean changing 6I's contract.

### What this does not do

It decides nothing about **when** a notification is delivered. Nothing in this
module, and nothing that imports it, calls `deliver`. There is no route, no
command, no worker, no schedule, no startup hook and no retry. A caller that
wants a delivery must ask for one explicitly, through
`notification_delivery_service`.

It defines no rule of its own. Ownership, refusals, the payload, timeouts and
failure containment are 6I's. Telegram's configuration check, message,
destination and duplicate memory are 6J's. Host, method and redirect policy
are the foundation's. This module only puts them together.

### The pattern it follows

`app.integrations.registry` builds the integration registry once, seals it and
hands it out through `get_integration_registry()`. Integration HTTP clients
are not closed in the application lifespan, and neither is this one. This is
the same shape, with one difference: integrations read settings lazily, but
the 6J adapter reads its two settings when constructed, so the graph is built
on first access rather than at import.

### Telegram registration

The 6J adapter is constructed from settings, and registered **only** if it
reports itself configured (a well-formed `TELEGRAM_ALLOWED_CHAT_ID` and a
plausible `TELEGRAM_BOT_TOKEN`). Otherwise nothing is registered, the
registry is sealed empty, and a request for `"telegram"` is refused by 6I as
`unknown_adapter`. No fallback destination, credential or channel exists.
Construction opens no connection: the sender's HTTP client is created on its
first request.

The in-memory `LocalRecordingAdapter` is deliberately **not** registered. It
exists for tests and keeps every payload it accepts for the life of the
process.
"""

import threading
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.delivery.registry import AdapterRegistry
from app.delivery.service import NotificationDeliveryService
from app.tasks.models import LOCAL_OWNER_ID
from app.telegram.notifier import TelegramNotificationAdapter

logger = get_logger(__name__)

_registry: Optional[AdapterRegistry] = None
#: Delivery services can be requested from the event loop or, through a sync
#: FastAPI dependency, from a worker thread. The lock makes "built once" true
#: for both.
_build_lock = threading.Lock()


def build_delivery_registry(settings: Settings) -> AdapterRegistry:
    """Build one sealed registry from settings. Opens no connection."""
    registry = AdapterRegistry()
    telegram = TelegramNotificationAdapter(settings)
    if telegram.configured:
        registry.register(telegram)
    registry.seal()
    logger.info(
        "Notification delivery composed",
        extra={"adapters": ",".join(registry.names()) or "none"},
    )
    return registry


def get_delivery_registry() -> AdapterRegistry:
    """The process's one delivery registry, built on first use."""
    global _registry
    if _registry is None:
        with _build_lock:
            if _registry is None:
                _registry = build_delivery_registry(get_settings())
    return _registry


def notification_delivery_service(
    session: AsyncSession, owner_id: uuid.UUID = LOCAL_OWNER_ID
) -> NotificationDeliveryService:
    """A 6I delivery service bound to the process's one registry.

    Per call, because the service holds this caller's session. The registry,
    and therefore every adapter and its duplicate memory, is shared.
    """
    return NotificationDeliveryService(session, owner_id, get_delivery_registry())


__all__ = [
    "build_delivery_registry",
    "get_delivery_registry",
    "notification_delivery_service",
]
