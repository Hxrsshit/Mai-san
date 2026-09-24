"""The reminder layer's one entry point into a chat turn.

Kept out of `ReminderService` because the service is about reminders and this
is about *turns*: recognising which of the three conversational shapes a
message is, and holding the one piece of per-conversation state the flow needs
-- a proposal awaiting confirmation.

### Why a confirmation step

Creating a reminder is a persistent, user-affecting action, and Mai's
architecture confirms those rather than performing them on a first reading.
There is a second reason specific to reminders: the schedule is *parsed*, and
a misparse is invisible until the moment the reminder should have fired.
Showing "tomorrow at 10:00" back and waiting for a yes makes a wrong reading
catchable while it still costs nothing.

### Why the pending proposal is in memory

A proposal lives for one exchange. Persisting it would mean a table, a
lifecycle and an expiry policy for something whose entire lifetime is "until
the user's next message" -- and a proposal lost to a restart is a reminder the
user simply asks for again. The same reasoning as Stage 4F-G's in-flight OAuth
state.
"""

import uuid
from typing import Dict, Optional

from app.core.logging import get_logger
from app.reminders.language import (
    ParsedSchedule,
    cancel_subject,
    is_cancel_request,
    is_list_request,
    is_reminder_request,
)
from app.reminders.schemas import ReminderOutcome, ReminderResult
from app.reminders.service import ReminderService

logger = get_logger(__name__)

#: Most proposals held at once. A ceiling on memory, and far past the number
#: of conversations a personal instance has open.
MAX_PENDING = 64

#: Confirmation and refusal vocabulary.
#:
#: Narrow on purpose, and matched on the *whole* message: "yes" confirms,
#: "yes but make it 11" does not, because that is a new request rather than an
#: agreement to the one on the table.
_AFFIRMATIVE = frozenset({
    "yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "go ahead",
    "do it", "please do", "confirm", "confirmed", "set it", "yes please",
})
_NEGATIVE = frozenset({
    "no", "n", "nope", "nah", "cancel", "don't", "dont", "no thanks",
    "never mind", "nevermind", "forget it", "stop",
})


class PendingProposals:
    """Reminder proposals awaiting a yes, keyed by conversation."""

    def __init__(self) -> None:
        self._pending: Dict[uuid.UUID, ParsedSchedule] = {}

    def put(self, conversation_id: uuid.UUID, parsed: ParsedSchedule) -> None:
        if len(self._pending) >= MAX_PENDING:
            # Drop the oldest rather than grow without bound. A dropped
            # proposal costs the user one repeated sentence.
            self._pending.pop(next(iter(self._pending)), None)
        self._pending[conversation_id] = parsed

    def take(self, conversation_id: uuid.UUID) -> Optional[ParsedSchedule]:
        """Read and remove. One-shot, so a proposal cannot be confirmed twice."""
        return self._pending.pop(conversation_id, None)

    def peek(self, conversation_id: uuid.UUID) -> Optional[ParsedSchedule]:
        return self._pending.get(conversation_id)

    def discard(self, conversation_id: uuid.UUID) -> None:
        self._pending.pop(conversation_id, None)


#: One table for the process, like Stage 4F-G's in-flight authorizations.
PENDING = PendingProposals()


class ReminderChat:
    """Turns a chat message into a reminder outcome, or declines it."""

    def __init__(
        self, service: ReminderService, pending: Optional[PendingProposals] = None
    ) -> None:
        self._service = service
        self._pending = pending if pending is not None else PENDING

    async def handle(
        self, conversation_id: uuid.UUID, message: str, normalised: Optional[str] = None
    ) -> ReminderResult:
        """Examine one turn. Never raises; degrades to NOT_REMINDER.

        Order matters. A pending proposal is resolved first, because "yes"
        must be read against what was actually proposed rather than
        re-matched as a fresh request -- the same ordering, and the same
        reason, as the research layer's.
        """
        try:
            reading = normalised or message

            pending = self._pending.peek(conversation_id)
            if pending is not None:
                resolved = self._resolve_pending(conversation_id, reading)
                if resolved is not None:
                    if resolved.outcome is ReminderOutcome.AWAITING_CONFIRMATION:
                        # Confirmed. Persist, and only then say so.
                        return await self._service.create(
                            pending, conversation_id=conversation_id
                        )
                    return resolved

            if is_list_request(reading):
                return await self._service.describe_active()

            if is_cancel_request(reading):
                return await self._service.cancel_matching(cancel_subject(reading))

            if not is_reminder_request(reading):
                return ReminderResult(outcome=ReminderOutcome.NOT_REMINDER)

            parsed = self._service.read_request(reading)
            if not parsed.ok:
                # Recognised, unreadable. Asking costs a turn; guessing sets a
                # reminder for a time the user did not name.
                return await self._service.create(parsed)

            self._pending.put(conversation_id, parsed)
            return ReminderResult(
                outcome=ReminderOutcome.AWAITING_CONFIRMATION,
                human_time=parsed.human_time,
                reply=(
                    f"I'll remind you **{parsed.human_time}** to {parsed.text}.\n\n"
                    "Reply \"yes\" to set it, or anything else to skip."
                ),
            )
        except Exception:  # noqa: BLE001
            # A reminder must never fail a chat turn, for the same reason
            # research must not: the user gets an answer, just a smaller one.
            logger.warning(
                "Reminder handling failed; continuing as an ordinary turn",
                extra={"conversation_id": str(conversation_id)},
            )
            return ReminderResult(outcome=ReminderOutcome.NOT_REMINDER)

    def _resolve_pending(
        self, conversation_id: uuid.UUID, message: str
    ) -> Optional[ReminderResult]:
        """Read a reply against a proposal on the table.

        Returns None when the message is neither a yes nor a no *and* is not
        about reminders at all -- the caller then treats the turn normally and
        the proposal is abandoned, rather than a stray sentence being read as
        agreement.
        """
        normalised = " ".join(message.lower().split()).strip(" .!")

        if normalised in _AFFIRMATIVE:
            # Signals "confirmed" to the caller, which then persists. The
            # proposal is removed here so a repeated "yes" cannot set two.
            self._pending.take(conversation_id)
            return ReminderResult(outcome=ReminderOutcome.AWAITING_CONFIRMATION)

        if normalised in _NEGATIVE:
            self._pending.take(conversation_id)
            return ReminderResult(
                outcome=ReminderOutcome.DECLINED,
                reply="Fine — I haven't set that reminder.",
            )

        # Anything else moves on. The proposal is dropped rather than left to
        # be confirmed by an unrelated "ok" three turns later.
        self._pending.discard(conversation_id)
        return None


__all__ = ["PENDING", "MAX_PENDING", "PendingProposals", "ReminderChat"]
