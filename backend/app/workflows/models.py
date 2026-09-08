"""Durable workflow state.

One new table. Steps are **not** stored here -- a step is an `Execution`, so
every Stage 4E guarantee (authorization, approval fingerprint, expiry, the
atomic claim, the append-only journal) applies to it without a second
implementation. What this table adds is the identity, the plan and the
workflow-level state that a set of independent executions cannot express.

The plan is stored as JSON rather than as rows because it is written once and
read whole. It is application-generated data, never model output, and it is
what the approval fingerprint is computed over -- so it must be recoverable
byte-for-byte to re-verify an approval.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database.metadata import Base
from app.workflows.states import WorkflowState

#: `jsonb` on PostgreSQL, `json` on SQLite. The same choice Stage 4E made.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


def _enum_values(enum_class) -> List[str]:
    return [member.value for member in enum_class]


def utcnow() -> datetime:
    """Python-side, not `func.now()`.

    A server default here would need a database round trip during flush, and
    Stage 2 established that doing so under the async driver raises
    `MissingGreenlet`.
    """
    return datetime.now(timezone.utc)


class Workflow(Base):
    """One planned composition of existing capabilities."""

    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )

    #: The conversation that proposed it. A workflow proposed in one
    #: conversation can never be confirmed from another -- the same rule
    #: Stage 4F-D applies to research proposals, for the same reason.
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    #: Which template produced this. One value today; stored so a later
    #: workflow shape is distinguishable in the audit record.
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="research_document")

    state: Mapped[WorkflowState] = mapped_column(
        Enum(WorkflowState, name="workflow_state", values_callable=_enum_values),
        nullable=False,
        default=WorkflowState.PENDING,
        index=True,
    )

    #: The plan, exactly as it was fingerprinted.
    plan: Mapped[Dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    #: What the approval is bound to. Computed by the application from the
    #: plan above; never accepted from a caller.
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

    #: An application reason code when something went wrong. Never a provider
    #: message, never an exception string, never a path.
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # An approved workflow must carry what it was approved against and
        # when that lapses. Without both, "is this still valid?" has no
        # answer, and the safe reading of no answer is no.
        CheckConstraint(
            "state <> 'approved' OR "
            "(approved_fingerprint IS NOT NULL AND approved_at IS NOT NULL "
            " AND approval_expires_at IS NOT NULL)",
            name="ck_workflows_approved_requires_fingerprint_and_expiry",
        ),
        CheckConstraint(
            "state NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
            name="ck_workflows_completed_requires_timestamp",
        ),
        Index("ix_workflows_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Workflow {self.id} {self.state.value}>"


__all__ = ["Workflow", "utcnow"]
