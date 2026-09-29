"""Tasks, their steps, and the journal of what happened to them.

Three tables, and each has one job.

**`tasks`** is what the user wants accomplished. It holds the objective, the
state, and a reference to the validated plan -- and deliberately not much
else, because most of what a task "has" is already owned somewhere better.
Permissions live in `app.execution`; what a tool is lives in `app.tools`;
what actually ran lives in `executions` and `execution_events`; what Mai
knows about the user lives in `memories`. A task that copied those would be a
second source of truth, and an audit trail with two answers is worse than
none.

**`task_steps`** is mutable per-step state. The plan itself is immutable --
it is the record of what was intended, and a record that changes cannot be
audited -- so progress is tracked beside it rather than inside it. The
`execution_id` column is a *reference* to the authoritative execution record,
never a copy of it.

**`task_events`** is the append-only journal, modelled directly on
`execution_events`. Nothing updates or deletes a row; one writer appends.

### Nothing here executes

There is no runner in Stage 6A. `tasks` can be created, planned, blocked,
cancelled and failed; nothing can move one to `running` or `completed`,
because no code path performs those transitions. That is enforced by
`app.tasks.service` and pinned by `STAGE_6A_REACHABLE`.
"""

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
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

from app.database.models.base import Base, utcnow
from app.tasks.states import TaskState, TaskStepState

#: JSON on SQLite, JSONB on PostgreSQL. The same declaration the execution
#: and workflow models use.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


def _enum_values(enum_cls) -> list:
    """Store the lowercase values, not the Python member names."""
    return [member.value for member in enum_cls]


#: The single local operator.
#:
#: Mai has no authentication layer and one user, so there is no principal to
#: read an identity from. This constant is that identity, written down once.
#:
#: It is **not** the seeded "user" entity in `app.relationships`: that is a
#: node in the knowledge graph, a subject facts are recorded about, and using
#: it as an ownership key would tie task ownership to whether an entity row
#: survived a merge.
#:
#: The column exists now, rather than when multi-user arrives, because a
#: background runtime that claims work must never have to guess whose work it
#: claimed. Making owner nullable today and mandatory later is a migration
#: over live rows with no correct backfill.
LOCAL_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")


class TaskOrigin(str, enum.Enum):
    """Who asked for this task. A closed set with one member.

    One member is the point. A task may be created by a person and by nothing
    else -- not by an email, a web page, a calendar invite, a reminder or a
    model's own output. Widening this enum is the change a reviewer would
    have to argue for, and `app.tasks.service` accepts no other value.
    """

    USER = "user"


class TaskEventType(str, enum.Enum):
    """What happened. The whole vocabulary, including later stages.

    Declared in full for the same reason as `TaskState`: this is a PostgreSQL
    enum type, and widening one later is a migration. Stage 6A emits only the
    subset in `app.tasks.events.STAGE_6A_EVENTS`, and a test pins that -- so
    no event here can be written for something that did not happen.
    """

    TASK_CREATED = "task_created"
    PLAN_ATTACHED = "plan_attached"
    ASSUMPTION_RECORDED = "assumption_recorded"
    STATE_CHANGED = "state_changed"
    TASK_BLOCKED = "task_blocked"
    TASK_CANCELLED = "task_cancelled"
    TASK_FAILED = "task_failed"
    # Stage 6D. The runner's own vocabulary.
    EXECUTION_CREATED = "execution_created"
    #: Stage 6E. Why Mai did not ask this time.
    STANDING_GRANT_USED = "standing_grant_used"
    RUNNER_BLOCKED = "runner_blocked"
    RUNNER_REFUSED = "runner_refused"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    OBSERVATION_RECORDED = "observation_recorded"
    REPLANNED = "replanned"
    BUDGET_EXCEEDED = "budget_exceeded"
    TASK_COMPLETED = "task_completed"


class Priority(str, enum.Enum):
    """Matches `app.planning.schemas.Priority`, deliberately."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Bounds. Constants in application code, none derived from user input.
MAX_OBJECTIVE_CHARS = 2_000
MAX_ERROR_CODE_CHARS = 64
MAX_RESULT_CHARS = 4_000
MAX_STEP_KEY_CHARS = 40
MAX_STEP_TITLE_CHARS = 120
#: Longest bound capability name. Matches the plan schema's bound, asserted
#: equal by a test so a name that fits a plan always fits a row.
MAX_CAPABILITY_CHARS = 64
#: After this many consecutive failures a task is given up on. The same
#: reasoning, and the same number, as `app.reminders.service`.
MAX_CONSECUTIVE_FAILURES = 3


class Task(Base):
    """One unit of work Mai has been asked to accomplish."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: Whose task this is. See `LOCAL_OWNER_ID`. No foreign key: there is no
    #: users table yet, and inventing one would be inventing authentication.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=LOCAL_OWNER_ID
    )

    #: Which conversation asked. Provenance, and the scope a future "what are
    #: you working on for me?" could narrow to.
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    origin: Mapped[TaskOrigin] = mapped_column(
        Enum(TaskOrigin, name="task_origin", values_callable=_enum_values),
        nullable=False,
        default=TaskOrigin.USER,
    )

    #: What the user wants, in their own words, bounded. User data: never
    #: logged, never treated as an instruction.
    objective: Mapped[str] = mapped_column(
        String(MAX_OBJECTIVE_CHARS), nullable=False
    )

    state: Mapped[TaskState] = mapped_column(
        Enum(TaskState, name="task_state", values_callable=_enum_values),
        nullable=False,
        default=TaskState.PROPOSED,
    )

    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, name="task_priority", values_callable=_enum_values),
        nullable=False,
        default=Priority.MEDIUM,
    )

    deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: The **validated** plan -- `app.planning.schemas.Plan`, dumped -- not
    #: the raw `PlanProposal`. The proposal is model output; the plan is what
    #: the application accepted after validating the dependency graph, and
    #: persisting the accepted artefact rather than the proposed one is the
    #: same rule Stage 5D.1 applies to prose.
    #:
    #: Immutable once attached. A replan writes a new plan and records a
    #: `replanned` event; it does not edit this in place, because a plan that
    #: changes cannot be audited against what was approved.
    plan: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=True
    )

    #: The step currently being worked, by `TaskStep.step_key`. Always NULL in
    #: Stage 6A: nothing works a step.
    current_step: Mapped[Optional[str]] = mapped_column(
        String(MAX_STEP_KEY_CHARS), nullable=True
    )

    #: What this task may spend before it must stop and ask. Set once, at
    #: creation, and never widened by anything the task itself reads -- a
    #: budget an email could raise is not a budget.
    budget: Mapped[Dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    #: What it has spent. Written only by a runner, so every value stays zero
    #: for the whole of Stage 6A; a structural test asserts no code path
    #: increments it.
    spent: Mapped[Dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    #: An application reason code, never an exception and never model output.
    error_code: Mapped[Optional[str]] = mapped_column(
        String(MAX_ERROR_CODE_CHARS), nullable=True
    )
    #: The outcome, when there is one. Application-written text.
    result: Mapped[Optional[str]] = mapped_column(
        String(MAX_RESULT_CHARS), nullable=True
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Stage 6C. When the plan was authorised for execution, and by whom.
    #:
    #: NULL until an application-side decision says so, which is the
    #: difference between having a plan and having permission. Nothing in the
    #: plan can set it: `authorize_plan` is the only writer.
    authorized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        onupdate=utcnow, server_default=func.now(),
    )

    steps: Mapped[List["TaskStep"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TaskStep.sequence",
    )
    events: Mapped[List["TaskEvent"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TaskEvent.sequence",
    )

    __table_args__ = (
        # The same shape as the reminders' cancellation constraint: a state
        # and its timestamp cannot disagree.
        CheckConstraint(
            "(state = 'cancelled' AND cancelled_at IS NOT NULL)"
            " OR (state <> 'cancelled' AND cancelled_at IS NULL)",
            name="cancelled_requires_timestamp",
        ),
        CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL)"
            " OR (state <> 'completed' AND completed_at IS NULL)",
            name="completed_requires_timestamp",
        ),
        CheckConstraint("failure_count >= 0", name="failure_count_non_negative"),
        # Every activity question is a query over (owner, state).
        Index("ix_tasks_owner_id_state", "owner_id", "state"),
        Index("ix_tasks_conversation_id", "conversation_id"),
        Index("ix_tasks_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Task {self.state.value} steps={len(self.steps)}>"


class TaskStep(Base):
    """One step of a task's plan, and what has happened to it."""

    __tablename__ = "task_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: The plan's own identifier for this step, e.g. `"step-one"`. Carried so
    #: a step can be matched back to the plan it came from without position
    #: arithmetic.
    step_key: Mapped[str] = mapped_column(
        String(MAX_STEP_KEY_CHARS), nullable=False
    )
    #: Position in the plan's deterministic topological order, from 1.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(String(MAX_STEP_TITLE_CHARS), nullable=False)

    state: Mapped[TaskStepState] = mapped_column(
        Enum(TaskStepState, name="task_step_state", values_callable=_enum_values),
        nullable=False,
        default=TaskStepState.PENDING,
    )

    #: Step keys this one waits on. A copy of the plan's edges, materialised
    #: so a runner can ask "is this runnable?" without re-parsing the plan.
    depends_on: Mapped[List[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    #: Stage 6C. The capability this step needs, as the **registry** named it.
    #:
    #: Written only by `TaskService.authorize_plan`, from a
    #: `CapabilityBinding` -- never copied from the plan. The plan holds what
    #: the model asked for; this holds what the application bound, and the
    #: difference is the whole point of the binding step. NULL means the step
    #: is prose and cannot execute.
    capability: Mapped[Optional[str]] = mapped_column(
        String(MAX_CAPABILITY_CHARS), nullable=True
    )
    #: The arguments the capability would be called with, as validated.
    #: Bounded by the plan schema before they reach here.
    arguments: Mapped[Dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    #: A **reference** to the authoritative execution record, never a copy of
    #: it. What the tool was, what it was given, whether it was authorised and
    #: what it returned all stay in `executions` and `execution_events`.
    execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("executions.id", ondelete="SET NULL"),
        nullable=True,
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        onupdate=utcnow, server_default=func.now(),
    )

    task: Mapped["Task"] = relationship(back_populates="steps")

    __table_args__ = (
        # One step per key per task, and one step per position. Both are the
        # plan's own guarantees; asserting them here means a materialisation
        # bug cannot produce a plan with two "step 3"s.
        Index("uq_task_steps_task_id_step_key", "task_id", "step_key", unique=True),
        Index("uq_task_steps_task_id_sequence", "task_id", "sequence", unique=True),
        CheckConstraint("sequence >= 1", name="sequence_is_positive"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TaskStep {self.sequence} {self.state.value}>"


class TaskEvent(Base):
    """One entry in a task's append-only journal.

    Modelled on `execution_events`, down to the monotonic sequence and the
    uniqueness of `(task_id, sequence)`. Nothing in the application updates or
    deletes a row here: one writer appends, no route offers an edit, and a
    test asserts both.
    """

    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[TaskEventType] = mapped_column(
        Enum(TaskEventType, name="task_event_type", values_callable=_enum_values),
        nullable=False,
    )

    #: Who or what caused it: `user`, `system`, `policy`. The same three
    #: values `execution_events` uses, and never a model.
    actor: Mapped[str] = mapped_column(String(32), nullable=False, default="system")

    #: Structured context. Constants, counts, states and reason codes only --
    #: the writer redacts before persisting, reusing the execution journal's
    #: own sanitiser so there is one forbidden-key list rather than two.
    event_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSONColumn, nullable=False, default=dict
    )

    #: Monotonic within one task, so the journal has a total order even when
    #: two events share a timestamp.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    task: Mapped["Task"] = relationship(back_populates="events")

    __table_args__ = (
        Index("uq_task_events_task_id_sequence", "task_id", "sequence", unique=True),
        Index("ix_task_events_task_id_occurred_at", "task_id", "occurred_at"),
        CheckConstraint("sequence >= 1", name="event_sequence_is_positive"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TaskEvent {self.event_type.value} seq={self.sequence}>"


__all__ = [
    "LOCAL_OWNER_ID",
    "MAX_CAPABILITY_CHARS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_OBJECTIVE_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_STEP_KEY_CHARS",
    "MAX_STEP_TITLE_CHARS",
    "Priority",
    "Task",
    "TaskEvent",
    "TaskEventType",
    "TaskOrigin",
    "TaskStep",
]
