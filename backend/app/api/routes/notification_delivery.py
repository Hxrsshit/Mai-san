"""The owner's task notifications over HTTP: read them (6M.2), and ask for
one to be delivered (6L).

    GET  /api/task-notifications?limit=&before=&unread_only=     6M.2
    GET  /api/task-notifications/{notification_id}                6M.2
    POST /api/task-notifications/{notification_id}/read  {}       6M.2
    POST /api/task-notifications/{notification_id}/deliveries     6L
         {"adapter": "telegram"}

### Reading (Stage 6M.2)

A thin adapter over 6H's `NotificationService`, which owns every query, and
over `DeliveryStatusReader`, which reads 6M.1's delivery records without
writing them. Both are bound in `deps.py` to the request's owner
(`get_owner_id`): no route takes an owner, and another owner's notification is
`notification_not_found`, exactly like a missing one. A list is newest first,
at most `MAX_LISTED`, keyset-paged by `before` (the last id of the previous
page). Marking read delegates to 6H's conditional `mark_read`; it is idempotent
and writes nothing else. A notification carries no text, so neither does a
response; delivery state is per attempted channel, with no lease, record id,
delivery key, recipient, credential or error text.

### Delivering (Stage 6L)

"Deliver this notification through this channel", and nothing more. The
route does HTTP only: it hands the id and the adapter name to the 6I
`NotificationDeliveryService` built by the 6K composition root, and turns the
6I result into a status code. Everything that matters is decided there:

- **Owner.** The service's owner is the authenticated owner (`get_owner_id`,
  Stage 6M.4), passed explicitly; never a request value. Another owner's
  notification is refused exactly like a missing one, by 6I's owner-scoped
  read.
- **Channel.** The name is looked up in the one sealed process registry.
  Unknown and unconfigured channels are the same answer: nothing is
  registered under that name. A name is data; it never resolves to code.
- **Message and destination.** The request carries neither. 6J builds the
  text from the notification and sends it to the one configured chat.
  The body accepts exactly one field, so a token, chat id, URL, owner or
  text in it is a validation error, not an ignored extra.

### What the delivery route does not do

It creates no notification, marks nothing read, changes no task, retries
nothing and schedules nothing. (Since Stage 6M.1, 6I records the delivery
durably; the route itself writes nothing.) One request, one 6I attempt.
Nothing calls this route but a person's client: no model output, chat turn,
runner or background loop constructs a request to it.

The body must be JSON. FastAPI refuses to read a JSON body sent under another
content type, and a JSON content type forces a CORS preflight, so a page on
another origin cannot trigger a delivery with a plain form post.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DeliveryStatuses, NotificationDeliveries, TaskNotifications
from app.delivery.contract import DeliveryOutcome, DeliveryResult
from app.delivery.status import DeliveryState
from app.tasks.models import TaskNotificationKind
from app.tasks.notifications import MAX_LISTED

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
    # Stage 6M.1: another attempt holds a live durable claim right now.
    "delivery_in_progress": status.HTTP_409_CONFLICT,
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


# ============================================================================
# Stage 6M.2: reading
# ============================================================================

#: A page when the client does not ask for a size.
DEFAULT_LISTED = 20


class TaskNotificationRead(BaseModel):
    """One notification as the API reports it. References and times only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: uuid.UUID
    task_id: uuid.UUID
    kind: TaskNotificationKind
    check_number: int
    execution_id: Optional[uuid.UUID] = None
    created_at: datetime
    read_at: Optional[datetime] = None
    deliveries: List[DeliveryState]


class TaskNotificationList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    notifications: List[TaskNotificationRead]
    #: Pass as `before` for the next page; None on the last page.
    next_before: Optional[uuid.UUID] = None


class MarkReadRequest(BaseModel):
    """An empty JSON object. Required, so that only a JSON request -- which a
    cross-site form cannot send without a preflight -- can mark anything."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _read(notification, deliveries: List[DeliveryState]) -> TaskNotificationRead:
    return TaskNotificationRead(
        id=notification.id,
        task_id=notification.task_id,
        kind=notification.kind,
        check_number=notification.check_number,
        execution_id=notification.execution_id,
        created_at=notification.created_at,
        read_at=notification.read_at,
        deliveries=deliveries,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="notification_not_found")


@router.get("", response_model=TaskNotificationList)
async def list_task_notifications(
    notifications: TaskNotifications,
    deliveries: DeliveryStatuses,
    limit: int = Query(default=DEFAULT_LISTED, ge=1, le=MAX_LISTED),
    before: Optional[uuid.UUID] = Query(default=None),
    unread_only: bool = Query(default=False),
) -> TaskNotificationList:
    """The owner's notifications, newest first, at most `limit`."""
    page = await notifications.page(limit=limit, before=before, unread_only=unread_only)
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_cursor"
        )
    states = await deliveries.for_notifications(n.id for n in page.notifications)
    return TaskNotificationList(
        notifications=[_read(n, states[n.id]) for n in page.notifications],
        next_before=page.next_before,
    )


@router.get("/{notification_id}", response_model=TaskNotificationRead)
async def get_task_notification(
    notification_id: uuid.UUID,
    notifications: TaskNotifications,
    deliveries: DeliveryStatuses,
) -> TaskNotificationRead:
    """One of the owner's notifications, with its delivery state."""
    notification = await notifications.get(notification_id)
    if notification is None:
        raise _not_found()
    states = await deliveries.for_notifications([notification.id])
    return _read(notification, states[notification.id])


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_task_notification_read(
    notification_id: uuid.UUID,
    request: MarkReadRequest,
    notifications: TaskNotifications,
) -> Response:
    """Mark one of the owner's notifications read. 204 whether this call
    marked it or it was already read; 404 if the owner has no such
    notification. Delivery is unaffected."""
    if not await notifications.mark_read(notification_id):
        if await notifications.get(notification_id) is None:
            raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
