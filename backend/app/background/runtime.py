"""Stage 6F: the one background runtime.

There is one loop in Mai, and this is it. It wakes at a bounded interval,
runs every kind of due work, and goes back to waiting. Reminders and tasks
share it; there is no second scheduler, no worker framework, no external
queue, and nothing kept in memory that the database does not also hold.

    tick
     |-- reminders:  run_due_reminders()          (Stage 5F.1, unchanged)
     |-- tasks:      discover -> claim -> TaskRunner.advance -> reschedule
     '-- wait on the stop event for one interval

### What was generalised from the reminder poller, and what was not

Generalised, because it is correct for any durable work:

* The loop's shape. It waits on a stop event rather than sleeping, so
  shutdown is immediate; a tick never raises, so one bad pass cannot kill
  the loop; `start` is idempotent and `stop` awaits the task it cancels.
* Persisted scheduling state. A task's `next_run_at` is the reminder's
  `next_run_at`: the only record of when work is due, so a restart resumes
  from the database rather than from anything held here.
* Claiming by conditional UPDATE, with `rowcount` as the arbiter.

**Not** generalised, because it is only safe for reminders:

* One transaction per pass. A reminder's only side effect is a notification
  row written in the same transaction, so a crash rolls the whole pass back
  and nothing has happened. A task's side effect is a tool call -- a search,
  a file write -- that a rollback cannot undo. So a task's claim is committed
  on its own *before* any work begins, and each task gets its own
  transaction so one failure cannot roll back another's completed step.
* Recurrence. Tasks do not recur in this stage.
* The occurrence-uniqueness index. A task relies instead on the runner's
  step claim and the execution service's derived idempotency key.

### What the runtime may do

It discovers due tasks, claims one, and calls `TaskRunner.advance` -- or,
for a monitoring task (Stage 6G), `TaskRunner.check`. That is all. It resolves no capability, asks no authorization question, reads no
standing grant, constructs no execution request, and calls neither the
dispatcher nor the execution service -- structural tests assert each. The
dependency direction is `runtime -> TaskRunner -> everything else`, never
`runtime -> tool`.

### Crash semantics

A  before the claim         nothing happened.
B  after the claim commits  `next_run_at` sits at the lease horizon; when the
                            lease expires the task is due again.
C  during `advance`         the work transaction rolls back: no step is left
                            `running` and no execution row survives. The task
                            is due again after the lease. **A tool call made
                            in this window may run again** -- a rollback
                            cannot undo an external effect, and this module
                            does not pretend otherwise. Task *state* is
                            exactly-once; external effects in this one window
                            are at-least-once.
D  after the tool, before   same as C: one transaction.
   the commit
E  after the commit         the rescheduled `next_run_at` is durable.
F  awaiting approval        `next_run_at` is NULL, so it is not polled until
                            a person reschedules it.
G  grant expired/revoked    the authorization service says approval is
                            required, which is F.
H  after a failed step      the step is `failed`, `next_run_at` NULL. Failed
                            steps are never retried automatically.

### Monitoring (Stage 6G)

A monitoring task is the same row, claimed the same way. Only the
scheduling consequence differs: a check whose condition did not hold makes
the task due again one persisted interval later; a check that held completes
the task and stops polling; a check that could not be evaluated counts as a
failure through the same bounded `_record_failure`, never as "not yet".
Crash windows are the ones above, with one addition: the check number is
claimed in the work transaction, so a crash rolls it back with everything
else and the retried check reuses the same execution identity.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Optional, Tuple

from sqlalchemy import select, update

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.tasks import events as journal
from app.tasks.models import MAX_CONSECUTIVE_FAILURES, Task, TaskEventType
from app.tasks.monitoring import interval_of
from app.tasks.runner import TaskRunner
from app.tasks.schemas import RunnerOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState

logger = get_logger(__name__)

#: How long a claim holds a task, in seconds.
#:
#: Longer than any single step should take -- every integration carries its
#: own request timeout well inside this -- and short enough that a process
#: which died holding a claim does not strand the task for long. When it
#: runs out the task is simply due again.
CLAIM_LEASE_SECONDS = 300

#: How long to wait before retrying a task another worker was holding.
CONTENTION_BACKOFF_SECONDS = 30

#: Base delay after an unexpected runner error. Doubles per consecutive
#: failure, and after `MAX_CONSECUTIVE_FAILURES` the task is blocked.
FAILURE_BACKOFF_SECONDS = 60

#: The ceiling no configuration can raise. Tasks within a tick are advanced
#: one at a time, never concurrently, so this is also the bound on how much
#: background work one tick can do.
HARD_MAX_TASKS_PER_TICK = 20

#: The states a task can be advanced from.
ADVANCEABLE_STATES = frozenset({TaskState.QUEUED, TaskState.RUNNING})


class TickReport(NamedTuple):
    """What one pass did. Counts only."""

    reminders_fired: int = 0
    tasks_discovered: int = 0
    tasks_claimed: int = 0
    tasks_advanced: int = 0


def _utc(moment: Optional[datetime]) -> datetime:
    value = moment or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def tasks_per_tick(settings: Settings) -> int:
    """The configured bound, clamped to the hard ceiling and to at least 1."""
    configured = getattr(settings, "BACKGROUND_MAX_TASKS_PER_TICK", 5)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = 5
    return max(1, min(value, HARD_MAX_TASKS_PER_TICK))


# --- Task work --------------------------------------------------------------


async def discover_due_tasks(
    session_factory, now: datetime, limit: int
) -> List[Tuple[uuid.UUID, uuid.UUID]]:
    """`(task_id, owner_id)` for due tasks, oldest first. Reads only.

    A task is due when it is scheduled, its time has come, it is in a state
    that can advance, and its plan is authorised. The owner comes back with
    the id because the runtime has no request and no user: the task's own
    owner is the identity everything downstream acts as.
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Task.id, Task.owner_id)
                .where(
                    Task.next_run_at.is_not(None),
                    Task.next_run_at <= now,
                    Task.state.in_(list(ADVANCEABLE_STATES)),
                    Task.authorized_at.is_not(None),
                )
                .order_by(Task.next_run_at.asc())
                .limit(limit)
            )
        ).all()
    return [(row[0], row[1]) for row in rows]


async def claim_task(
    session_factory, task_id: uuid.UUID, owner_id: uuid.UUID, now: datetime
) -> bool:
    """Take one due task for one step. Exactly one caller wins.

    The reminder pattern, in its own committed transaction: a conditional
    UPDATE that names the task, its owner, and the condition that it is due
    now. The winner moves `next_run_at` to the lease horizon, so every other
    poller finds it no longer due. The claim and its journal entry commit
    together, before any work begins -- which is what makes a crash during
    the work recoverable rather than invisible.
    """
    lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
    async with session_factory() as session:
        result = await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.owner_id == owner_id,
                Task.next_run_at.is_not(None),
                Task.next_run_at <= now,
                Task.state.in_(list(ADVANCEABLE_STATES)),
            )
            .values(next_run_at=lease_until)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.rollback()
            return False
        await journal.record(
            session, task_id, TaskEventType.BACKGROUND_CLAIMED,
            actor="system", metadata={"lease_seconds": CLAIM_LEASE_SECONDS},
        )
        await session.commit()
    return True


#: What each runner outcome means for scheduling. Anything not named here
#: stops polling -- a new outcome added later is unscheduled rather than
#: retried until someone decides otherwise.
_CONTINUE = frozenset({RunnerOutcome.STEP_COMPLETED})

#: Stage 6G. A check ran and the condition did not hold: due again after the
#: task's own persisted interval.
_CHECK_AGAIN = frozenset({RunnerOutcome.CONDITION_NOT_MET})

#: Outcomes that did work, for the tick's count.
_ADVANCED = frozenset({
    RunnerOutcome.STEP_COMPLETED, RunnerOutcome.TASK_COMPLETED,
    RunnerOutcome.CONDITION_MET, RunnerOutcome.CONDITION_NOT_MET,
})

#: Another worker holds the work. Contention, not failure.
_CONTENDED = frozenset({"step_already_claimed", "check_already_claimed"})

#: Blocked for a reason waiting will not fix: a person must act.
_WAIT_FOR_PERSON = frozenset({
    "awaiting_human_approval", "task_not_queued", "no_runnable_step",
})


async def advance_claimed_task(
    session_factory,
    settings: Settings,
    task_id: uuid.UUID,
    owner_id: uuid.UUID,
    now: datetime,
) -> Optional[RunnerOutcome]:
    """Advance one claimed task by one step, and record what comes next.

    The runner and its whole safety envelope do the work. This function's
    job is the scheduling consequence: continue, back off, or stop.
    """
    async with session_factory() as session:
        tasks = TaskService(session, settings=settings, owner_id=owner_id)
        runner = TaskRunner(tasks)

        task = await tasks.get(task_id)
        if task is not None and task.state is TaskState.QUEUED:
            # The runner's own transition, so `running` is written in one
            # place in the codebase.
            await runner.mark_task_running(task_id)

        monitoring = task is not None and task.monitor is not None
        if monitoring:
            result = await runner.check(task_id)
        else:
            result = await runner.advance(task_id)

        if result.outcome is RunnerOutcome.REFUSED and result.reason == "runner_error":
            # The runner caught something unexpected. Its session may be
            # unusable, and nothing it did should be kept.
            await session.rollback()
            await _record_failure(session_factory, settings, task_id, owner_id, now)
            return result.outcome

        if result.outcome is RunnerOutcome.CHECK_FAILED:
            # The check ran and told us nothing. What it did -- the execution,
            # the observation, the failure event, the spent check number -- is
            # kept; then the failure is counted like any other, bounded, and
            # never retried sooner than the monitor's own interval.
            await session.commit()
            await _record_failure(
                session_factory, settings, task_id, owner_id, now,
                min_delay_seconds=interval_of(task.monitor) or 0,
            )
            return result.outcome

        await _reschedule(session, task_id, result, now)
        await session.commit()
        return result.outcome


async def _reschedule(session, task_id: uuid.UUID, result, now: datetime) -> None:
    task = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalars().first()
    if task is None:
        return

    if result.outcome in _CONTINUE:
        # More work may be ready. Due now; the next tick takes it, so one
        # tick still advances one step per task.
        task.next_run_at = now
        task.failure_count = 0
        await session.flush()
        return

    if result.outcome in _CHECK_AGAIN:
        interval = interval_of(task.monitor)
        if interval is not None:
            task.next_run_at = now + timedelta(seconds=interval)
            task.failure_count = 0
            await session.flush()
            return
        # No interval to wait: fall through and stop, rather than guess one.

    if result.outcome is RunnerOutcome.BLOCKED and result.reason in _CONTENDED:
        # Another worker holds the step. Contention, not failure.
        task.next_run_at = now + timedelta(seconds=CONTENTION_BACKOFF_SECONDS)
        await session.flush()
        return

    # Everything else stops polling: completed, failed, refused, over
    # budget, or waiting for a person. Polling any of these again would
    # either repeat finished work or ask a question nobody is there to
    # answer -- which is the approval-starvation case, and why an
    # awaiting-approval task costs nothing while it waits.
    task.next_run_at = None
    await session.flush()
    await journal.record(
        session, task_id, TaskEventType.BACKGROUND_UNSCHEDULED,
        actor="system",
        metadata={"outcome": result.outcome.value, "reason": result.reason},
    )


async def _record_failure(
    session_factory, settings, task_id, owner_id, now: datetime,
    min_delay_seconds: int = 0,
) -> None:
    """Count an unexpected runner error, and give up after enough of them.

    Bounded by `MAX_CONSECUTIVE_FAILURES`, the constant Stage 6A declared
    for exactly this. After the bound the task is blocked -- not failed,
    because an unexplained error says nothing about whether the work is
    wrong, and a person can decide.
    """
    async with session_factory() as session:
        task = (
            await session.execute(
                select(Task).where(Task.id == task_id, Task.owner_id == owner_id)
            )
        ).scalars().first()
        if task is None:
            return
        task.failure_count = int(task.failure_count or 0) + 1

        if task.failure_count >= MAX_CONSECUTIVE_FAILURES:
            task.next_run_at = None
            await session.flush()
            await TaskService(session, settings=settings, owner_id=owner_id).transition(
                task_id, TaskState.BLOCKED, actor="system",
                reason="background_failures_exhausted",
            )
            await journal.record(
                session, task_id, TaskEventType.BACKGROUND_UNSCHEDULED,
                actor="system",
                metadata={"reason": "failures_exhausted",
                          "failure_count": task.failure_count},
            )
        else:
            delay = max(
                FAILURE_BACKOFF_SECONDS * (2 ** (task.failure_count - 1)),
                min_delay_seconds,
            )
            task.next_run_at = now + timedelta(seconds=delay)
            await session.flush()
        await session.commit()


async def run_due_tasks(
    session_factory, settings: Optional[Settings] = None, now: Optional[datetime] = None
) -> Tuple[int, int, int]:
    """Advance due tasks, one step each, one at a time. Never raises.

    Returns `(discovered, claimed, advanced)`. Sequential by design: there is
    no `gather` and no task per item, so the bound on concurrent background
    work is one, and the bound per tick is `tasks_per_tick`.
    """
    settings = settings or get_settings()
    moment = _utc(now)
    discovered = claimed = advanced = 0
    try:
        due = await discover_due_tasks(session_factory, moment, tasks_per_tick(settings))
    except Exception as exc:  # noqa: BLE001 - a tick must never kill the loop
        logger.error("Could not discover due tasks", extra={"error": type(exc).__name__})
        return 0, 0, 0
    discovered = len(due)

    for task_id, owner_id in due:
        try:
            if not await claim_task(session_factory, task_id, owner_id, moment):
                continue
            claimed += 1
            outcome = await advance_claimed_task(
                session_factory, settings, task_id, owner_id, moment
            )
            if outcome in _ADVANCED:
                advanced += 1
        except Exception as exc:  # noqa: BLE001 - one task must not stop the rest
            logger.error(
                "Background task pass failed",
                extra={"task_id": str(task_id), "error": type(exc).__name__},
            )

    if claimed:
        logger.info(
            "Background tasks advanced",
            extra={"discovered": discovered, "claimed": claimed, "advanced": advanced},
        )
    return discovered, claimed, advanced


# --- The one loop -------------------------------------------------------------


class BackgroundRuntime:
    """The one background loop. Started and stopped by the app lifespan."""

    def __init__(self, session_factory, settings: Optional[Settings] = None) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        #: Completed ticks. Lets a caller wait for the loop to have done a
        #: pass without sleeping on a guess.
        self.ticks = 0
        self._tick_done = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def reminders_enabled(self) -> bool:
        return bool(
            self._settings.REMINDERS_ENABLED
            and self._settings.REMINDER_SCHEDULER_ENABLED
        )

    @property
    def tasks_enabled(self) -> bool:
        return bool(getattr(self._settings, "BACKGROUND_TASKS_ENABLED", False))

    @property
    def has_work_sources(self) -> bool:
        return self.reminders_enabled or self.tasks_enabled

    def start(self) -> None:
        """Begin polling. Idempotent: a second call does nothing."""
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="mai-background-runtime")
        logger.info(
            "Background runtime started",
            extra={
                "interval_seconds": self._settings.REMINDER_POLL_SECONDS,
                "reminders": self.reminders_enabled,
                "tasks": self.tasks_enabled,
            },
        )

    async def stop(self) -> None:
        """Stop polling and wait for the loop to finish. Idempotent."""
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # The cancellation this method asked for. Anything else raised by
            # the loop has already been logged by the tick.
            pass
        logger.info("Background runtime stopped")

    async def tick(self, now: Optional[datetime] = None) -> TickReport:
        """One pass over every kind of due work. Never raises."""
        fired = 0
        discovered = claimed = advanced = 0

        if self.reminders_enabled:
            try:
                from app.reminders.scheduler import run_due_reminders

                fired = await run_due_reminders(
                    self._session_factory, self._settings, now=now
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Reminder pass failed", extra={"error": type(exc).__name__})

        if self.tasks_enabled:
            discovered, claimed, advanced = await run_due_tasks(
                self._session_factory, self._settings, now=now
            )

        return TickReport(fired, discovered, claimed, advanced)

    async def wait_for_ticks(self, count: int, timeout: float = 5.0) -> bool:
        """Wait until at least `count` ticks have completed, or time out."""
        async def _wait():
            while self.ticks < count:
                self._tick_done.clear()
                await self._tick_done.wait()

        try:
            await asyncio.wait_for(_wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _loop(self) -> None:
        interval = max(1, int(self._settings.REMINDER_POLL_SECONDS))
        while not self._stopping.is_set():
            await self.tick()
            self.ticks += 1
            self._tick_done.set()
            try:
                # Waiting on the stop event rather than sleeping means
                # shutdown is immediate instead of up to one interval late.
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


__all__ = [
    "ADVANCEABLE_STATES",
    "CLAIM_LEASE_SECONDS",
    "CONTENTION_BACKOFF_SECONDS",
    "FAILURE_BACKOFF_SECONDS",
    "HARD_MAX_TASKS_PER_TICK",
    "BackgroundRuntime",
    "TickReport",
    "advance_claimed_task",
    "claim_task",
    "discover_due_tasks",
    "run_due_tasks",
    "tasks_per_tick",
]
