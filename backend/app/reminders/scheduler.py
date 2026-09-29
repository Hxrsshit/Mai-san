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


# Stage 6F. There is one background loop in Mai, and it is
# `app.background.runtime.BackgroundRuntime`. It runs `run_due_reminders`
# above on every tick, alongside due task work.
#
# `ReminderScheduler` is kept as a name for that same class -- not a
# subclass, not a wrapper -- so existing callers keep working and a
# structural test can still assert there is exactly one loop class. The
# reminder-specific logic that made 5F.1 correct lives in the function above
# and in `ReminderService.fire`, and is unchanged.
from app.background.runtime import BackgroundRuntime as ReminderScheduler  # noqa: E402


__all__ = ["ReminderScheduler", "run_due_reminders"]
