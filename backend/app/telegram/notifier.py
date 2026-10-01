"""Stage 6J: Telegram as a notification delivery channel.

    6H notification -> 6I NotificationDeliveryService -> this adapter
                                                           |
                                              BotApiSender (the foundation)
                                                           |
                                              Telegram Bot API (one fixed host)

This is the whole of Telegram's part in notifications: turn one
`DeliveryPayload` into one short, deterministic text and hand it to the
Telegram sender that already exists. It creates no notification, decides
nothing about whether one should exist, changes no task, reads nothing, and
calls no model. It has no loop, no retry and no schedule: one call, one
attempt, and the caller decides what happens next.

### Where everything comes from

* **The destination** is `TELEGRAM_ALLOWED_CHAT_ID`, read once from
  settings. A payload carries no chat, recipient or channel field (the 6I
  contract is closed), so nothing about a notification can steer where it
  goes.
* **The credential** is `TELEGRAM_BOT_TOKEN`, read once from settings and
  handed to the existing `BotApiSender`. This module never stores it, never
  builds a URL with it, and never reads the environment itself.
* **The host, the method and the redirect rule** belong to the foundation's
  `NetworkPolicy` (`api.telegram.org`, POST only, redirects refused, and
  private, loopback and link-local addresses refused). This module adds no
  HTTP client of its own.

### Fail closed

Without a plausible token and a well-formed chat id the adapter is not
configured, and `deliver` returns `FAILED` without calling the sender, so an
unconfigured deployment cannot send a request to Telegram -- in particular
not one to a URL with an empty token.

### Message

Built only from the payload's own typed fields (a closed headline per kind,
the task id, the check number, a UTC time), so it carries no objective,
argument, result or owner, and no value in it can contain markup or text
chosen by anyone else. No `parse_mode` is sent. It is capped at Telegram's
limit by deterministic truncation, never by fetching anything.

### Idempotency, and its limit

The 6I payload carries a `delivery_key` derived from the notification and the
adapter. This adapter remembers the keys it delivered, in memory and bounded,
and answers a repeat with `DUPLICATE`. Only a successful send is remembered,
so a failed attempt can be retried by an explicit caller. That memory lives
and dies with the process: a durable once-per-notification guarantee across
restarts would need persisted delivery records, which no stage has added.

### Errors and logs

Whatever the sender raises becomes `FAILED`. The exception is never bound,
rendered or logged, because Telegram's API URL contains the bot token and
transport errors can carry that URL. Logs carry only a notification id, an
adapter name and a fixed reason code.
"""

import asyncio
import re
from collections import OrderedDict
from datetime import timezone
from typing import Dict, Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.delivery.contract import (
    DeliveryPayload,
    DeliveryStatus,
    NotificationAdapter,
    delivery_key,
)
from app.tasks.models import TaskNotificationKind
from app.telegram.client import TELEGRAM_MESSAGE_LIMIT, BotApiSender, TelegramSender

logger = get_logger(__name__)

#: Telegram chat ids are signed integers (negative for groups and channels).
_CHAT_ID = re.compile(r"-?[0-9]{1,20}")

#: What a bot token can plausibly look like. Deliberately a character class,
#: not a format: it exists to keep a malformed value from changing the shape
#: of the request path (`/`, `?`, `#`, whitespace), not to validate tokens.
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9:_-]{10,200}")

#: One line per notification kind. Closed: a new kind needs a new line, and a
#: test fails until it has one.
HEADLINES: Dict[TaskNotificationKind, str] = {
    TaskNotificationKind.CONDITION_MET: "Monitoring condition met.",
    TaskNotificationKind.MONITORING_FAILED: "Monitoring stopped after repeated failures.",
}

#: The most delivery keys remembered. Old ones are dropped first.
REMEMBERED_KEYS = 4096


def parse_chat_id(value) -> Optional[int]:
    """The configured chat id, or None if it is absent or malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _CHAT_ID.fullmatch(text):
        return None
    chat_id = int(text)
    return chat_id if chat_id != 0 else None


def token_is_plausible(value) -> bool:
    return isinstance(value, str) and bool(_TOKEN_CHARS.fullmatch(value.strip()))


def render_message(payload: DeliveryPayload) -> str:
    """The Telegram text for one payload. Deterministic, bounded, content-free."""
    created = payload.created_at
    if created.tzinfo is not None:
        created = created.astimezone(timezone.utc)
    lines = (
        "Mai notification",
        HEADLINES.get(payload.kind, "Monitoring update."),
        "Task: %s" % payload.task_id,
        "Check: %d" % payload.check_number,
        "Time: %s UTC" % created.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return "\n".join(lines)[:TELEGRAM_MESSAGE_LIMIT]


class TelegramNotificationAdapter(NotificationAdapter):
    """Delivers a notification payload to the one configured Telegram chat."""

    name = "telegram"

    def __init__(
        self, settings: Settings, sender: Optional[TelegramSender] = None
    ) -> None:
        self._chat_id = parse_chat_id(settings.TELEGRAM_ALLOWED_CHAT_ID)
        self._configured = (
            self._chat_id is not None
            and token_is_plausible(settings.TELEGRAM_BOT_TOKEN)
        )
        # The settings object is not kept: the token reaches the sender and
        # nothing else here ever holds it.
        self._sender = (
            sender if sender is not None
            else (BotApiSender(settings) if self._configured else None)
        )
        self._delivered: "OrderedDict[str, None]" = OrderedDict()
        # One send at a time, so a key is checked, sent and remembered as one
        # step and two racing calls for the same notification send once. Made
        # on first use, inside the running loop: on Python 3.9 a Lock binds to
        # the current event loop when it is constructed, and this adapter may
        # be built from synchronous code where there is none (and after any
        # `asyncio.run()` there is none to find).
        self._lock: Optional[asyncio.Lock] = None

    @property
    def configured(self) -> bool:
        return self._configured and self._sender is not None

    async def deliver(self, payload) -> DeliveryStatus:
        if not isinstance(payload, DeliveryPayload):
            return self._failed("invalid_payload", None)
        if not self.configured:
            return self._failed("telegram_not_configured", payload)
        if payload.delivery_key != delivery_key(payload.notification_id, self.name):
            # A key minted for another adapter is not this adapter's identity.
            return self._failed("delivery_key_mismatch", payload)

        if self._lock is None:
            # No `await` between this check and the assignment, so two first
            # calls cannot each create one.
            self._lock = asyncio.Lock()
        async with self._lock:
            if payload.delivery_key in self._delivered:
                return DeliveryStatus.DUPLICATE
            try:
                await self._sender.send_text(self._chat_id, render_message(payload))
            except Exception:  # noqa: BLE001 - see "Errors and logs" above
                return self._failed("telegram_send_failed", payload)
            self._remember(payload.delivery_key)
        return DeliveryStatus.DELIVERED

    def _remember(self, key: str) -> None:
        self._delivered[key] = None
        # One key is added per call, so one eviction restores the bound.
        if len(self._delivered) > REMEMBERED_KEYS:
            self._delivered.popitem(last=False)

    def _failed(self, reason: str, payload: Optional[DeliveryPayload]) -> DeliveryStatus:
        logger.warning(
            "Telegram notification not delivered",
            extra={
                "adapter": self.name,
                "reason": reason,
                "notification_id": str(payload.notification_id) if payload else "",
            },
        )
        return DeliveryStatus.FAILED


__all__ = [
    "HEADLINES",
    "REMEMBERED_KEYS",
    "TelegramNotificationAdapter",
    "parse_chat_id",
    "render_message",
    "token_is_plausible",
]
