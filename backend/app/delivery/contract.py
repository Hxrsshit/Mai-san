"""The contract between Mai and a notification channel.

Three types and a protocol, and each is closed:

* `DeliveryPayload` -- everything an adapter is ever given. Six fields, all
  identifiers, a kind, a number and a timestamp. No objective, no condition,
  no tool output, no arguments, no owner, no credentials. A channel that
  wants words reads them from records it is separately allowed to read.
* `DeliveryStatus` -- what an adapter may report.
* `DeliveryResult` -- what the boundary reports to its caller.
* `NotificationAdapter` -- what a channel must implement.

`delivery_key` is the payload's idempotency identity. It is derived only from
the notification's own identity and the adapter's name, so delivering the
same notification through the same adapter always carries the same key, and
an adapter that dedupes on it delivers once. The boundary itself keeps no
delivery state: a durable once-per-adapter guarantee across restarts would
need persisted delivery records, which this stage deliberately does not add.
"""

import enum
import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tasks.models import TaskNotificationKind

#: Adapter names: lowercase, starting with a letter, short. A name is a key in
#: a registry, never something that resolves to code.
ADAPTER_NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: Longest reason code a result may carry. Codes are constants, not messages.
MAX_REASON_CHARS = 64


def delivery_key(notification_id: uuid.UUID, adapter_name: str) -> str:
    """The idempotency identity of one notification through one adapter."""
    return f"notification:{notification_id}:adapter:{adapter_name}"


class DeliveryPayload(BaseModel):
    """What an adapter receives. Frozen, closed, and free of content."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    notification_id: uuid.UUID
    task_id: uuid.UUID
    kind: TaskNotificationKind
    check_number: int = Field(ge=0)
    created_at: datetime
    delivery_key: str = Field(min_length=1, max_length=200)


class DeliveryStatus(str, enum.Enum):
    """What an adapter may report about one delivery."""

    #: The channel accepted it.
    DELIVERED = "delivered"
    #: The channel had already accepted this delivery key; nothing was sent.
    DUPLICATE = "duplicate"
    #: The channel could not accept it.
    FAILED = "failed"


class DeliveryOutcome(str, enum.Enum):
    """What the boundary reports. The adapter's status, or a refusal."""

    DELIVERED = "delivered"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    #: Nothing was handed to any adapter.
    REFUSED = "refused"


class DeliveryResult(BaseModel):
    """The boundary's answer. Identifiers and a reason code only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: DeliveryOutcome
    reason: Optional[str] = Field(default=None, max_length=MAX_REASON_CHARS)
    adapter: Optional[str] = None
    notification_id: Optional[uuid.UUID] = None
    delivery_key: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _reason_is_a_code(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError("a reason is a code, not a message")
        return value


class NotificationAdapter:
    """A delivery channel. Subclass and implement `deliver`.

    An adapter receives a `DeliveryPayload` and nothing else -- no session,
    no service, no settings object. It must treat `payload.delivery_key` as
    its idempotency key, and must not raise for an ordinary channel failure
    (return `DeliveryStatus.FAILED`); anything it does raise is contained by
    the boundary and reported as a failure.
    """

    #: The adapter's registry name. Must match `ADAPTER_NAME`.
    name: str = ""

    async def deliver(self, payload: DeliveryPayload) -> DeliveryStatus:
        raise NotImplementedError


__all__ = [
    "ADAPTER_NAME",
    "DeliveryOutcome",
    "DeliveryPayload",
    "DeliveryResult",
    "DeliveryStatus",
    "MAX_REASON_CHARS",
    "NotificationAdapter",
    "delivery_key",
]
