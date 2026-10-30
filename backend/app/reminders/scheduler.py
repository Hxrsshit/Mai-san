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

**Whose reminders (Stage 7B unit 8).** The pass finds due reminders that
belong to an active member's personal scope (`active_member_rows`), so an
unattributed (legacy) reminder or one of a revoked member is never selected
and cannot occupy the bounded pass. Each is then fired only after its member
is re-read under a share lock (`lock_active_principal`, the unit 7 gate) and
the scope built from that principal owns the row; the firing service is
bound to exactly that scope.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.identity.principal import lock_active_principal
from app.identity.scope import KnowledgeScope, active_member_rows, personal_member_of
from app.reminders.models import Reminder, ReminderState
from app.reminders.service import MAX_PER_TICK, ReminderService

logger = get_logger(__name__)


async def due_reminders(session, now: Optional[datetime] = None) -> List[Reminder]:
    """Every active member's scheduled reminders whose time has come, oldest
    first, at most `MAX_PER_TICK`. Never an unattributed row."""
    moment = now or datetime.now(timezone.utc)
    return list((await session.execute(
        select(Reminder)
        .where(
            Reminder.state == ReminderState.SCHEDULED,
            Reminder.next_run_at <= moment,
            active_member_rows(Reminder),
        )
        .order_by(Reminder.next_run_at.asc())
        .limit(MAX_PER_TICK)
    )).scalars().all())


async def _fire_for_its_member(session, settings, reminder: Reminder) -> bool:
    """Fire `reminder` through a service bound to its member's scope, only if
    that member is still active (re-read under a share lock) and the scope
    built from them owns the row. A refusal is False, never an error."""
    member_id = personal_member_of(reminder)
    if member_id is None:
        return False
    principal = await lock_active_principal(session, member_id)
    if principal is None:
        logger.info("Reminder not fired: its member is no longer active",
                    extra={"reminder_id": str(reminder.id)})
        return False
    scope = KnowledgeScope.personal(principal)
    if not scope.owns(reminder):
        return False
    return await ReminderService(session, settings=settings, scope=scope).fire(reminder)


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
            for reminder in await due_reminders(session, now=now):
                # One savepoint per reminder: a failure on one -- its
                # member's re-check included -- skips that reminder only,
                # and never aborts the rest of the pass (Stage 7B unit 8).
                try:
                    async with session.begin_nested():
                        if await _fire_for_its_member(session, settings, reminder):
                            fired += 1
                except Exception as exc:  # noqa: BLE001 - one reminder only
                    logger.error(
                        "Reminder skipped",
                        extra={"reminder_id": str(reminder.id), "error": type(exc).__name__},
                    )
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


__all__ = ["ReminderScheduler", "due_reminders", "run_due_reminders"]
