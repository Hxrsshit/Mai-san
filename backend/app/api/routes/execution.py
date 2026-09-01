"""Controlled execution endpoints.

Four separate calls, deliberately not one:

    POST /api/executions                  propose  -- records, runs nothing
    POST /api/executions/{id}/approve     approve  -- binds, runs nothing
    POST /api/executions/{id}/revoke      revoke   -- withdraws
    POST /api/executions/{id}/execute     run      -- the only one with effect

There is no combined "propose and run" endpoint, and adding one would defeat
the design: the gap between approving and running is where a human decision
lives, and collapsing it would turn approval into a formality.

Nothing here is reachable by a model. These are HTTP endpoints a person's
client calls; no generated text is routed to them, and the chat path has no
code that constructs one of these requests.
"""

import uuid

from fastapi import APIRouter, status

from app.api.deps import Executions
from app.core.errors import MaiError
from app.execution.errors import (
    AlreadyRunning,
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalRequired,
    ApprovalRevoked,
    ExecutionDisabled,
    ExecutionError,
    InvalidStateTransition,
    NotAuthorized,
    NotExecutable,
    ToolFailure,
    UnknownExecution,
    WorkspaceViolation,
)
from app.execution.schemas import (
    ApprovalRequest,
    ExecuteRequest,
    ExecutionEventRead,
    ExecutionHistoryRead,
    ExecutionRead,
    ExecutionRequest,
    ExecutionResultRead,
    RevocationRequest,
)
from app.execution.truthfulness import statement_for, succeeded
from app.tools.schemas import AuthorizationStatus

router = APIRouter(prefix="/api/executions", tags=["executions"])

#: Refusal -> HTTP status. A table rather than a chain of `isinstance`, so a
#: new refusal type has to be given a status deliberately.
#:
#: The default is 403 and not 500: an unmapped refusal is still a refusal, and
#: reporting one as a server error would suggest the action might have
#: happened. Every one of these means it did not.
_STATUS_BY_ERROR = {
    ExecutionDisabled: status.HTTP_503_SERVICE_UNAVAILABLE,
    UnknownExecution: status.HTTP_404_NOT_FOUND,
    InvalidStateTransition: status.HTTP_409_CONFLICT,
    AlreadyRunning: status.HTTP_409_CONFLICT,
    ApprovalRequired: status.HTTP_403_FORBIDDEN,
    ApprovalInvalid: status.HTTP_403_FORBIDDEN,
    ApprovalExpired: status.HTTP_403_FORBIDDEN,
    ApprovalRevoked: status.HTTP_403_FORBIDDEN,
    NotAuthorized: status.HTTP_403_FORBIDDEN,
    NotExecutable: status.HTTP_400_BAD_REQUEST,
    WorkspaceViolation: status.HTTP_400_BAD_REQUEST,
    ToolFailure: status.HTTP_400_BAD_REQUEST,
}

_DEFAULT_REFUSAL_STATUS = status.HTTP_403_FORBIDDEN


class ExecutionRefused(MaiError):
    """An execution refusal, in the application's standard error envelope.

    Carries the refusal's `reason` code and nothing else. `detail` is
    developer context -- a state name, a field name -- and stays in the log
    rather than the response, so a refusal cannot echo a path or a value back
    to whoever probed for it.
    """

    def __init__(self, refusal: ExecutionError) -> None:
        self.status_code = _STATUS_BY_ERROR.get(
            type(refusal), _DEFAULT_REFUSAL_STATUS
        )
        self.code = refusal.reason
        super().__init__(_MESSAGES.get(refusal.reason, "This action was refused."))


#: Human-readable text per reason. Written out rather than derived from the
#: code so each one says something useful to a person reading it.
_MESSAGES = {
    "execution_disabled": (
        "Execution is switched off for this deployment. Nothing was run."
    ),
    "no_such_execution": "No such execution.",
    "invalid_state_transition": (
        "This execution is not in a state where that is possible."
    ),
    "approval_required": "This execution has not been approved.",
    "approval_does_not_match_this_action": (
        "The approval was granted for a different action. Nothing was run."
    ),
    "approval_expired": "The approval has expired. Nothing was run.",
    "approval_revoked": "The approval was revoked. Nothing was run.",
    "not_authorized": "This action is not permitted.",
    "tool_is_not_executable": (
        "This tool has no implementation and cannot be run."
    ),
    "execution_already_claimed": (
        "This execution has already been claimed by another attempt."
    ),
    "path_outside_workspace": (
        "That path is outside the workspace. Nothing was run."
    ),
    "tool_execution_failed": "The action was attempted and did not complete.",
}


def _read(execution) -> ExecutionRead:
    """Project a record for the client.

    `requires_approval` is derived from the authorization status, never stored
    and never taken from a request: a client cannot mark its own action as one
    that needs no approval.
    """
    return ExecutionRead(
        id=execution.id,
        tool_name=execution.tool_name,
        state=execution.state,
        authorization_status=execution.authorization_status,
        risk_level=execution.risk_level,
        requires_approval=(
            execution.authorization_status is not AuthorizationStatus.ALLOWED
        ),
        arguments=dict(execution.arguments or {}),
        idempotency_key=execution.idempotency_key,
        approved_at=execution.approved_at,
        approval_expires_at=execution.approval_expires_at,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        result_summary=execution.result_summary,
        error_code=execution.error_code,
        # Both derived from `state` alone. Neither is stored, and no caller
        # can supply either -- so a response cannot claim an outcome the
        # record does not support.
        statement=statement_for(execution.state),
        succeeded=succeeded(execution.state),
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


@router.post(
    "",
    response_model=ExecutionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Propose an action. Records it; runs nothing.",
)
async def create_execution(
    payload: ExecutionRequest, executions: Executions
) -> ExecutionRead:
    """Record a proposed action and report how it was authorized.

    Returning `201` here means a *record* was created. It does not mean
    anything ran, and for an action requiring approval the state will be
    `proposed` with `requires_approval` true.
    """
    try:
        execution = await executions.create(payload)
    except ExecutionError as refusal:
        raise ExecutionRefused(refusal) from refusal
    return _read(execution)


@router.post(
    "/{execution_id}/approve",
    response_model=ExecutionRead,
    summary="Approve one action, bound to its exact payload, for a limited time",
)
async def approve_execution(
    execution_id: uuid.UUID, payload: ApprovalRequest, executions: Executions
) -> ExecutionRead:
    """Grant approval. Still runs nothing.

    The approval is bound to a fingerprint of the tool and arguments as they
    are now, and expires. Changing the payload afterwards invalidates it.
    """
    try:
        execution = await executions.approve(execution_id)
    except ExecutionError as refusal:
        raise ExecutionRefused(refusal) from refusal
    return _read(execution)


@router.post(
    "/{execution_id}/revoke",
    response_model=ExecutionRead,
    summary="Withdraw an approval or refuse a proposal",
)
async def revoke_execution(
    execution_id: uuid.UUID, payload: RevocationRequest, executions: Executions
) -> ExecutionRead:
    """Revoke. Takes effect immediately and cannot be undone.

    The one operation the service does not gate on `EXECUTION_ENABLED`:
    removing a permission must not depend on the switch that grants them.
    Turning the switch off is itself stronger than revoking -- nothing can
    dispatch -- so a deployment with execution off exposes no route here at
    all, and has nothing to revoke.
    """
    try:
        execution = await executions.revoke(execution_id)
    except ExecutionError as refusal:
        raise ExecutionRefused(refusal) from refusal
    return _read(execution)


@router.post(
    "/{execution_id}/execute",
    response_model=ExecutionResultRead,
    summary="Run an approved action. The only endpoint with a side effect.",
)
async def execute_execution(
    execution_id: uuid.UUID, payload: ExecuteRequest, executions: Executions
) -> ExecutionResultRead:
    """Attempt the action.

    Every gate is re-checked here, including the authorization decision taken
    at proposal time: policy may have changed, and the question that matters
    is whether this is permitted now.
    """
    try:
        execution = await executions.run(execution_id)
    except ExecutionError as refusal:
        raise ExecutionRefused(refusal) from refusal

    return ExecutionResultRead(execution=_read(execution))


@router.get(
    "/{execution_id}",
    response_model=ExecutionRead,
    summary="The current state of one execution",
)
async def read_execution(
    execution_id: uuid.UUID, executions: Executions
) -> ExecutionRead:
    try:
        execution = await executions.get(execution_id)
    except ExecutionError as refusal:
        raise ExecutionRefused(refusal) from refusal
    return _read(execution)


@router.get(
    "/{execution_id}/history",
    response_model=ExecutionHistoryRead,
    summary="The append-only journal for one execution",
)
async def read_execution_history(
    execution_id: uuid.UUID, executions: Executions
) -> ExecutionHistoryRead:
    """Every recorded event, oldest first, including refused attempts.

    Refusals are part of the history. An audit trail that showed only what
    succeeded would answer the wrong question.
    """
    try:
        events = await executions.history(execution_id)
    except ExecutionError as refusal:
        raise ExecutionRefused(refusal) from refusal

    return ExecutionHistoryRead(
        execution_id=execution_id,
        events=[
            ExecutionEventRead(
                id=event.id,
                event_type=event.event_type.value,
                actor=event.actor,
                occurred_at=event.occurred_at,
                metadata=dict(event.event_metadata or {}),
            )
            for event in events
        ],
        total=len(events),
    )
