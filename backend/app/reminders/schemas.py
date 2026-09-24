"""Reminder API and service schemas."""

import enum
import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.reminders.models import Recurrence, ReminderState


class ReminderOutcome(str, enum.Enum):
    """What the reminder layer did on one turn.

    Explicit states rather than a boolean, for the reason every enum in this
    codebase is: "nothing happened" has several causes and each calls for a
    different sentence. "I could not read a time" and "that reminder does not
    exist" are both no, and telling someone the wrong one is a small
    avoidable lie.
    """

    NOT_REMINDER = "not_reminder"
    #: Parsed, shown back, waiting for the user to confirm. Nothing persisted.
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    #: Persisted. The only outcome that licenses "I'll remind you".
    CREATED = "created"
    #: The user declined the proposal.
    DECLINED = "declined"
    #: A pending proposal was dropped because the next turn was about
    #: something else.
    ABANDONED = "abandoned"
    #: Recognised as a reminder request Mai could not read.
    NEEDS_CLARIFICATION = "needs_clarification"
    LISTED = "listed"
    CANCELLED = "cancelled"
    #: A cancellation matching more than one reminder. Nothing was cancelled.
    AMBIGUOUS_CANCEL = "ambiguous_cancel"
    NOTHING_TO_CANCEL = "nothing_to_cancel"
    #: Persistence failed. Mai must not claim the reminder exists.
    FAILED = "failed"
    DISABLED = "disabled"


class ReminderRead(BaseModel):
    """One reminder, as the API reports it."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    text: str
    state: ReminderState
    recurrence: Recurrence
    next_run_at: datetime
    timezone_name: str
    fire_count: int
    created_at: datetime


class ReminderList(BaseModel):
    reminders: List[ReminderRead]
    total: int


class NotificationRead(BaseModel):
    """One fired occurrence, as the API reports it."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    reminder_id: uuid.UUID
    text: str
    due_at: datetime
    delivered_at: datetime


class NotificationList(BaseModel):
    notifications: List[NotificationRead]
    total: int


class ReminderResult(BaseModel):
    """The reminder layer's report for one chat turn.

    `reply` is application-written for every outcome. A reminder confirmation
    is a statement about what the application did, and a model asked to phrase
    it could turn "I could not save that" into "I'll remind you" -- which is
    exactly the Stage 5D.1 failure, in a new place.
    """

    model_config = ConfigDict(frozen=True)

    outcome: ReminderOutcome = ReminderOutcome.NOT_REMINDER
    reply: str = ""
    reminder_id: Optional[uuid.UUID] = None
    #: How the schedule was read, shown back so a misparse is catchable.
    human_time: str = ""
    matched: int = 0
    reason: Optional[str] = Field(default=None, max_length=64)

    @property
    def has_reply(self) -> bool:
        """Whether the application is answering instead of the model."""
        return bool(self.reply)

    @property
    def created(self) -> bool:
        """The one state that licenses a claim that a reminder exists."""
        return self.outcome is ReminderOutcome.CREATED


__all__ = [
    "NotificationList",
    "NotificationRead",
    "ReminderList",
    "ReminderOutcome",
    "ReminderRead",
    "ReminderResult",
]
