"""What a workflow plan is, and what binds it.

A plan is **data, not a program**. It names steps the application already
knows how to perform, in an order the application chose, with arguments the
application validated. Nothing here is executable, nothing here is supplied by
a model, and there is no field a plan can carry that changes what a step is
permitted to do -- authorization is asked of Stage 4C at execution time, every
time.

The fingerprint is the load-bearing part. It is computed here, from
application state, and never accepted from a caller: a client-supplied
fingerprint would let whoever supplies it decide what an approval covers.
"""

import enum
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.workflows.limits import (
    MAX_ARTIFACT_CONTENT_CHARS,
    MAX_DEPENDENCY_EDGES,
    MAX_STEPS,
)


class StepKind(str, enum.Enum):
    """What a step does. A closed set, matched to what Mai can actually do.

    Not a free-form tool name: a plan may only contain kinds this enum names,
    so an unrecognised or invented step cannot be represented at all -- it
    fails at parse time rather than at authorization time.
    """

    #: Run a web search through the Stage 4F-D research path.
    RESEARCH = "research"
    #: Ask the model to synthesise the research into prose. No side effect,
    #: no tool, no authorization -- it is the one step that is not an
    #: execution, because writing text into a variable is not an action.
    SYNTHESISE = "synthesise"
    #: Write the synthesis to a file through `create_text_file`.
    ARTIFACT = "artifact"


#: Which tool each executable kind runs. The single source of that mapping.
#:
#: `SYNTHESISE` is absent, and that absence is the point: a step with no tool
#: cannot be dispatched, so the synthesis step has no route to a side effect
#: however it is reached.
TOOL_FOR_KIND: Dict[StepKind, str] = {
    StepKind.RESEARCH: "web_search",
    StepKind.ARTIFACT: "create_text_file",
}


class WorkflowStep(BaseModel):
    """One step. Frozen, so a holder cannot edit it into a different action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(..., ge=0, lt=MAX_STEPS)
    kind: StepKind
    #: Steps that must have succeeded first. Indices, not references, so a
    #: dependency cannot point outside this plan.
    depends_on: Tuple[int, ...] = ()

    #: Arguments known when the plan was made. For research this is the whole
    #: payload; for the artifact it is the path only -- the content does not
    #: exist until the synthesis step has run. See `plan_fingerprint`.
    arguments: Dict[str, Any] = Field(default_factory=dict)

    @property
    def tool_name(self) -> Optional[str]:
        return TOOL_FOR_KIND.get(self.kind)

    @property
    def is_executable(self) -> bool:
        """Whether this step runs a tool, and so needs authorization."""
        return self.kind in TOOL_FOR_KIND


class WorkflowPlan(BaseModel):
    """An ordered, bounded, closed set of steps.

    Validated on construction rather than on use: an invalid plan should not
    be representable, so there is no later point at which someone must
    remember to check it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What the user asked for, bounded. Kept so the proposal can quote it and
    #: so an audit reader can see what produced this plan.
    request: str = Field(default="", max_length=2000)
    steps: Tuple[WorkflowStep, ...] = ()

    def model_post_init(self, _context: Any) -> None:
        if len(self.steps) > MAX_STEPS:
            raise ValueError(f"a plan may not exceed {MAX_STEPS} steps")

        indices = [step.index for step in self.steps]
        if indices != sorted(indices) or len(set(indices)) != len(indices):
            raise ValueError("step indices must be unique and ascending")

        edges = sum(len(step.depends_on) for step in self.steps)
        if edges > MAX_DEPENDENCY_EDGES:
            raise ValueError(
                f"a plan may not exceed {MAX_DEPENDENCY_EDGES} dependency edges"
            )

        known = set(indices)
        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in known:
                    raise ValueError("a dependency names no step in this plan")
                if dependency >= step.index:
                    # Forward and self references are refused rather than
                    # sorted out: a plan whose steps depend on later steps has
                    # no valid order, and a cycle cannot be represented at all
                    # if every edge must point backwards.
                    raise ValueError("a dependency must point to an earlier step")

    @property
    def executable_steps(self) -> Tuple[WorkflowStep, ...]:
        return tuple(step for step in self.steps if step.is_executable)

    def step(self, index: int) -> Optional[WorkflowStep]:
        for candidate in self.steps:
            if candidate.index == index:
                return candidate
        return None


def plan_fingerprint(workflow_id: uuid.UUID, plan: WorkflowPlan) -> str:
    """What an approval is bound to. Computed here; never accepted.

    Covers the workflow's identity, every step's position, its kind, its tool
    and the arguments that exist at approval time. Change any of those after
    approval and the fingerprint no longer matches, so the approval no longer
    applies -- which is the whole mechanism behind "an approval for one
    operation is not an approval for another".

    **The artifact's content is deliberately not in here, and cannot be.** It
    does not exist when the user approves: it is synthesised from research
    that has not run yet. What *is* bound is the artifact's path, so the file
    the user agreed to is the file that gets written. Content is constrained
    instead by three other things -- it is written inside the workspace, it is
    inert data rather than anything executable, and the path it lands at
    cannot move. This is stated plainly in the architecture note rather than
    left for a reader to notice.
    """
    canonical = json.dumps(
        {
            "workflow": str(workflow_id),
            "steps": [
                {
                    "index": step.index,
                    "kind": step.kind.value,
                    "tool": step.tool_name or "",
                    "depends_on": list(step.depends_on),
                    "arguments": step.arguments,
                }
                for step in plan.steps
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkflowOutcome(str, enum.Enum):
    """What a workflow turn produced. Application state, never model output."""

    NOT_WORKFLOW = "not_workflow"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    #: Research succeeded but the artifact did not, or the reverse. The
    #: distinction from FAILED matters: a partial workflow has real results to
    #: report, and saying "it failed" would discard them.
    PARTIAL = "partial"
    FAILED = "failed"
    DECLINED = "declined"
    ABANDONED = "abandoned"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"


class StepReport(BaseModel):
    """What one step did. Built from execution records, never from a model."""

    model_config = ConfigDict(frozen=True)

    index: int
    kind: StepKind
    #: `succeeded`, `failed`, `skipped`. Taken from the execution record for
    #: executable steps, so a step is reported successful only if the
    #: executor said so.
    status: str = "pending"
    detail: str = Field(default="", max_length=200)


class WorkflowResult(BaseModel):
    """The workflow layer's report for one chat turn."""

    model_config = ConfigDict(frozen=True)

    outcome: WorkflowOutcome = WorkflowOutcome.NOT_WORKFLOW
    workflow_id: Optional[uuid.UUID] = None

    #: Application-written text to send instead of calling the model.
    reply: str = ""

    #: The rendered research, as untrusted external content. Only set when
    #: research actually returned results.
    research_block: str = ""
    result_count: int = 0

    #: What was written, when something was. A workspace-relative path.
    artifact_path: str = Field(default="", max_length=400)
    artifact_written: bool = False

    steps: Tuple[StepReport, ...] = ()
    reason: Optional[str] = Field(default=None, max_length=64)

    @property
    def has_reply(self) -> bool:
        return bool(self.reply)

    @property
    def needs_synthesis(self) -> bool:
        """Whether the model should write this turn's answer."""
        return self.outcome in (
            WorkflowOutcome.COMPLETED, WorkflowOutcome.PARTIAL
        )


__all__ = [
    "MAX_ARTIFACT_CONTENT_CHARS",
    "StepKind",
    "StepReport",
    "TOOL_FOR_KIND",
    "WorkflowOutcome",
    "WorkflowPlan",
    "WorkflowResult",
    "WorkflowStep",
    "plan_fingerprint",
]
