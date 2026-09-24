"""Reminder ORM models.

Two tables, and the split is the point.

`reminders` is the *schedule*: what to say, when, in which zone, and whether
it repeats. `reminder_notifications` is one row per *occurrence that fired* --
the delivery record, the notification the user reads, and the thing that makes
"fired exactly once" a database property rather than a hope.

### Why not an `Execution`

Stage 4E's `Execution` is one approved payload run once. A recurring reminder
is a standing instruction that fires many times, and forcing it into that
shape would mean either mutating a terminal record or minting an execution per
occurrence before anything is due. What *is* reused is the part worth reusing:
the conditional-UPDATE claim from `app.execution.dispatcher`, which is the
pattern that makes concurrent firing safe.

### Why the text is inert

`text` is whatever the user typed. It is rendered into a notification and
shown back, and nothing reads it for meaning. There is no field here through
which a reminder could name a tool, a URL, a recipient or a capability --
the same "cannot name what does not exist" reasoning the tool schemas use.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow


def _enum_values(enum_cls) -> list:
    """Store the lowercase values, not the Python member names."""
    return [member.value for member in enum_cls]


class ReminderState(str, enum.Enum):
    """Where a reminder is in its life.

    Deliberately small, and terminal states are terminal. `CANCELLED` and
    `COMPLETED` have no outgoing edge, which is what stops a cancelled
    reminder from being resurrected by a later scheduler pass.
    """

    #: Persisted and waiting. The only state the scheduler will claim from.
    SCHEDULED = "scheduled"
    #: A one-time reminder that has fired, or a recurring one that was ended.
    COMPLETED = "completed"
    #: The user cancelled it. Never fires again.
    CANCELLED = "cancelled"
    #: Firing failed repeatedly and the reminder was given up on.
    FAILED = "failed"


class Recurrence(str, enum.Enum):
    """How often a reminder repeats. A closed, deliberately small set.

    Anything a person can say that is not one of these is refused rather than
    approximated -- "every other Tuesday in odd months" is a schedule this
    stage does not support, and guessing at it would set a reminder the user
    did not ask for.
    """

    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"


class NotificationState(str, enum.Enum):
    """Whether the user has seen one fired occurrence."""

    PENDING = "pending"
    READ = "read"


class Reminder(Base):
    """One standing instruction to say something at a time."""

    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: What to remind the user of, in their own words. Inert content.
    text: Mapped[str] = mapped_column(String(500), nullable=False)

    state: Mapped[ReminderState] = mapped_column(
        Enum(ReminderState, name="reminder_state", values_callable=_enum_values),
        nullable=False,
        default=ReminderState.SCHEDULED,
    )
    recurrence: Mapped[Recurrence] = mapped_column(
        Enum(Recurrence, name="reminder_recurrence", values_callable=_enum_values),
        nullable=False,
        default=Recurrence.ONCE,
    )

    #: When this fires next, in UTC. The scheduler's only ordering key.
    #:
    #: Stored in UTC and compared in UTC, because "is it due?" is a question
    #: about instants and a local time is not one. The zone below is kept so
    #: the *next* occurrence of a recurring reminder lands at the same local
    #: clock time across a DST change, which is what a person means by
    #: "every day at 9am".
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: The IANA zone the user's wording was interpreted in, recorded so a
    #: reminder remains explainable after `MAI_TIMEZONE` changes.
    timezone_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC"
    )

    #: How many times it has fired. Read by nothing critical; kept because
    #: "did this ever run?" is the first question asked of a reminder that
    #: seems wrong.
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Consecutive failures, so a reminder that cannot fire is eventually
    #: given up on rather than retried forever.
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_fired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Which conversation asked for it. Provenance, and the scope a
    #: conversational "what reminders do I have?" could narrow to later.
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "fire_count >= 0 AND failure_count >= 0",
            name="counters_non_negative",
        ),
        # A cancelled reminder must say when, and nothing else may.
        CheckConstraint(
            "(state = 'cancelled' AND cancelled_at IS NOT NULL)"
            " OR (state <> 'cancelled' AND cancelled_at IS NULL)",
            name="cancelled_requires_timestamp",
        ),
        # The scheduler's only query: due, scheduled, oldest first. One index
        # serves it, and there is deliberately not a second.
        Index("ix_reminders_state_next_run_at", "state", "next_run_at"),
        Index("ix_reminders_conversation_id", "conversation_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Reminder {self.state.value} {self.text[:30]!r}>"


class ReminderNotification(Base):
    """One occurrence that fired. The thing the user actually reads."""

    __tablename__ = "reminder_notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reminders.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: A copy of the reminder's text as it was when this occurrence fired.
    #: Copied rather than joined so the notification still reads correctly
    #: after the reminder is cancelled and its row cascades away.
    text: Mapped[str] = mapped_column(String(500), nullable=False)

    #: The instant this occurrence was *due*, not the instant it fired.
    #:
    #: Unique per reminder, which is what makes double-firing impossible: a
    #: second scheduler pass that somehow reached the same occurrence cannot
    #: insert a second row for it.
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    state: Mapped[NotificationState] = mapped_column(
        Enum(
            NotificationState,
            name="reminder_notification_state",
            values_callable=_enum_values,
        ),
        nullable=False,
        default=NotificationState.PENDING,
    )

    __table_args__ = (
        Index(
            "uq_reminder_notifications_reminder_id_due_at",
            "reminder_id",
            "due_at",
            unique=True,
        ),
        Index("ix_reminder_notifications_state_delivered_at", "state", "delivered_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ReminderNotification {self.state.value} due={self.due_at}>"


__all__ = [
    "NotificationState",
    "Recurrence",
    "Reminder",
    "ReminderNotification",
    "ReminderState",
]
