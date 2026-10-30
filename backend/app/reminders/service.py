"""Reminder persistence, conversational management, and atomic firing.

Three responsibilities, deliberately in one place so there is one module to
audit: turning a recognised request into a persisted reminder, answering
"what reminders do I have" and "cancel the one about X", and the claim the
scheduler uses to fire an occurrence exactly once.

### What the model does and does not do

The model never reaches this module. A reminder's schedule comes from
`app.reminders.language`, which is deterministic; every reply is written here
in application code. That is not stylistic: Stage 5D.1 established that Mai
must not claim an action happened unless the record says it did, and a
confirmation sentence phrased by a model is a claim the model authored. So
"I'll remind you" is emitted on exactly one branch -- after the row committed.

### Firing exactly once

The claim is the conditional-UPDATE pattern from `app.execution.dispatcher`:
a single statement whose WHERE clause names the state *and the occurrence*
the caller believes it is claiming. Two schedulers racing both issue it;
`rowcount` makes exactly one of them the winner. Behind that, the unique index
on `(reminder_id, due_at)` means even a claim that somehow succeeded twice
cannot produce two notifications for the same occurrence.

### Whose reminders (Stage 7B unit 8)

The service belongs to one knowledge scope, given at construction: a
member's own reminders are created with `scope.write_columns()`, and every
read, cancellation, claim and notification is filtered by `scope.clause`.
A conversation is linked only if it is in the same scope. Notifications have
no scope of their own: they are read and marked only through a reminder of
the scope. The scheduler (`app.reminders.scheduler`) finds due reminders of
active members, re-checks each member under a share lock, and fires through
a service built for that member's scope; unattributed (legacy) reminders are
quarantined and never fire.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.models.conversation import Conversation
from app.identity.scope import KnowledgeScope, require_scope
from app.reminders import language
from app.reminders.language import ParsedSchedule, ScheduleProblem
from app.reminders.models import (
    NotificationState,
    Recurrence,
    Reminder,
    ReminderNotification,
    ReminderState,
)
from app.reminders.schemas import ReminderOutcome, ReminderResult

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)

#: Consecutive failures after which a reminder is given up on.
#:
#: Bounded so a reminder that cannot fire -- a row the notification insert
#: keeps rejecting, say -- does not occupy the scheduler forever. Three is
#: enough to ride out a transient database blip and few enough to notice.
MAX_CONSECUTIVE_FAILURES = 3

#: Most reminders listed in one conversational answer.
MAX_LISTED = 20

#: Most reminders the scheduler claims in one pass. A ceiling on how much one
#: iteration can do, so a backlog drains steadily rather than in one burst.
MAX_PER_TICK = 20

#: What Mai says for each way a schedule can be unreadable. Written here, in
#: application code, so the sentence and the state cannot disagree.
_PROBLEM_REPLIES = {
    ScheduleProblem.NO_TIME: (
        "I can set that reminder — when should it go off? Something like "
        "\"tomorrow at 10am\" or \"in 3 hours\" works."
    ),
    ScheduleProblem.UNSUPPORTED_SCHEDULE: (
        "I can only set reminders for a specific time, a delay like \"in 3 "
        "hours\", or a simple daily or weekly repeat. Could you put it one of "
        "those ways?"
    ),
    ScheduleProblem.IN_THE_PAST: (
        "That time has already passed. When would you like the reminder?"
    ),
    ScheduleProblem.TOO_FAR_AHEAD: (
        "That is further ahead than I can set a reminder for. Could you pick "
        "a nearer time?"
    ),
    ScheduleProblem.NO_TEXT: (
        "What should I remind you about?"
    ),
}


def as_utc(moment: datetime) -> datetime:
    """A stored timestamp, as an aware UTC instant.

    Every timestamp this module writes is UTC, but not every driver hands it
    back that way: asyncpg returns an aware datetime, SQLite returns a naive
    one. `astimezone` on a naive value silently reinterprets it as the
    *machine's* local time, which turns a correct row into an occurrence hours
    away -- and the error is invisible on PostgreSQL, so it would reach
    production as a first-run surprise rather than a test failure.

    Stamping the zone the value was written in is the honest reading.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class ReminderService:
    """Everything that reads or writes a reminder."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        *,
        scope: KnowledgeScope,
    ) -> None:
        self._session = session
        self._scope = require_scope(scope, "ReminderService")
        self._settings = settings or get_settings()

    @property
    def scope(self) -> KnowledgeScope:
        return self._scope

    # --- Timezone ------------------------------------------------------------

    def timezone(self):
        """The zone the user's wording is interpreted in.

        The same accessor `app.calendar.service` uses, and for the same
        reason: "tomorrow at 10" is a local claim, and answering it in UTC
        would set the reminder five and a half hours out in Asia/Kolkata.
        """
        from zoneinfo import ZoneInfo

        name = getattr(self._settings, "MAI_TIMEZONE", "UTC") or "UTC"
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            logger.warning("Unknown MAI_TIMEZONE; using UTC for reminders")
            return timezone.utc

    def _now_local(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.timezone())

    # --- Creation -------------------------------------------------------------

    def read_request(self, message: str) -> ParsedSchedule:
        """Parse without persisting. Deterministic, and makes no database call."""
        zone = self.timezone()
        return language.parse(message, self._now_local(), str(zone))

    async def create(
        self, parsed: ParsedSchedule, conversation_id: Optional[uuid.UUID] = None
    ) -> ReminderResult:
        """Persist a parsed reminder.

        Returns `CREATED` only after the row is flushed. Every other path
        returns `FAILED` with a reason, and the caller's reply says so --
        there is no branch here that reports success on an unwritten row.
        """
        if not parsed.ok or parsed.run_at is None:
            return ReminderResult(
                outcome=ReminderOutcome.NEEDS_CLARIFICATION,
                reply=_PROBLEM_REPLIES.get(
                    parsed.problem, _PROBLEM_REPLIES[ScheduleProblem.NO_TIME]
                ),
                reason=parsed.problem.value if parsed.problem else None,
            )

        if conversation_id is not None and not await self._conversation_in_scope(
            conversation_id
        ):
            # Never link another scope's conversation (design section 4);
            # the reminder itself is still the member's own.
            conversation_id = None

        reminder = Reminder(
            text=parsed.text[: language.MAX_TEXT_CHARS],
            state=ReminderState.SCHEDULED,
            recurrence=parsed.recurrence,
            # Stored in UTC. "Is it due?" is a question about instants.
            next_run_at=parsed.run_at.astimezone(timezone.utc),
            timezone_name=parsed.timezone_name,
            conversation_id=conversation_id,
            **self._scope.write_columns(),
        )

        try:
            async with self._session.begin_nested():
                self._session.add(reminder)
                await self._session.flush()
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to persist a reminder",
                # Ids and a reason. Never the reminder text: it is the user's
                # own words and may name a person, a diagnosis or an employer.
                extra={"error": str(exc)},
            )
            return ReminderResult(
                outcome=ReminderOutcome.FAILED,
                reply=(
                    "I could not save that reminder just then, so it is not "
                    "set. Ask me again and I will try once more."
                ),
                reason="persistence_failed",
            )

        logger.info(
            "Reminder created",
            extra={
                "reminder_id": str(reminder.id),
                "recurrence": reminder.recurrence.value,
                "timezone": reminder.timezone_name,
                "text_chars": len(reminder.text),
            },
        )
        return ReminderResult(
            outcome=ReminderOutcome.CREATED,
            reminder_id=reminder.id,
            human_time=parsed.human_time,
            reply=(
                f"Done — I'll remind you {parsed.human_time} to "
                f"{parsed.text}."
            ),
        )

    async def _conversation_in_scope(self, conversation_id: uuid.UUID) -> bool:
        try:
            return (await self._session.execute(
                select(Conversation.id).where(
                    Conversation.id == conversation_id, self._scope.clause(Conversation)
                )
            )).first() is not None
        except _DB_ERRORS:
            return False

    # --- Reading ---------------------------------------------------------------

    async def active(self, limit: int = MAX_LISTED) -> List[Reminder]:
        """Scheduled reminders, soonest first."""
        statement = (
            select(Reminder)
            .where(Reminder.state == ReminderState.SCHEDULED, self._scope.clause(Reminder))
            .order_by(Reminder.next_run_at.asc())
            .limit(limit)
        )
        try:
            return list((await self._session.execute(statement)).scalars().all())
        except _DB_ERRORS as exc:
            logger.error("Failed to list reminders", extra={"error": str(exc)})
            return []

    async def get(self, reminder_id: uuid.UUID) -> Optional[Reminder]:
        """One of this scope's reminders by id, or None -- another member's
        reminder is indistinguishable from a missing one. A filtered SELECT,
        never `session.get` (the identity map would bypass the filter)."""
        try:
            return (await self._session.execute(
                select(Reminder).where(Reminder.id == reminder_id, self._scope.clause(Reminder))
                .execution_options(populate_existing=True)
            )).scalars().first()
        except _DB_ERRORS as exc:
            logger.error("Failed to read a reminder", extra={"error": type(exc).__name__})
            return None

    async def cancel_by_id(self, reminder_id: uuid.UUID) -> ReminderResult:
        """Cancel one of this scope's reminders by id. NOTHING_TO_CANCEL with
        no reminder id when it is not this scope's (or does not exist)."""
        reminder = await self.get(reminder_id)
        if reminder is None:
            return ReminderResult(outcome=ReminderOutcome.NOTHING_TO_CANCEL,
                                  reply="No such reminder.", reason="reminder_not_found")
        return await self._cancel(reminder)

    async def describe_active(self) -> ReminderResult:
        """The conversational answer to "what reminders do I have?"."""
        reminders = await self.active()
        if not reminders:
            return ReminderResult(
                outcome=ReminderOutcome.LISTED,
                matched=0,
                reply="You have no reminders set.",
            )

        zone = self.timezone()
        lines = [
            f"- {reminder.text} — "
            f"{language.describe(as_utc(reminder.next_run_at).astimezone(zone), reminder.recurrence)}"
            for reminder in reminders
        ]
        header = (
            "You have one reminder:"
            if len(reminders) == 1
            else f"You have {len(reminders)} reminders:"
        )
        return ReminderResult(
            outcome=ReminderOutcome.LISTED,
            matched=len(reminders),
            reply=header + "\n" + "\n".join(lines),
        )

    # --- Cancellation -------------------------------------------------------------

    async def cancel_matching(self, subject: str) -> ReminderResult:
        """Cancel the one reminder a phrase names.

        Refuses to act on an ambiguous phrase. Cancelling several reminders
        because a word matched several of them is a destructive guess, and the
        user cannot see what was lost until the one they wanted never fires.
        """
        reminders = await self.active(limit=MAX_LISTED)
        if not reminders:
            return ReminderResult(
                outcome=ReminderOutcome.NOTHING_TO_CANCEL,
                reply="You have no reminders to cancel.",
            )

        phrase = (subject or "").strip().lower()
        if not phrase:
            # "Cancel my reminder" with one reminder set is unambiguous; with
            # several it is not, and picking one would be the same guess.
            if len(reminders) == 1:
                return await self._cancel(reminders[0])
            return ReminderResult(
                outcome=ReminderOutcome.AMBIGUOUS_CANCEL,
                matched=len(reminders),
                reply=(
                    f"You have {len(reminders)} reminders — which should I "
                    "cancel? Naming part of it is enough."
                ),
            )

        matches = [r for r in reminders if phrase in r.text.lower()]
        if not matches:
            return ReminderResult(
                outcome=ReminderOutcome.NOTHING_TO_CANCEL,
                reply=(
                    f"I could not find a reminder about \"{subject}\". "
                    "Ask me what reminders you have and I'll list them."
                ),
            )
        if len(matches) > 1:
            listed = "\n".join(f"- {r.text}" for r in matches)
            return ReminderResult(
                outcome=ReminderOutcome.AMBIGUOUS_CANCEL,
                matched=len(matches),
                reply=(
                    f"That matches {len(matches)} reminders — which one?\n"
                    f"{listed}"
                ),
            )
        return await self._cancel(matches[0])

    async def _cancel(self, reminder: Reminder) -> ReminderResult:
        """Move one reminder to CANCELLED, atomically.

        Conditional on it still being SCHEDULED, so a reminder that fired
        between the read and the write is not "cancelled" after the fact --
        which would make the confirmation a lie about something that already
        happened.
        """
        now = datetime.now(timezone.utc)
        try:
            result = await self._session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder.id,
                    Reminder.state == ReminderState.SCHEDULED,
                    self._scope.clause(Reminder),
                )
                .values(
                    state=ReminderState.CANCELLED,
                    cancelled_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
        except _DB_ERRORS as exc:
            logger.error("Failed to cancel a reminder", extra={"error": str(exc)})
            return ReminderResult(
                outcome=ReminderOutcome.FAILED,
                reply="I could not cancel that just then. Please try again.",
                reason="cancel_failed",
            )

        if result.rowcount != 1:
            return ReminderResult(
                outcome=ReminderOutcome.NOTHING_TO_CANCEL,
                reply=(
                    "That reminder is no longer scheduled, so there was "
                    "nothing to cancel."
                ),
            )

        logger.info("Reminder cancelled", extra={"reminder_id": str(reminder.id)})
        return ReminderResult(
            outcome=ReminderOutcome.CANCELLED,
            reminder_id=reminder.id,
            matched=1,
            reply=f"Cancelled — I won't remind you about {reminder.text}.",
        )

    # --- Firing ---------------------------------------------------------------------

    async def due(self, now: Optional[datetime] = None) -> List[Reminder]:
        """This scope's scheduled reminders whose time has come. Oldest
        first. (The scheduler's cross-member discovery is
        `app.reminders.scheduler.due_reminders`.)

        `now` is injectable so the scheduler is testable at an arbitrary
        instant without sleeping.
        """
        moment = now or datetime.now(timezone.utc)
        statement = (
            select(Reminder)
            .where(
                Reminder.state == ReminderState.SCHEDULED,
                Reminder.next_run_at <= moment,
                self._scope.clause(Reminder),
            )
            .order_by(Reminder.next_run_at.asc())
            .limit(MAX_PER_TICK)
        )
        try:
            return list((await self._session.execute(statement)).scalars().all())
        except _DB_ERRORS as exc:
            logger.error("Failed to read due reminders", extra={"error": str(exc)})
            return []

    async def fire(self, reminder: Reminder) -> bool:
        """Deliver one occurrence, exactly once. Returns whether it fired.

        The order is deliberate and is the whole correctness argument:

        1. **Claim** with a conditional UPDATE naming both the state and the
           occurrence (`next_run_at`). A second scheduler holding the same row
           issues the same statement and matches nothing.
        2. **Insert** the notification. The unique index on
           `(reminder_id, due_at)` is the second line: even a claim that
           somehow succeeded twice cannot deliver twice.

        Claiming first means a crash between the two leaves the occurrence
        claimed but undelivered -- a missed reminder. The reverse order would
        leave it delivered but unclaimed, and the next pass would deliver it
        again. A missed reminder is recoverable by the user; a duplicate at
        3am is not.

        Only a reminder of this service's scope is fired (Stage 7B unit 8);
        the claim itself is scope-filtered too.
        """
        if not self._scope.owns(reminder):
            logger.warning(
                "Reminder fire refused: not in the service's scope",
                extra={"reminder_id": str(reminder.id)},
            )
            return False
        # Normalised before any arithmetic: a naive value from the driver
        # would be read as machine-local and land the next occurrence hours
        # out. `stored` is what the WHERE clause compares against -- that
        # comparison is the database's, so it gets the column value unchanged.
        stored = reminder.next_run_at
        due_at = as_utc(stored)
        following = language.next_occurrence(
            due_at.astimezone(self._zone_of(reminder)), reminder.recurrence
        )
        now = datetime.now(timezone.utc)

        if following is None:
            claimed_state = ReminderState.COMPLETED
            claimed_next = due_at
        else:
            claimed_state = ReminderState.SCHEDULED
            claimed_next = following.astimezone(timezone.utc)

        try:
            result = await self._session.execute(
                update(Reminder)
                .where(
                    Reminder.id == reminder.id,
                    Reminder.state == ReminderState.SCHEDULED,
                    # The occurrence, not just the row. Without this a
                    # recurring reminder already advanced by another pass
                    # would be advanced again.
                    Reminder.next_run_at == stored,
                    self._scope.clause(Reminder),
                )
                .values(
                    state=claimed_state,
                    next_run_at=claimed_next,
                    last_fired_at=now,
                    fire_count=Reminder.fire_count + 1,
                    failure_count=0,
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
        except _DB_ERRORS as exc:
            logger.error(
                "Reminder claim failed",
                extra={"reminder_id": str(reminder.id), "error": str(exc)},
            )
            return False

        if result.rowcount != 1:
            # Another pass claimed this occurrence, or it was cancelled
            # between the read and the write. Either way this attempt must
            # not deliver.
            logger.info(
                "Reminder occurrence already claimed",
                extra={"reminder_id": str(reminder.id)},
            )
            return False

        try:
            async with self._session.begin_nested():
                self._session.add(
                    ReminderNotification(
                        reminder_id=reminder.id,
                        # Copied, so the notification survives the reminder.
                        text=reminder.text,
                        due_at=due_at,
                        state=NotificationState.PENDING,
                    )
                )
                await self._session.flush()
        except IntegrityError:
            # The unique index refused a second notification for this
            # occurrence. The claim is still correct; there is simply nothing
            # more to deliver.
            logger.info(
                "Reminder occurrence was already delivered",
                extra={"reminder_id": str(reminder.id)},
            )
            return False
        except _DB_ERRORS as exc:
            logger.error(
                "Reminder delivery failed after claiming",
                extra={"reminder_id": str(reminder.id), "error": str(exc)},
            )
            await self._record_failure(reminder)
            return False

        logger.info(
            "Reminder fired",
            extra={
                "reminder_id": str(reminder.id),
                "recurrence": reminder.recurrence.value,
                "recurring": following is not None,
            },
        )
        return True

    async def _record_failure(self, reminder: Reminder) -> None:
        """Count a failure, and give up after enough of them."""
        now = datetime.now(timezone.utc)
        try:
            await self._session.execute(
                update(Reminder)
                .where(Reminder.id == reminder.id, self._scope.clause(Reminder))
                .values(
                    failure_count=Reminder.failure_count + 1,
                    state=(
                        ReminderState.FAILED
                        if reminder.failure_count + 1 >= MAX_CONSECUTIVE_FAILURES
                        else ReminderState.SCHEDULED
                    ),
                    updated_at=now,
                )
                .execution_options(synchronize_session="fetch")
            )
        except _DB_ERRORS:  # pragma: no cover - the caller already failed
            logger.error("Could not record a reminder failure")

    def _zone_of(self, reminder: Reminder):
        from zoneinfo import ZoneInfo

        try:
            return ZoneInfo(reminder.timezone_name)
        except Exception:  # noqa: BLE001
            return timezone.utc

    # --- Notifications ---------------------------------------------------------------

    async def pending_notifications(
        self, limit: int = MAX_LISTED
    ) -> Tuple[List[ReminderNotification], int]:
        """Fired occurrences of this scope's reminders the user has not seen.
        A notification is reached only through its reminder's scope."""
        statement = (
            select(ReminderNotification)
            .join(Reminder, Reminder.id == ReminderNotification.reminder_id)
            .where(
                ReminderNotification.state == NotificationState.PENDING,
                self._scope.clause(Reminder),
            )
            .order_by(ReminderNotification.delivered_at.asc())
            .limit(limit)
        )
        try:
            rows = list((await self._session.execute(statement)).scalars().all())
            total = (
                await self._session.execute(
                    select(func.count())
                    .select_from(ReminderNotification)
                    .join(Reminder, Reminder.id == ReminderNotification.reminder_id)
                    .where(
                        ReminderNotification.state == NotificationState.PENDING,
                        self._scope.clause(Reminder),
                    )
                )
            ).scalar_one()
        except _DB_ERRORS as exc:
            logger.error("Failed to read notifications", extra={"error": str(exc)})
            return [], 0
        return rows, int(total)

    async def mark_read(self, notification_id: uuid.UUID) -> bool:
        """Mark one delivered occurrence as seen."""
        try:
            result = await self._session.execute(
                update(ReminderNotification)
                .where(
                    ReminderNotification.id == notification_id,
                    ReminderNotification.state == NotificationState.PENDING,
                    ReminderNotification.reminder_id.in_(
                        select(Reminder.id).where(self._scope.clause(Reminder))
                    ),
                )
                .values(state=NotificationState.READ)
                .execution_options(synchronize_session="fetch")
            )
        except _DB_ERRORS as exc:
            logger.error("Failed to mark a notification read", extra={"error": str(exc)})
            return False
        return result.rowcount == 1


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_LISTED",
    "MAX_PER_TICK",
    "ReminderService",
    "as_utc",
]
