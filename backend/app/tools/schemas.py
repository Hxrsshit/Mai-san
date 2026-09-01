"""Stage 4C tool and authorization schemas.

Three kinds of type, and the boundaries between them are the stage:

- `ToolDefinition` — what the **application** declares about a capability.
  Registered in code, frozen, and the only source of risk and approval facts.
- `ActionProposal` — what a **model, plan or user** may propose. Parsed from
  untrusted input. It carries a tool name and arguments, and nothing else.
- `AuthorizationDecision` — what the **policy** concluded. Every field is
  computed from the registry and application policy.

The security design is the same one Stages 4A and 4B use, applied to authority
over actions: **a proposal has no field that could authorise anything.** There
is no `approved`, no `requires_approval`, no `risk_level` on `ActionProposal`.
A model cannot set what it cannot name.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Bounds on proposal input. Arguments come from model output, so they are
#: capped before validation rather than after.
MAX_TOOL_NAME_LENGTH = 64
MAX_ARGUMENT_KEYS = 20
MAX_ARGUMENT_KEY_LENGTH = 64
MAX_ARGUMENT_VALUE_CHARS = 4000


class ToolCategory(str, Enum):
    """What kind of capability a tool is. Metadata only.

    A category grants nothing. It exists so policy can be expressed over
    groups ("no communication tools yet") without enumerating every tool, and
    so a human reading the registry can see its shape at a glance.
    """

    INFORMATION = "information"
    COMMUNICATION = "communication"
    FILE_OPERATION = "file_operation"
    SYSTEM = "system"
    CREATIVE = "creative"
    #: Framework-only tools with no real capability. See `catalog.py`.
    DIAGNOSTIC = "diagnostic"


class RiskLevel(str, Enum):
    """How much damage a capability could do if misused.

    Application-defined, set at registration, and never derived from anything
    a model or a user said. The ordering below is load-bearing: policy
    compares risk levels, so they must be totally ordered.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


#: Severity order, lowest first. Used by policy comparisons.
RISK_ORDER: List[RiskLevel] = [
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.CRITICAL,
]


def risk_rank(level: RiskLevel) -> int:
    return RISK_ORDER.index(level)


class ExecutionMode(str, Enum):
    """How a tool would run -- once anything can run.

    Stage 4C admitted only `UNAVAILABLE`: nothing could run, so a definition
    claiming otherwise would have been false. Stage 4E supplies an executor
    for three workspace tools, so `SYNCHRONOUS` became true for those and the
    validator was relaxed exactly that far.

    `BACKGROUND` is still refused, and not merely because it is unimplemented.
    Background execution means an action that runs without someone waiting for
    it, which is the shape of an autonomous loop; Stage 4E forbids that
    outright. It stays unreachable until a stage argues for it on its own
    merits rather than inheriting permission from this one.
    """

    UNAVAILABLE = "unavailable"
    SYNCHRONOUS = "synchronous"
    BACKGROUND = "background"


class ToolDefinition(BaseModel):
    """An application-declared capability.

    Frozen. The registry hands these out directly, so immutability is what
    makes "registry metadata cannot be mutated through request data" true
    rather than merely intended -- a caller holding one cannot change it, and
    therefore cannot change what the registry believes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Canonical identifier. Lowercase, exact-matched, never fuzzy-matched.
    name: str = Field(..., min_length=1, max_length=MAX_TOOL_NAME_LENGTH)
    description: str = Field(..., min_length=1, max_length=300)
    category: ToolCategory
    risk_level: RiskLevel

    #: Whether a human must confirm before this tool could ever run. Policy
    #: may raise this to true; nothing can lower it.
    requires_approval: bool = True

    #: UNAVAILABLE unless an executor exists for this tool. See
    #: `ExecutionMode`; BACKGROUND is refused by the validator below.
    execution_mode: ExecutionMode = ExecutionMode.UNAVAILABLE

    #: An operator switch. A disabled tool is registered but forbidden, which
    #: is how a capability can exist in the catalogue before it is trusted.
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _canonical_name(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("tool name cannot be blank")
        if not all(char.isalnum() or char in "_-" for char in cleaned):
            raise ValueError(
                "tool name must contain only letters, digits, '-' and '_'"
            )
        return cleaned

    @field_validator("execution_mode")
    @classmethod
    def _no_background_execution(cls, value: ExecutionMode) -> ExecutionMode:
        """A definition may not claim a mode the application cannot honour.

        Stage 4C's rule was "only UNAVAILABLE", because nothing could run and
        a definition saying it could would be a lie the registry then repeats
        to every later reader. Stage 4E made `SYNCHRONOUS` true for three
        workspace tools, so the rule narrowed to what is still false.

        BACKGROUND stays refused: it describes an action running with nobody
        waiting on it, and Stage 4E permits no autonomous or self-directed
        execution. The invariant is unchanged -- a tool may not declare a mode
        the application cannot honour -- only the set of honourable modes grew.
        """
        if value is ExecutionMode.BACKGROUND:
            raise ValueError(
                "background execution does not exist; no tool may declare it"
            )
        return value


class ActionSource(str, Enum):
    """Where a proposal came from. Informational, never authoritative.

    Recorded so a decision can be explained later. A proposal from `USER` is
    treated exactly like one from `MODEL`: the source is a fact about the
    proposal's history, not a claim about its legitimacy.
    """

    USER = "user"
    MODEL = "model"
    PLAN = "plan"
    SYSTEM = "system"


class ActionProposal(BaseModel):
    """A request that some tool be used. Not authorisation.

    `extra="ignore"`: a proposal carrying `approved`, `requires_approval` or
    `risk_level` has those dropped before anything reads them. They are not
    fields here, so there is no parsing order, no validator precedence and no
    future refactor that could let one through.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    tool_name: str = Field(..., min_length=1, max_length=MAX_TOOL_NAME_LENGTH)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    source: ActionSource = ActionSource.MODEL

    #: Free text describing why this was proposed. Data, never instructions.
    rationale: Optional[str] = Field(default=None, max_length=300)

    @field_validator("tool_name")
    @classmethod
    def _normalise(cls, value: str) -> str:
        """Strip and lowercase. Nothing else.

        Deliberately not fuzzy. `delete_all_files` must not become
        `delete_file`, so the only transformations are ones that cannot change
        which tool is meant.
        """
        return value.strip().lower()

    @field_validator("arguments")
    @classmethod
    def _bound_arguments(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """Cap the argument bag before any tool schema sees it."""
        if len(value) > MAX_ARGUMENT_KEYS:
            raise ValueError(f"at most {MAX_ARGUMENT_KEYS} arguments")
        for key in value:
            if not isinstance(key, str) or len(key) > MAX_ARGUMENT_KEY_LENGTH:
                raise ValueError("argument keys must be short strings")
        return value


class AuthorizationStatus(str, Enum):
    """The four outcomes. Explicit states, never a bare boolean.

    `ALLOWED` means *the authorization layer does not forbid this*. It does not
    mean "execute". There is nothing in Stage 4C that could act on it.
    """

    UNKNOWN_TOOL = "unknown_tool"
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"
    ALLOWED = "allowed"


#: Restrictiveness order, least restrictive first. Policy takes the maximum
#: over every rule that fired, which is what makes authorization monotonic:
#: a rule can only ever tighten the outcome.
STATUS_ORDER: List[AuthorizationStatus] = [
    AuthorizationStatus.ALLOWED,
    AuthorizationStatus.APPROVAL_REQUIRED,
    AuthorizationStatus.FORBIDDEN,
    AuthorizationStatus.UNKNOWN_TOOL,
]


def status_rank(status: AuthorizationStatus) -> int:
    return STATUS_ORDER.index(status)


def most_restrictive(*statuses: AuthorizationStatus) -> AuthorizationStatus:
    """The tightest of several outcomes. Where policies overlap, this wins."""
    return max(statuses, key=status_rank)


class DenialReason:
    """Why a proposal was refused or gated.

    Application constants. Nothing here derives from model output or user
    text, so a decision cannot be used to echo an attacker's words back.
    """

    UNKNOWN_TOOL = "no_such_tool"
    EMPTY_NAME = "empty_tool_name"
    TOOL_DISABLED = "tool_disabled"
    CATEGORY_FORBIDDEN = "category_not_permitted"
    RISK_FORBIDDEN = "risk_level_not_permitted"
    NO_EXECUTION_CAPABILITY = "no_execution_capability_exists"
    INTENT_FORBIDS = "intent_carries_no_execution_capability"
    INVALID_ARGUMENTS = "arguments_failed_validation"
    APPROVAL_REQUIRED = "human_approval_required"
    HIGH_RISK_APPROVAL = "high_risk_requires_approval"
    ALLOWED = "not_forbidden_by_policy"


class AuthorizationDecision(BaseModel):
    """What the policy concluded. Every field computed, none parsed.

    Frozen: a decision cannot be edited into a permission after the fact.
    """

    model_config = ConfigDict(frozen=True)

    status: AuthorizationStatus
    reason: str

    #: The canonical name that was looked up. Echoing it back lets a caller
    #: see what was actually resolved, which matters when normalisation
    #: changed the input.
    tool_name: str

    #: Registry facts, repeated for the caller's convenience. Absent when the
    #: tool is unknown -- there is nothing authoritative to report.
    category: Optional[ToolCategory] = None
    risk_level: Optional[RiskLevel] = None

    #: True whenever a human would have to confirm. Always true when the
    #: status is APPROVAL_REQUIRED, and never lowered by any input.
    requires_approval: bool = True

    #: Validated arguments, when a tool declared a schema and they passed.
    #: A separate field from the proposal's raw bag, so nothing unvalidated
    #: can be mistaken for something checked.
    validated_arguments: Optional[Dict[str, Any]] = None

    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def is_allowed(self) -> bool:
        """True only for ALLOWED. Descriptive; nothing branches on it to act."""
        return self.status is AuthorizationStatus.ALLOWED

    @property
    def is_refused(self) -> bool:
        return self.status in {
            AuthorizationStatus.UNKNOWN_TOOL,
            AuthorizationStatus.FORBIDDEN,
        }


class ToolResultStatus(str, Enum):
    """Outcome of a future tool run. No tool produces one in Stage 4C."""

    SUCCESS = "success"
    FAILURE = "failure"


class ToolResult(BaseModel):
    """The output contract future tools must satisfy.

    Defined now so the shape is fixed before anything can produce one. The
    important property is the one it inherits from Stage 3B: a tool result is
    **data**. When a later stage feeds one into a prompt it must travel the
    same reference-data path retrieved knowledge does, never as an
    instruction.

    Nothing in Stage 4C constructs one outside its own tests.
    """

    model_config = ConfigDict(frozen=True)

    status: ToolResultStatus
    tool_name: str
    data: Optional[Dict[str, Any]] = None
    #: Operational detail: timings, counts. Never authority.
    metadata: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


# --- API views --------------------------------------------------------------


class ToolRead(BaseModel):
    """A registered tool, as shown by the API."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    category: ToolCategory
    risk_level: RiskLevel
    requires_approval: bool
    execution_mode: ExecutionMode
    enabled: bool


class ToolListRead(BaseModel):
    items: List[ToolRead] = Field(default_factory=list)
    total: int = 0


class AuthorizationRequest(BaseModel):
    """Body of the authorization dry-run endpoint."""

    tool_name: str = Field(..., min_length=1, max_length=MAX_TOOL_NAME_LENGTH)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    source: ActionSource = ActionSource.USER


class AuthorizationRead(BaseModel):
    status: AuthorizationStatus
    reason: str
    tool_name: str
    category: Optional[ToolCategory] = None
    risk_level: Optional[RiskLevel] = None
    requires_approval: bool

    @classmethod
    def from_decision(cls, decision: AuthorizationDecision) -> "AuthorizationRead":
        return cls(
            status=decision.status,
            reason=decision.reason,
            tool_name=decision.tool_name,
            category=decision.category,
            risk_level=decision.risk_level,
            requires_approval=decision.requires_approval,
        )


__all__ = [
    "ActionProposal",
    "ActionSource",
    "AuthorizationDecision",
    "AuthorizationRead",
    "AuthorizationRequest",
    "AuthorizationStatus",
    "DenialReason",
    "ExecutionMode",
    "RISK_ORDER",
    "RiskLevel",
    "STATUS_ORDER",
    "ToolCategory",
    "ToolDefinition",
    "ToolListRead",
    "ToolRead",
    "ToolResult",
    "ToolResultStatus",
    "most_restrictive",
    "risk_rank",
    "status_rank",
]
