"""What a client is told about a workflow.

Deliberately small. A client needs to distinguish a proposal from an answer,
and to know whether a file was actually written -- and nothing else. Absent by
design: the approval fingerprint, the stored plan, execution ids, the
workspace root, and any absolute path.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.workflows.schemas import WorkflowOutcome, WorkflowResult


class WorkflowRead(BaseModel):
    """One turn's workflow state, as the wire sees it."""

    model_config = ConfigDict(frozen=True)

    outcome: WorkflowOutcome
    #: Whether a file was written. Set from the execution record, so it is
    #: never a prediction -- the same rule `ResearchRead.searched` follows.
    artifact_written: bool = False
    #: The workspace-relative name the user was shown when they approved.
    #: Never an absolute path: where the workspace lives is not the client's
    #: business, and a leaked root is a leaked deployment detail.
    artifact_path: str = Field(default="", max_length=400)
    result_count: int = 0
    reason: Optional[str] = None

    @classmethod
    def from_result(cls, result: WorkflowResult) -> Optional["WorkflowRead"]:
        """None when the turn had nothing to do with a workflow.

        `None` rather than an object full of falsy fields, so a client cannot
        mistake "no workflow" for "a workflow that did nothing".
        """
        if result is None or result.outcome is WorkflowOutcome.NOT_WORKFLOW:
            return None
        return cls(
            outcome=result.outcome,
            artifact_written=result.artifact_written,
            artifact_path=result.artifact_path,
            result_count=result.result_count,
            reason=result.reason,
        )


__all__ = ["WorkflowRead"]
