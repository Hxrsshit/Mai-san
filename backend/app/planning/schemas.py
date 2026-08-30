"""Stage 4B goal and plan schemas.

The same two-type split Stage 4A uses, for the same reason:

- `PlanProposal` is what a **model** may propose. Parsed from untrusted output.
- `Plan` is what the **application** accepted. Reaching it means the proposal
  passed schema validation *and* graph validation.

A `Plan` is inert data. It has no method that does anything, no reference to a
tool, no execution state and no approval field a model could set. A task
titled "Send the outreach email" is a sentence about future work; nothing in
this codebase can act on it, and Stage 4B adds nothing that could.
"""

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.intent.schemas import IntentType
from app.planning import limits

#: Task identifiers are slugs: stable, readable, and safe to put in a URL, a
#: log line or a rendered list without escaping. Restricting the character set
#: also means an id cannot carry markup or a delimiter into anything that
#: renders it.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Priority(str, Enum):
    """A deliberately small, closed set.

    Arbitrary priority strings make plans incomparable and invite the model to
    invent scales ("urgent-critical-p0"). Three levels is enough to express
    what matters and little enough to stay consistent.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlanStatus(str, Enum):
    """Why a planning attempt ended the way it did.

    Not an execution state. There is no `running`, `complete` or `failed`
    here, because nothing runs: this records what happened to the *planning*,
    not to the plan.
    """

    #: A validated plan was produced.
    READY = "ready"
    #: The goal was too unclear to plan without inventing constraints.
    NEEDS_CLARIFICATION = "needs_clarification"
    #: The intent did not call for a plan.
    NOT_ELIGIBLE = "not_eligible"
    #: Planning was attempted and could not produce a valid plan.
    FAILED = "failed"
    #: Planning is switched off.
    DISABLED = "disabled"


# --- Goal -------------------------------------------------------------------


class Goal(BaseModel):
    """What the user is trying to achieve, as the application understands it.

    Built from the Stage 4A intent plus the model's reading of the request.
    `source_intent` records which classification led here, so a plan can
    always be traced back to the understanding that justified making it.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    summary: str = Field(..., min_length=1, max_length=limits.MAX_GOAL_SUMMARY_CHARS)
    desired_outcome: Optional[str] = Field(
        default=None, max_length=limits.MAX_DESIRED_OUTCOME_CHARS
    )
    scope: Optional[str] = Field(default=None, max_length=limits.MAX_SCOPE_CHARS)

    #: Limits the user stated. Never inferred -- an invented constraint is
    #: indistinguishable from a real one once it is in the field, so anything
    #: the model supplies without being told belongs in `assumptions`.
    constraints: List[str] = Field(
        default_factory=list, max_length=limits.MAX_CONSTRAINTS
    )

    source_intent: IntentType


# --- What a model may propose -----------------------------------------------


class ProposedTask(BaseModel):
    """One step, as proposed. Validated for shape only; the graph comes later."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(
        ...,
        min_length=limits.MIN_TASK_ID_LENGTH,
        max_length=limits.MAX_TASK_ID_LENGTH,
    )
    title: str = Field(..., min_length=1, max_length=limits.MAX_TASK_TITLE_CHARS)
    description: Optional[str] = Field(
        default=None, max_length=limits.MAX_TASK_DESCRIPTION_CHARS
    )
    priority: Priority = Priority.MEDIUM
    dependencies: List[str] = Field(
        default_factory=list, max_length=limits.MAX_DEPENDENCIES_PER_TASK
    )
    expected_outcome: Optional[str] = Field(
        default=None, max_length=limits.MAX_EXPECTED_OUTCOME_CHARS
    )
    completion_criteria: List[str] = Field(
        default_factory=list, max_length=limits.MAX_COMPLETION_CRITERIA
    )

    @field_validator("id")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        lowered = value.strip().lower()
        if not TASK_ID_PATTERN.match(lowered):
            raise ValueError(
                "task id must be a lowercase slug of letters, digits, '-' and '_'"
            )
        return lowered

    @field_validator("dependencies")
    @classmethod
    def _normalise_dependencies(cls, value: List[str]) -> List[str]:
        """Lowercase, de-duplicate, preserve order.

        Duplicates are normalised away rather than rejected: a model listing
        the same prerequisite twice is describing a graph correctly and
        writing it clumsily, and failing the whole plan for that would trade a
        real plan for a formatting complaint. A dependency that does not
        *exist*, by contrast, is a broken graph and is rejected in layer 2.
        """
        seen: List[str] = []
        for entry in value:
            cleaned = entry.strip().lower()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @field_validator("title", "description", "expected_outcome")
    @classmethod
    def _tidy_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("completion_criteria")
    @classmethod
    def _tidy_criteria(cls, value: List[str]) -> List[str]:
        cleaned = [" ".join(item.split()) for item in value]
        kept = [item[: limits.MAX_CRITERION_CHARS] for item in cleaned if item]
        return kept


class PlanProposal(BaseModel):
    """A whole plan, as proposed by a model.

    `extra="ignore"`: a model that invents a field -- `approved`, `execute`,
    `tool` -- must not be able to widen the shape. Anything unnamed is dropped
    before it is read.
    """

    model_config = ConfigDict(extra="ignore")

    goal_summary: str = Field(
        ..., min_length=1, max_length=limits.MAX_GOAL_SUMMARY_CHARS
    )
    desired_outcome: Optional[str] = Field(
        default=None, max_length=limits.MAX_DESIRED_OUTCOME_CHARS
    )
    scope: Optional[str] = Field(default=None, max_length=limits.MAX_SCOPE_CHARS)

    tasks: List[ProposedTask] = Field(..., min_length=1, max_length=limits.MAX_TASKS)

    assumptions: List[str] = Field(
        default_factory=list, max_length=limits.MAX_ASSUMPTIONS
    )
    risks: List[str] = Field(default_factory=list, max_length=limits.MAX_RISKS)
    success_criteria: List[str] = Field(
        default_factory=list, max_length=limits.MAX_SUCCESS_CRITERIA
    )

    @field_validator("assumptions", "risks", "success_criteria")
    @classmethod
    def _tidy_statements(cls, value: List[str]) -> List[str]:
        cleaned = [" ".join(item.split()) for item in value]
        return [item[: limits.MAX_STATEMENT_CHARS] for item in cleaned if item]

    @model_validator(mode="after")
    def _unique_task_ids(self) -> "PlanProposal":
        seen = set()
        for task in self.tasks:
            if task.id in seen:
                raise ValueError(f"duplicate task id: {task.id!r}")
            seen.add(task.id)
        return self


# --- What the application accepted ------------------------------------------


class PlanTask(BaseModel):
    """A validated step. Present in a `Plan` only if the graph checks passed."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    dependencies: List[str] = Field(default_factory=list)
    expected_outcome: Optional[str] = None
    completion_criteria: List[str] = Field(default_factory=list)

    #: Position in the deterministic topological order, from 1.
    order: int = 0
    #: Dependency depth: 0 for a task that waits on nothing.
    depth: int = 0


class Plan(BaseModel):
    """A validated, dependency-ordered plan. Inert data.

    There is no `execute`, `run` or `approve` on this type, no reference to a
    tool, and no field a model can set that grants anything. A task saying
    "Send the outreach email" is a sentence; Stage 4B contains nothing capable
    of sending one.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    goal: Goal
    tasks: List[PlanTask] = Field(default_factory=list)

    #: Explicitly labelled as assumptions, and kept that way. Stage 4B never
    #: writes these to the memory system: an assumption promoted to a memory
    #: becomes a fact about the user that the user never stated.
    assumptions: List[str] = Field(default_factory=list)
    #: Informational. Risks alter nothing -- there is no execution policy for
    #: them to alter.
    risks: List[str] = Field(default_factory=list)
    #: Plan data, not a stop condition. There is no loop to stop.
    success_criteria: List[str] = Field(default_factory=list)

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    @property
    def dependency_count(self) -> int:
        return sum(len(task.dependencies) for task in self.tasks)

    @property
    def ordered_ids(self) -> List[str]:
        """Task ids in execution-safe order. An ordering, not a schedule."""
        return [task.id for task in self.tasks]


class PlanningResult(BaseModel):
    """The outcome of one planning attempt.

    `plan` is present only when `status is READY`. Every other status carries a
    reason and no plan, so a caller cannot mistake a failure for an empty plan.
    """

    model_config = ConfigDict(frozen=True)

    status: PlanStatus
    plan: Optional[Plan] = None

    #: Why planning did not produce a plan. An application constant, never
    #: model output or user text.
    reason: Optional[str] = None
    #: What the user would need to say for planning to be possible. Set only
    #: for NEEDS_CLARIFICATION.
    clarification_needed: Optional[str] = None

    model_calls: int = Field(default=0, ge=0, le=1)
    duration_ms: float = 0.0

    @property
    def has_plan(self) -> bool:
        return self.status is PlanStatus.READY and self.plan is not None


# --- API views --------------------------------------------------------------


class PlanTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: Optional[str] = None
    priority: Priority
    dependencies: List[str] = Field(default_factory=list)
    expected_outcome: Optional[str] = None
    completion_criteria: List[str] = Field(default_factory=list)
    order: int
    depth: int


class PlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_summary: str
    desired_outcome: Optional[str] = None
    tasks: List[PlanTaskRead] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)

    @classmethod
    def from_plan(cls, plan: Plan) -> "PlanRead":
        return cls(
            id=plan.id,
            goal_summary=plan.goal.summary,
            desired_outcome=plan.goal.desired_outcome,
            tasks=[PlanTaskRead.model_validate(task) for task in plan.tasks],
            assumptions=list(plan.assumptions),
            risks=list(plan.risks),
            success_criteria=list(plan.success_criteria),
        )


class PlanningRead(BaseModel):
    """The API view of a planning outcome."""

    status: PlanStatus
    plan: Optional[PlanRead] = None
    reason: Optional[str] = None
    clarification_needed: Optional[str] = None

    @classmethod
    def from_result(cls, result: PlanningResult) -> "PlanningRead":
        return cls(
            status=result.status,
            plan=PlanRead.from_plan(result.plan) if result.plan else None,
            reason=result.reason,
            clarification_needed=result.clarification_needed,
        )


class PlanDebugRequest(BaseModel):
    conversation_id: Optional[uuid.UUID] = None
    message: str = Field(..., min_length=1, max_length=8000)


__all__ = [
    "Goal",
    "Plan",
    "PlanDebugRequest",
    "PlanProposal",
    "PlanRead",
    "PlanStatus",
    "PlanTask",
    "PlanTaskRead",
    "PlanningRead",
    "PlanningResult",
    "Priority",
    "ProposedTask",
    "TASK_ID_PATTERN",
]
