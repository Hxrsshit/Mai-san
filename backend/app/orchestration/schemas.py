"""Stage 4D orchestration schemas.

The pipeline's typed vocabulary:

- `ActionCandidate` — something the application's own matcher recognised in a
  message. Produced by application code, never by a model.
- `ProposalOutcome` — one candidate carried all the way to a Stage 4C
  decision, with its decision attached.
- `OrchestrationResult` — what the whole pass concluded, as one explicit
  outcome.

**Nothing here can execute, and nothing here claims anything did.** There is no
`executed` field, no `completed` state and no `result` from a tool, because
Stage 4D has no way to produce one. The outcome enum's most permissive member
is named `action_allowed_not_executed` for exactly that reason: the state it
describes is "policy would permit this, and it did not happen".
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tools.schemas import (
    ActionSource,
    AuthorizationStatus,
    RiskLevel,
    ToolCategory,
)

#: The most proposals one request may produce.
#:
#: Five, as the specification suggests. The bound exists because candidates
#: come from matching over a message the user controls: without it, a message
#: naming every tool repeatedly would produce an unbounded authorization pass.
#: In practice a request implying more than a couple of distinct actions is
#: ambiguous, and ambiguity resolves to no action rather than to many.
MAX_PROPOSALS = 5


class ActionOutcome(str, Enum):
    """What an orchestration pass concluded. Explicit states, never a boolean.

    Two "nothing happened" states rather than one, because the difference is
    operationally important: `NOT_ELIGIBLE` means the pipeline never looked,
    and `NO_ACTION` means it looked and found nothing. Only the first is
    evidence that gating worked.
    """

    #: The intent did not warrant looking for an action. No work was done.
    NOT_ELIGIBLE = "not_eligible"
    #: Eligible, but nothing in the message matched a known capability.
    NO_ACTION = "no_action"
    #: Something was proposed that names no registered tool.
    ACTION_UNKNOWN = "action_unknown"
    #: Policy refuses it.
    ACTION_FORBIDDEN = "action_forbidden"
    #: A human would have to confirm before anything could happen.
    ACTION_REQUIRES_APPROVAL = "action_requires_approval"
    #: Policy does not forbid it -- and it did not happen.
    ACTION_ALLOWED_NOT_EXECUTED = "action_allowed_not_executed"


#: Restrictiveness order, least restrictive first. The overall outcome is the
#: most restrictive across every proposal, so one refused action is never
#: hidden behind another that was permitted.
OUTCOME_ORDER: List[ActionOutcome] = [
    ActionOutcome.ACTION_ALLOWED_NOT_EXECUTED,
    ActionOutcome.ACTION_REQUIRES_APPROVAL,
    ActionOutcome.ACTION_FORBIDDEN,
    ActionOutcome.ACTION_UNKNOWN,
]


def outcome_rank(outcome: ActionOutcome) -> int:
    return OUTCOME_ORDER.index(outcome)


#: How a Stage 4C decision maps onto an orchestration outcome. Total over the
#: four statuses, so a new status cannot be silently dropped.
STATUS_TO_OUTCOME: Dict[AuthorizationStatus, ActionOutcome] = {
    AuthorizationStatus.ALLOWED: ActionOutcome.ACTION_ALLOWED_NOT_EXECUTED,
    AuthorizationStatus.APPROVAL_REQUIRED: ActionOutcome.ACTION_REQUIRES_APPROVAL,
    AuthorizationStatus.FORBIDDEN: ActionOutcome.ACTION_FORBIDDEN,
    AuthorizationStatus.UNKNOWN_TOOL: ActionOutcome.ACTION_UNKNOWN,
}


class IneligibilityReason:
    """Why a turn never entered the action path. Application constants."""

    DISABLED = "orchestration_disabled"
    EMPTY_MESSAGE = "empty_message"
    NO_INTENT = "intent_not_classified"
    INTENT_NOT_ACTION_CAPABLE = "intent_is_not_action_capable"
    NOTHING_MATCHED = "no_known_capability_matched"
    AMBIGUOUS = "request_too_ambiguous_to_act_on"


class ActionCandidate(BaseModel):
    """A capability the application's own matcher recognised in a message.

    Produced by `matching.py` from an application-authored phrase table. A
    model never produces one, so a model cannot name a tool that was not
    already registered -- the matcher can only emit names it was given.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    arguments: Dict[str, object] = Field(default_factory=dict)
    #: Where in the normalised message the phrase matched. Used only to make
    #: ordering deterministic.
    matched_at: int = 0
    #: The phrase that matched, for explanation. Application text, not user text.
    matched_phrase: str = ""


class ProposalOutcome(BaseModel):
    """One proposal and the Stage 4C decision it received.

    Every proposal is authorized independently: one decision never influences
    another, and an approval requirement on one action never covers a second.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    source: ActionSource
    outcome: ActionOutcome
    status: AuthorizationStatus
    reason: str
    category: Optional[ToolCategory] = None
    risk_level: Optional[RiskLevel] = None
    requires_approval: bool = True
    matched_phrase: str = ""

    @property
    def was_executed(self) -> bool:
        """Always False. Present so the answer is explicit rather than absent.

        Stage 4D has no executor, so there is no state in which this could be
        true. It exists to be asserted against.
        """
        return False


class OrchestrationResult(BaseModel):
    """What one orchestration pass concluded.

    Frozen. A result cannot be edited into a claim that something ran.
    """

    model_config = ConfigDict(frozen=True)

    outcome: ActionOutcome
    proposals: List[ProposalOutcome] = Field(default_factory=list)

    #: Why nothing was proposed, when nothing was. An application constant.
    reason: Optional[str] = None
    #: What the user could say to make an ambiguous request actionable.
    clarification_needed: Optional[str] = None

    #: Candidates the bound discarded. Recorded so truncation is visible.
    candidates_discarded: int = 0
    #: Candidates removed as exact duplicates of an earlier one.
    duplicates_removed: int = 0

    #: Model calls this pass made. Always zero: identification is deterministic.
    model_calls: int = Field(default=0, ge=0, le=0)
    duration_ms: float = 0.0

    @property
    def acted(self) -> bool:
        """Always False. Nothing in Stage 4D can act.

        A property rather than a field, so it cannot be set by validation,
        deserialisation, or a future refactor that adds a constructor argument.
        """
        return False

    @property
    def has_proposals(self) -> bool:
        return bool(self.proposals)


# --- API views --------------------------------------------------------------


class ProposalRead(BaseModel):
    tool_name: str
    source: ActionSource
    outcome: ActionOutcome
    status: AuthorizationStatus
    reason: str
    risk_level: Optional[RiskLevel] = None
    requires_approval: bool
    #: Always false. Stated on the wire so a client cannot infer completion.
    executed: bool = False


class OrchestrationRead(BaseModel):
    outcome: ActionOutcome
    proposals: List[ProposalRead] = Field(default_factory=list)
    reason: Optional[str] = None
    clarification_needed: Optional[str] = None
    #: Always false, for the same reason.
    executed: bool = False

    @classmethod
    def from_result(cls, result: OrchestrationResult) -> "OrchestrationRead":
        return cls(
            outcome=result.outcome,
            proposals=[
                ProposalRead(
                    tool_name=proposal.tool_name,
                    source=proposal.source,
                    outcome=proposal.outcome,
                    status=proposal.status,
                    reason=proposal.reason,
                    risk_level=proposal.risk_level,
                    requires_approval=proposal.requires_approval,
                    executed=False,
                )
                for proposal in result.proposals
            ],
            reason=result.reason,
            clarification_needed=result.clarification_needed,
            executed=False,
        )


class OrchestrationDebugRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


__all__ = [
    "MAX_PROPOSALS",
    "OUTCOME_ORDER",
    "STATUS_TO_OUTCOME",
    "ActionCandidate",
    "ActionOutcome",
    "IneligibilityReason",
    "OrchestrationDebugRequest",
    "OrchestrationRead",
    "OrchestrationResult",
    "ProposalOutcome",
    "ProposalRead",
    "outcome_rank",
]
