"""The reminder scheduler: a bounded poller, not a distributed job system.

Mai is a personal, local-first system. One process, one loop, one table. The
correctness that matters here is *exactly once per occurrence* and *survives a
restart*, and both are properties of the database rather than of this loop --
which is what makes a loop this simple defensible.

- **Survives restart** because nothing is held in memory. A reminder's next
  occurrence is a column; a restarted process reads it and carries on, and a
  reminder that came due while the process was down fires on the next tick.
- **Fires once** because `ReminderService.fire` claims the occurrence with a
  conditional UPDATE and the notification table refuses a duplicate. Two
  processes, or two overlapping ticks, are safe.

The loop itself is deliberately dumb: wake, ask for due reminders, fire each,
sleep. It owns no state, so there is nothing in it to get out of step with the
database.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.reminders.service import ReminderService

logger = get_logger(__name__)


async def run_due_reminders(
    session_factory, settings: Optional[Settings] = None, now: Optional[datetime] = None
) -> int:
    """Fire everything that is due. Returns how many fired. Never raises.

    One session for the pass, committed once at the end: the claims and the
    notifications land together, so a crash mid-pass leaves the unclaimed
    reminders untouched rather than half-advanced.

    `now` is injectable so the whole scheduler is testable at an arbitrary
    instant. Tests never sleep.
    """
    settings = settings or get_settings()
    fired = 0

    try:
        async with session_factory() as session:
            service = ReminderService(session, settings=settings)
            due = await service.due(now=now)
            for reminder in due:
                if await service.fire(reminder):
                    fired += 1
            if session.in_transaction():
                await session.commit()
    except Exception as exc:  # noqa: BLE001 - a tick must never kill the loop
        logger.error(
            "Reminder tick failed",
            # A reason, never a reminder's text.
            extra={"error": str(exc)},
            exc_info=exc,
        )
        return 0

    if fired:
        logger.info("Reminders fired", extra={"count": fired})
    return fired


class ReminderScheduler:
    """Owns the polling task. Started and stopped by the app lifespan."""

    def __init__(self, session_factory, settings: Optional[Settings] = None) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin polling. Idempotent."""
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="reminder-scheduler")
        logger.info(
            "Reminder scheduler started",
            extra={"interval_seconds": self._settings.REMINDER_POLL_SECONDS},
        )

    async def stop(self) -> None:
        """Stop polling and wait for the current tick to finish."""
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        logger.info("Reminder scheduler stopped")

    async def _loop(self) -> None:
        interval = max(1, int(self._settings.REMINDER_POLL_SECONDS))
        while not self._stopping.is_set():
            await run_due_reminders(self._session_factory, self._settings)
            try:
                # Waiting on the stop event rather than sleeping means
                # shutdown is immediate instead of up to one interval late.
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise


__all__ = ["ReminderScheduler", "run_due_reminders"]
