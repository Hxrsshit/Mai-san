"""Stage 6L: a person asks for one task notification to be delivered.

    POST /api/task-notifications/{notification_id}/deliveries
    {"adapter": "telegram"}

"Deliver this notification through this channel", and nothing more. The
route does HTTP only: it hands the id and the adapter name to the 6I
`NotificationDeliveryService` built by the 6K composition root, and turns the
6I result into a status code. Everything that matters is decided there:

- **Owner.** The service's owner is the composition's own default, never a
  request value. Another owner's notification is refused exactly like a
  missing one, by 6I's owner-scoped read.
- **Channel.** The name is looked up in the one sealed process registry.
  Unknown and unconfigured channels are the same answer: nothing is
  registered under that name. A name is data; it never resolves to code.
- **Message and destination.** The request carries neither. 6J builds the
  text from the notification and sends it to the one configured chat.
  The body accepts exactly one field, so a token, chat id, URL, owner or
  text in it is a validation error, not an ignored extra.

### What this does not do

It creates no notification, marks nothing read, changes no task, records no
delivery, retries nothing and schedules nothing. One request, one 6I attempt.
Nothing calls this route but a person's client: no model output, chat turn,
runner or background loop constructs a request to it.

The body must be JSON. FastAPI refuses to read a JSON body sent under another
content type, and a JSON content type forces a CORS preflight, so a page on
another origin cannot trigger a delivery with a plain form post.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import NotificationDeliveries
from app.delivery.contract import DeliveryOutcome, DeliveryResult

router = APIRouter(prefix="/api/task-notifications", tags=["notifications"])

#: The longest adapter name the request will carry. Registered names are at
#: most 32 characters; anything longer cannot name one.
MAX_ADAPTER_NAME_CHARS = 32

#: 6I refusal reason -> HTTP status. A table, so a new reason has to be given
#: a status deliberately; an unmapped refusal is still a refusal (409), never
#: a 500 that might suggest something was sent.
_REFUSED_STATUS = {
    "notification_not_found": status.HTTP_404_NOT_FOUND,
    "unknown_adapter": status.HTTP_404_NOT_FOUND,
    "malformed_notification": status.HTTP_422_UNPROCESSABLE_CONTENT,
}

#: The code returned when 6I gives no reason of its own: an adapter that
#: answered "failed" rather than raising. Never a message, never exception text.
FAILED_WITHOUT_REASON = "delivery_failed"
REFUSED_WITHOUT_REASON = "delivery_refused"


class DeliveryRequest(BaseModel):
    """The whole request body: which registered channel. Nothing else."""

    model_config = ConfigDict(extra="forbid", strict=True)

    adapter: str = Field(min_length=1, max_length=MAX_ADAPTER_NAME_CHARS)


@router.post("/{notification_id}/deliveries", response_model=DeliveryResult)
async def deliver_notification(
    notification_id: uuid.UUID,
    request: DeliveryRequest,
    service: NotificationDeliveries,
) -> DeliveryResult:
    """One delivery attempt. 200 when the channel accepted it (`delivered`)
    or already had it (`duplicate`); otherwise an error carrying 6I's reason
    code and nothing else."""
    result = await service.deliver(notification_id, request.adapter)
    if result.outcome is DeliveryOutcome.REFUSED:
        raise HTTPException(
            status_code=_REFUSED_STATUS.get(result.reason, status.HTTP_409_CONFLICT),
            detail=result.reason or REFUSED_WITHOUT_REASON,
        )
    if result.outcome is DeliveryOutcome.FAILED:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.reason or FAILED_WITHOUT_REASON,
        )
    return result


__all__ = ["router"]
