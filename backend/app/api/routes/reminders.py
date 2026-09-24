"""Stage 5F.1: reading and managing reminders.

Read and cancel only. There is deliberately no create endpoint: a reminder is
created through conversation, where the schedule is parsed deterministically,
shown back, and confirmed. An HTTP create would be a second way to make one,
with a second place to get the timezone and the confirmation right.

Nothing here interprets reminder text. It is returned as stored and rendered
by the client as text.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AppSettings, Reminders
from app.core.logging import get_logger
from app.reminders.schemas import (
    NotificationList,
    NotificationRead,
    ReminderList,
    ReminderRead,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


@router.get("", response_model=ReminderList)
async def list_reminders(
    service: Reminders, limit: int = Query(default=20, ge=1, le=100)
) -> ReminderList:
    """Scheduled reminders, soonest first."""
    reminders = await service.active(limit=limit)
    return ReminderList(
        reminders=[ReminderRead.model_validate(r, from_attributes=True) for r in reminders],
        total=len(reminders),
    )


@router.post("/{reminder_id}/cancel", response_model=ReminderRead)
async def cancel_reminder(reminder_id: uuid.UUID, service: Reminders) -> ReminderRead:
    """Cancel one reminder by id.

    By id, so this path cannot be ambiguous the way a phrase can. The
    conversational path does the matching and refuses when several reminders
    fit; this one is already specific.
    """
    from app.reminders.models import Reminder

    reminder = await service._session.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="reminder_not_found"
        )

    result = await service._cancel(reminder)
    if not result.outcome.value == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=result.outcome.value
        )
    refreshed = await service._session.get(Reminder, reminder_id)
    return ReminderRead.model_validate(refreshed, from_attributes=True)


@router.get("/notifications", response_model=NotificationList)
async def list_notifications(
    service: Reminders, limit: int = Query(default=20, ge=1, le=100)
) -> NotificationList:
    """Fired reminders the user has not seen yet.

    This is the notification transport for v1: an in-app inbox the client
    polls. Deliberately local -- no email, no push, no third party -- so the
    first thing proven is `scheduler -> Mai -> user`, with nothing leaving
    the machine.
    """
    notifications, total = await service.pending_notifications(limit=limit)
    return NotificationList(
        notifications=[
            NotificationRead.model_validate(n, from_attributes=True)
            for n in notifications
        ],
        total=total,
    )


@router.post("/notifications/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_notification_read(
    notification_id: uuid.UUID, service: Reminders
) -> None:
    """Mark one delivered reminder as seen."""
    if not await service.mark_read(notification_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="notification_not_found"
        )


__all__ = ["router"]
