"""What a task turn produced, and what the read API returns.

Application state, never model output. Nothing here carries a token, a
header, an endpoint or a tool argument.
"""

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tasks.models import (
    MAX_OBJECTIVE_CHARS,
    MAX_RESULT_CHARS,
    Priority,
    TaskEventType,
    TaskOrigin,
)
from app.tasks.states import TaskState, TaskStepState


class TaskOutcome(str, enum.Enum):
    """What a task operation did. Explicit, because "nothing happened" has
    several causes and each needs a different sentence."""

    CREATED = "created"
    UPDATED = "updated"
    CANCELLED = "cancelled"
    #: The requested transition is not a declared edge.
    INVALID_TRANSITION = "invalid_transition"
    #: The task is in a terminal state and cannot move.
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"
    #: The caller is not the owner.
    FORBIDDEN = "forbidden"
    #: Refused before anything was written -- a bad objective, a bad plan.
    REFUSED = "refused"
    FAILED = "failed"


#: Default bounds a task is created with.
#:
#: Inert in Stage 6A: nothing executes, so nothing spends. They are set at
#: creation anyway so that the stage which adds a runner inherits a budget
#: rather than inventing one for tasks that already exist.
DEFAULT_BUDGET: Dict[str, int] = {
    "max_steps": 20,
    "max_tool_calls": 40,
    "max_model_calls": 20,
    "max_seconds": 900,
}

#: The keys a budget may carry. A closed set, so a caller cannot invent a
#: bound that nothing enforces.
BUDGET_KEYS = frozenset(DEFAULT_BUDGET)


class TaskResult(BaseModel):
    """The report for one task operation."""

    model_config = ConfigDict(frozen=True)

    outcome: TaskOutcome
    task_id: Optional[uuid.UUID] = None
    state: Optional[TaskState] = None
    #: An application reason code. Never an exception, never model output.
    reason: Optional[str] = Field(default=None, max_length=64)

    @property
    def ok(self) -> bool:
        return self.outcome in {
            TaskOutcome.CREATED, TaskOutcome.UPDATED, TaskOutcome.CANCELLED
        }


class RunnerOutcome(str, enum.Enum):
    """What one runner invocation did. A closed set, and every member is a
    fact the database can be asked to confirm afterwards."""

    #: A step ran and finished.
    STEP_COMPLETED = "step_completed"
    #: A step ran and did not finish.
    STEP_FAILED = "step_failed"
    #: The last step finished, so the task is done.
    TASK_COMPLETED = "task_completed"
    #: Nothing could be advanced right now -- dependencies, approval, or
    #: another caller holding the step. Try again later.
    BLOCKED = "blocked"
    #: Nothing may be advanced. Terminal, unauthorised, expired, or refused.
    REFUSED = "refused"
    #: A bound was reached.
    BUDGET_EXCEEDED = "budget_exceeded"
    #: Stage 6G. A monitoring check found its condition held. The task is
    #: complete and monitoring stops.
    CONDITION_MET = "condition_met"
    #: Stage 6G. A monitoring check ran and the condition did not hold. Check
    #: again after the interval.
    CONDITION_NOT_MET = "condition_not_met"
    #: Stage 6G. A monitoring check could not be performed, or ran but could
    #: not be evaluated. Never reported as "not met": nothing was learned.
    CHECK_FAILED = "check_failed"


class RunnerResult(BaseModel):
    """What one invocation of the runner did. Deterministic."""

    model_config = ConfigDict(frozen=True)

    outcome: RunnerOutcome
    task_id: Optional[uuid.UUID] = None
    step_key: Optional[str] = None
    #: A reference to the authoritative execution record, when one exists.
    execution_id: Optional[uuid.UUID] = None
    state: Optional[TaskState] = None
    #: An application reason code. Never model text, never an exception.
    reason: Optional[str] = Field(default=None, max_length=64)

    @property
    def advanced(self) -> bool:
        """Whether this invocation moved the task forward."""
        return self.outcome in {
            RunnerOutcome.STEP_COMPLETED, RunnerOutcome.TASK_COMPLETED,
            RunnerOutcome.CONDITION_MET, RunnerOutcome.CONDITION_NOT_MET,
        }


class TaskStepRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    step_key: str
    sequence: int
    title: str
    state: TaskStepState
    depends_on: List[str] = Field(default_factory=list)
    #: A reference to the execution record, when one exists. The execution's
    #: own detail is read from the execution API, not copied here.
    execution_id: Optional[uuid.UUID] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TaskEventRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    event_type: TaskEventType
    actor: str
    sequence: int
    occurred_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    conversation_id: Optional[uuid.UUID] = None
    origin: TaskOrigin
    objective: str = Field(max_length=MAX_OBJECTIVE_CHARS)
    state: TaskState
    priority: Priority
    deadline: Optional[datetime] = None
    current_step: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    spent: Dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    result: Optional[str] = Field(default=None, max_length=MAX_RESULT_CHARS)
    failure_count: int = 0
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    #: How many steps the plan has. The plan itself is **not** returned by
    #: default: it is bounded but large, and a listing does not need it.
    step_count: int = 0


class TaskDetail(TaskRead):
    """One task, with its steps and journal."""

    steps: List[TaskStepRead] = Field(default_factory=list)
    events: List[TaskEventRead] = Field(default_factory=list)
    #: The validated plan as stored. Present only on the detail view.
    plan: Optional[Dict[str, Any]] = None


class PlanStepPreview(BaseModel):
    """One step of a plan, as shown before anything runs."""

    model_config = ConfigDict(frozen=True)

    step_key: str
    sequence: int
    title: str
    description: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    expected_outcome: Optional[str] = None
    completion_criteria: List[str] = Field(default_factory=list)
    #: Stage 6C. The capability the application **bound**, from the
    #: registry -- not the name the model wrote. NULL until the plan is
    #: authorised, and NULL forever for a prose-only step.
    capability: Optional[str] = None
    #: Whether this step's dependencies are satisfied and it could be worked
    #: next. Computed from the graph, never stored.
    runnable: bool = False
    #: Where this step is. Always `pending` while nothing executes.
    state: TaskStepState
    #: Present only once a step has actually run. A reference to the
    #: authoritative execution record, never a copy of it.
    execution_id: Optional[uuid.UUID] = None


class PlanPreview(BaseModel):
    """What Mai intends to do, before it does any of it.

    Assembled from persisted state: the task row, its materialised steps and
    the stored plan. No field here is written by a model at read time -- the
    plan was validated by application code on the way in, and this renders
    what was stored.

    Deliberately absent: anything about credentials, providers, endpoints or
    authorization. A preview is for a person deciding whether to approve, and
    none of those help them decide.
    """

    model_config = ConfigDict(frozen=True)

    task_id: uuid.UUID
    objective: str
    task_state: TaskState
    #: The plan's own identifier, from the validated plan. There is one plan
    #: per task and it is immutable, so this is the version.
    plan_id: Optional[uuid.UUID] = None
    goal_summary: Optional[str] = None
    steps: List[PlanStepPreview] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    budget: Dict[str, Any] = Field(default_factory=dict)
    spent: Dict[str, Any] = Field(default_factory=dict)

    #: Where execution stands. All three are inert while no runner exists,
    #: and saying so is the point: a preview that omitted them would let a
    #: reader assume something had started.
    current_step: Optional[str] = None
    step_count: int = 0
    executed_step_count: int = 0
    #: Stage 6C. When the plan became permission. NULL until it did.
    authorized_at: Optional[datetime] = None

    @property
    def has_executed(self) -> bool:
        return self.executed_step_count > 0


class TaskList(BaseModel):
    model_config = ConfigDict(frozen=True)

    tasks: List[TaskRead] = Field(default_factory=list)
    total: int = 0


class ActivityAnswer(BaseModel):
    """One of the six activity questions, answered from records.

    `question` is an application constant and `tasks` is what the query
    returned. There is no field here a model writes.
    """

    model_config = ConfigDict(frozen=True)

    question: str
    states: List[TaskState] = Field(default_factory=list)
    tasks: List[TaskRead] = Field(default_factory=list)
    total: int = 0


class ActivityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    did: ActivityAnswer
    doing: ActivityAnswer
    will_do: ActivityAnswer
    failed: ActivityAnswer
    waiting_for: ActivityAnswer
    needs_approval: ActivityAnswer


__all__ = [
    "BUDGET_KEYS",
    "DEFAULT_BUDGET",
    "ActivityAnswer",
    "ActivityReport",
    "PlanPreview",
    "RunnerOutcome",
    "RunnerResult",
    "PlanStepPreview",
    "TaskDetail",
    "TaskEventRead",
    "TaskList",
    "TaskOutcome",
    "TaskRead",
    "TaskResult",
    "TaskStepRead",
]
