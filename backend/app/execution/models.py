"""Execution ORM models.

Two tables, with different rules.

`executions` is mutable state: one row per action, its lifecycle column moving
forward through declared transitions only.

`execution_events` is an append-only journal. The service never updates or
deletes one, and no API route can. Reconstructing "what happened to this
action?" reads the journal, not the row -- because the row only ever shows
where something ended up.

**No secrets are stored.** Not the provider key, not the database URL, not raw
tool output, not a stack trace. `result_summary` is a bounded sentence the
tool produced; `error_code` is an application constant.
"""

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database.models.base import Base, utcnow
from app.execution.states import ExecutionState
from app.tools.schemas import AuthorizationStatus, RiskLevel


def _enum_values(enum_cls) -> list:
    return [member.value for member in enum_cls]


#: JSON that is `jsonb` on PostgreSQL and `json` on SQLite.
#:
#: Not a blanket `sa.JSON`: on PostgreSQL, `jsonb` is the type that can be
#: indexed and queried, and choosing it now avoids a migration later. The
#: SQLite variant keeps the test suite working unchanged.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class ExecutionEventType(str, enum.Enum):
    """Every lifecycle event worth reconstructing later."""

    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REVOKED = "revoked"
    EXPIRED = "expired"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"
    #: A run was attempted and a gate refused it *before* the tool was
    #: reached. Distinct from EXECUTION_FAILED, which means the tool ran and
    #: did not succeed. Conflating them would make the journal unable to
    #: answer the question it exists for: did anything actually happen?
    REFUSED = "refused"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Execution(Base):
    """One proposed action, and whatever became of it."""

    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The normalised arguments, exactly as they would be passed to the tool.
    #: The approval fingerprint is taken over these, so editing them after
    #: approval invalidates it.
    arguments: Mapped[Dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    #: The Stage 4C decision, recorded at proposal time. Kept so the audit can
    #: show what policy said, and re-checked at execution rather than trusted.
    authorization_status: Mapped[AuthorizationStatus] = mapped_column(
        Enum(
            AuthorizationStatus,
            name="execution_authorization_status",
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    risk_level: Mapped[Optional[RiskLevel]] = mapped_column(
        Enum(RiskLevel, name="execution_risk_level", values_callable=_enum_values),
        nullable=True,
    )

    state: Mapped[ExecutionState] = mapped_column(
        Enum(ExecutionState, name="execution_state", values_callable=_enum_values),
        nullable=False,
        default=ExecutionState.PROPOSED,
    )

    #: SHA-256 over (tool, arguments), taken when approval was granted.
    #: NULL until then. Compared against a freshly computed fingerprint before
    #: anything runs.
    approved_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: What the client called this request. UNIQUE: the database, not an
    #: application check, is what makes a duplicate request impossible --
    #: two concurrent creates cannot see each other's uncommitted row.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    #: The conversation this execution was proposed from, when it came from
    #: chat. NULL for executions created through the execution API.
    #:
    #: Added in Stage 4F-D so a chat turn can find the proposal its own
    #: previous turn made. It is also the right model independently: an
    #: execution proposed from a conversation belongs to it, and an audit
    #: reader asking "where did this come from?" should not have to guess.
    #:
    #: `ondelete="SET NULL"` rather than CASCADE: deleting a conversation
    #: must not delete the record that something was executed. The journal
    #: outlives the chat that started it.
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: A bounded sentence from the tool. Never raw output.
    result_summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    #: An application constant. Never an exception string or a traceback.
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    events: Mapped[List["ExecutionEvent"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="ExecutionEvent.occurred_at",
    )

    __table_args__ = (
        # The idempotency guarantee, enforced where it cannot be raced.
        Index("uq_executions_idempotency_key", "idempotency_key", unique=True),
        Index("ix_executions_state", "state"),
        Index("ix_executions_tool_name", "tool_name"),
        Index("ix_executions_created_at", "created_at"),
        Index("ix_executions_conversation_id", "conversation_id"),
        # An approved execution must carry the fingerprint it was approved
        # against and an expiry. Without both, "is this approval still valid?"
        # has no answer, and the safe reading of no answer is no.
        CheckConstraint(
            "state <> 'approved' OR "
            "(approved_fingerprint IS NOT NULL AND approved_at IS NOT NULL "
            " AND approval_expires_at IS NOT NULL)",
            name="approved_requires_fingerprint_and_expiry",
        ),
        # A finished execution must say when it finished.
        CheckConstraint(
            "state NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
            name="completed_requires_timestamp",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Execution {self.tool_name} {self.state.value}>"


class ExecutionEvent(Base):
    """One thing that happened. Append-only.

    Nothing in the application updates or deletes a row here. The service
    writes; no route offers an edit; a test asserts both.
    """

    __tablename__ = "execution_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[ExecutionEventType] = mapped_column(
        Enum(
            ExecutionEventType,
            name="execution_event_type",
            values_callable=_enum_values,
        ),
        nullable=False,
    )

    #: Who or what caused it: `user`, `system`, `policy`. Never a model.
    actor: Mapped[str] = mapped_column(String(32), nullable=False, default="system")

    #: Structured context. Constants, counts and states only -- the writer is
    #: `audit.py`, which redacts before persisting.
    event_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSONColumn, nullable=False, default=dict
    )

    #: Monotonic within one execution, so the journal has a total order even
    #: when two events share a timestamp.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    execution: Mapped["Execution"] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_execution_events_execution_id", "execution_id"),
        Index("ix_execution_events_occurred_at", "occurred_at"),
        # One sequence number per execution: the journal cannot be reordered
        # by inserting a duplicate position.
        Index(
            "uq_execution_events_sequence",
            "execution_id",
            "sequence",
            unique=True,
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExecutionEvent {self.event_type.value} seq={self.sequence}>"


__all__ = ["Execution", "ExecutionEvent", "ExecutionEventType"]
