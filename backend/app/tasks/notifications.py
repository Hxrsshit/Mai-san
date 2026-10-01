"""Stage 6H: notifications for monitoring outcomes.

A monitoring task (Stage 6G) ends in one of two ways worth telling a person
about: its condition held, or its checks kept failing and it was blocked.
This module turns each of those outcomes into one durable notification --
and does nothing else.

    BackgroundRuntime -> TaskRunner.check -> evaluate -> task transition
                                                              |
                                          record_outcome (same transaction)
                                                              |
                                          task_notifications + task journal

### What this module is not

It is not a delivery system. Nothing here sends anything anywhere: no
Telegram, no email, no push, no HTTP. A notification is a row the owner can
read, exactly like Stage 5F.1's reminder inbox. Channels that deliver it are
adapters, built later, that read through `NotificationService`.

It is not an engine. It has no loop, no scheduler, no claim of its own, and
it runs nothing: it never resolves a capability, asks an authorization
question, or creates an execution. Structural tests assert each.

### The two rules that make it exactly-once

1. **Same transaction as the outcome.** `record_outcome` is called inside the
   transaction that completes or blocks the task. A crash anywhere before
   the commit rolls the outcome and the notification back together, so the
   retried outcome produces it again -- once.
2. **A unique index names the outcome.** `(task_id, kind, check_number)`.
   Should the same outcome somehow be reached twice -- a replay, a race, a
   bug -- the database refuses the second row and this module treats that
   as "already notified". A person can resume a blocked task and it can fail
   again after further checks; that is a different check number, so a
   different outcome, so a new notification.

### Content

None. A notification stores a kind, references and timestamps. It holds no
text, so there is nothing for a model to read as instructions, nothing that
can name a channel or a recipient, and nothing to leak.
"""

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.tasks import events as journal
from app.tasks.models import TaskEventType, TaskNotification, TaskNotificationKind
from app.tasks.states import TaskState

logger = get_logger(__name__)

#: The only outcomes that produce a notification, and the task state each
#: one requires. A notification is written only once the task is already in
#: that state -- after the outcome, never in anticipation of it.
NOTIFIABLE_OUTCOMES: Dict[TaskNotificationKind, TaskState] = {
    TaskNotificationKind.CONDITION_MET: TaskState.COMPLETED,
    TaskNotificationKind.MONITORING_FAILED: TaskState.BLOCKED,
}

#: The most notifications one listing returns.
MAX_LISTED = 50


async def record_outcome(
    session: AsyncSession,
    task,
    kind: TaskNotificationKind,
    execution_id: Optional[uuid.UUID] = None,
) -> Optional[TaskNotification]:
    """Record one notification for a monitoring outcome that just happened.

    Returns the notification, or None when there is nothing to record: the
    task is not a monitoring task, is not in the state the outcome requires,
    or this outcome was already recorded. Never raises for those cases --
    the outcome itself has already happened and must not be undone by a
    notification that could not be written.

    The owner is the task's own. There is no parameter for it, nor for a
    channel, a recipient or any text.
    """
    if not isinstance(kind, TaskNotificationKind):
        return None
    if task is None or task.monitor is None:
        return None
    if NOTIFIABLE_OUTCOMES[kind] is not task.state:
        return None

    notification = TaskNotification(
        owner_id=task.owner_id,
        task_id=task.id,
        kind=kind,
        check_number=int(task.check_count or 0),
        execution_id=execution_id,
    )
    try:
        async with session.begin_nested():
            session.add(notification)
            await session.flush()
    except IntegrityError:
        # The unique index refused it: this outcome is already recorded.
        # The savepoint rolled back only the duplicate.
        logger.info(
            "Notification already recorded",
            extra={"task_id": str(task.id), "kind": kind.value},
        )
        return None

    await journal.record(
        session, task.id, TaskEventType.NOTIFICATION_CREATED, actor="system",
        metadata={
            "kind": kind.value,
            "notification_id": str(notification.id),
            "check": notification.check_number,
        },
    )
    logger.info(
        "Notification recorded",
        extra={"task_id": str(task.id), "kind": kind.value},
    )
    return notification


class NotificationService:
    """The owner's view of their notifications. Read and mark read; nothing else.

    Every query is filtered on the owner it was constructed with. There is no
    create here -- `record_outcome` is the one writer -- no delete, and no
    method that sends, executes or authorizes anything.
    """

    def __init__(self, session: AsyncSession, owner_id: uuid.UUID) -> None:
        self._session = session
        self._owner_id = owner_id

    async def unread(self, limit: int = MAX_LISTED) -> List[TaskNotification]:
        """Notifications not yet seen, oldest first."""
        bounded = max(1, min(int(limit), MAX_LISTED))
        return list((await self._session.execute(
            select(TaskNotification)
            .where(
                TaskNotification.owner_id == self._owner_id,
                TaskNotification.read_at.is_(None),
            )
            .order_by(TaskNotification.created_at.asc(), TaskNotification.id.asc())
            .limit(bounded)
        )).scalars().all())

    async def for_task(self, task_id: uuid.UUID) -> List[TaskNotification]:
        """Every notification one of the owner's tasks produced."""
        return list((await self._session.execute(
            select(TaskNotification)
            .where(
                TaskNotification.owner_id == self._owner_id,
                TaskNotification.task_id == task_id,
            )
            .order_by(TaskNotification.created_at.asc(), TaskNotification.id.asc())
        )).scalars().all())

    async def get(self, notification_id: uuid.UUID) -> Optional[TaskNotification]:
        return (await self._session.execute(
            select(TaskNotification).where(
                TaskNotification.id == notification_id,
                TaskNotification.owner_id == self._owner_id,
            )
        )).scalars().first()

    async def mark_read(self, notification_id: uuid.UUID) -> bool:
        """Mark one notification seen. True only for the call that did it.

        Conditional on the owner and on its still being unread, so a second
        call, or a call for another owner's notification, changes nothing.
        """
        result = await self._session.execute(
            update(TaskNotification)
            .where(and_(
                TaskNotification.id == notification_id,
                TaskNotification.owner_id == self._owner_id,
                TaskNotification.read_at.is_(None),
            ))
            .values(read_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session="fetch")
        )
        return result.rowcount == 1


__all__ = [
    "MAX_LISTED",
    "NOTIFIABLE_OUTCOMES",
    "NotificationService",
    "record_outcome",
]
