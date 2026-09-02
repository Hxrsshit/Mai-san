"""Execution request, result and API schemas.

The separation that matters here is between what a **client** may send and
what the **application** decides. A client supplies a tool name, arguments and
an idempotency key. Everything else -- state, authorization, risk, timestamps,
success -- is computed, and none of it is a field on any request model, so
there is nothing for a forged value to land in.
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.execution.states import ExecutionState
from app.tools.schemas import AuthorizationStatus, RiskLevel

#: Bounds on what a client may send.
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_ARGUMENT_KEYS = 20


def payload_fingerprint(tool_name: str, arguments: Dict[str, Any]) -> str:
    """A deterministic hash of exactly what would run.

    This is what makes approval integrity-bound. The fingerprint is taken when
    approval is granted and recomputed before execution; if the tool or any
    argument changed in between, the two differ and the approval no longer
    applies.

    `sort_keys` makes it independent of dictionary ordering, so a re-serialised
    but identical payload still matches. `default=str` keeps it total -- an
    unexpected value type produces a different fingerprint rather than an
    exception, which fails closed.
    """
    canonical = json.dumps(
        {"tool": (tool_name or "").strip().lower(), "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ExecutionOutcome(BaseModel):
    """What a tool returns when it succeeded.

    A structured summary, not raw output. Tool output can be arbitrarily large
    and can contain whatever the workspace contains, so what is *persisted* is
    the summary; `data` is returned to the caller in the response and is not
    written to the execution record.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = Field(..., max_length=500)
    data: Dict[str, Any] = Field(default_factory=dict)

    #: Safe operational facts for the audit journal -- an integration name,
    #: an operation, a latency, an attempt count, a status code.
    #:
    #: Separate from `data` because the two go to different places and have
    #: different rules. `data` is returned to the caller and may contain
    #: content from outside; this is persisted forever and may not. It passes
    #: through `audit.sanitise` regardless, so a mistake here is bounded.
    audit_metadata: Dict[str, Any] = Field(default_factory=dict)


# --- Requests: everything a client may send ---------------------------------


class ExecutionRequest(BaseModel):
    """Create an execution record from a proposed action.

    `extra="forbid"`, not `ignore`. Elsewhere in this codebase an invented
    field is noise to drop; here it would be an attempt to set application
    state on an operation with a real side effect, and refusing says so.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1, max_length=64)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    #: Supplied by the client so a retry is recognisable as the same request.
    #: Omitted, one is derived from the payload -- see `ExecutionService`.
    idempotency_key: Optional[str] = Field(
        default=None, max_length=MAX_IDEMPOTENCY_KEY_LENGTH
    )


class ApprovalRequest(BaseModel):
    """Approve one execution. Carries no authority of its own.

    There is no `approved`, `state`, `authorization` or `expires_at` field. The
    act of calling the endpoint is the approval; what it approves is fixed by
    the execution record, not by anything in this body.
    """

    model_config = ConfigDict(extra="forbid")

    #: Optional free text recorded in the audit trail.
    note: Optional[str] = Field(default=None, max_length=300)


class RevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=300)


class ExecuteRequest(BaseModel):
    """Run an approved execution.

    The idempotency key is echoed back rather than re-supplied: a client that
    retries sends the same execution id, and the record already carries its
    key.
    """

    model_config = ConfigDict(extra="forbid")


# --- Responses: what the application reports --------------------------------


class ExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    state: ExecutionState
    authorization_status: AuthorizationStatus
    risk_level: Optional[RiskLevel] = None
    requires_approval: bool

    arguments: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str

    approved_at: Optional[datetime] = None
    approval_expires_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    result_summary: Optional[str] = None
    error_code: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    #: A sentence the application is willing to stand behind for this state.
    #: Derived from `state` alone -- see `app.execution.truthfulness`.
    statement: str = ""
    #: True only in SUCCEEDED. Present so a client never has to infer it.
    succeeded: bool = False


class ExecutionEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    actor: str
    occurred_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExecutionHistoryRead(BaseModel):
    execution_id: uuid.UUID
    events: List[ExecutionEventRead] = Field(default_factory=list)
    total: int = 0


class ExecutionResultRead(BaseModel):
    """The response to an execute call: the record plus any returned data."""

    execution: ExecutionRead
    #: Present only on success. Not persisted -- see `ExecutionOutcome`.
    data: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ApprovalRequest",
    "ExecuteRequest",
    "ExecutionEventRead",
    "ExecutionHistoryRead",
    "ExecutionOutcome",
    "ExecutionRead",
    "ExecutionRequest",
    "ExecutionResultRead",
    "RevocationRequest",
    "payload_fingerprint",
]
