"""Execution lifecycle: propose, approve, revoke, run.

The four operations a client can perform, each a separate deliberate step. The
separation is the safety property -- proposing writes a record and runs
nothing, approving binds a human decision to one payload and runs nothing, and
only an explicit run request reaches the dispatcher.

Nothing here is triggered by a model. `ChatService` may *identify* that a turn
described an action (Stage 4D), and identifying is where it stops: no code
path leads from a generated reply to `create()`, let alone to `run()`.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution import approvals, audit
from app.execution.dispatcher import Dispatcher
from app.execution.errors import (
    ExecutionDisabled,
    ExecutionError,
    InvalidStateTransition,
    NotExecutable,
    ToolFailure,
    UnknownExecution,
    WorkspaceViolation,
)
from app.execution.models import Execution, ExecutionEvent, ExecutionEventType
from app.execution.schemas import (
    ExecutionOutcome,
    ExecutionRequest,
    payload_fingerprint,
)
from app.execution.states import ExecutionState, can_transition
from app.execution.tools import get_executable_registry
from app.tools.authorization import AuthorizationService
from app.tools.schemas import ActionProposal, ActionSource, AuthorizationStatus

logger = get_logger(__name__)


class ExecutionService:
    """The application's own view of the execution lifecycle."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        authorization: Optional[AuthorizationService] = None,
        dispatcher: Optional[Dispatcher] = None,
        executable: Optional[object] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._authorization = authorization or AuthorizationService()
        # Injectable so a test can exercise the lifecycle against its own
        # executors. Previously `approve` reached for the process registry
        # directly while the dispatcher took an injectable one, so the two
        # could disagree about what exists -- and a test tool could be
        # dispatched but never approved.
        self._executable = executable or get_executable_registry()
        self._dispatcher = dispatcher or Dispatcher(
            session,
            settings=self._settings,
            authorization=self._authorization,
            registry=self._executable,
        )

    # --- Propose ------------------------------------------------------------

    async def create(
        self,
        request: ExecutionRequest,
        conversation_id: Optional[uuid.UUID] = None,
    ) -> Execution:
        """Record a proposed execution. Runs nothing.

        The authorization decision is taken now and stored, so a client can be
        told immediately that an action is forbidden rather than discovering it
        after approving. It is *also* taken again at dispatch: this copy is a
        record of what was decided, never the basis for deciding.
        """
        self._require_enabled()

        decision = self._authorization.authorize(
            ActionProposal(
                tool_name=request.tool_name,
                arguments=dict(request.arguments),
                source=ActionSource.USER,
            )
        )

        # A client that supplies no key gets one derived from the payload, so
        # the same action requested twice is still recognised as one request.
        # Deriving rather than generating is the safer default: a random key
        # would make every retry a new action.
        key = request.idempotency_key or payload_fingerprint(
            decision.tool_name, dict(request.arguments)
        )

        existing = await self._by_idempotency_key(key)
        if existing is not None:
            # A retry of a request already recorded. Returning the original
            # record is what makes the endpoint safe to call twice; creating a
            # second row would let one intent become two actions.
            return existing

        execution = Execution(
            tool_name=decision.tool_name,
            arguments=dict(request.arguments),
            state=ExecutionState.PROPOSED,
            authorization_status=decision.status,
            risk_level=decision.risk_level,
            idempotency_key=key,
            # Set only when the proposal came from a chat turn. Executions
            # created through the API leave it NULL, which is also what makes
            # them unconfirmable from chat.
            conversation_id=conversation_id,
        )
        self._session.add(execution)

        try:
            await self._session.flush()
        except IntegrityError:
            # Two concurrent requests carrying the same key. The unique
            # constraint decided; the loser reads the winner's row rather than
            # retrying, so both callers see one execution.
            await self._session.rollback()
            duplicate = await self._by_idempotency_key(key)
            if duplicate is None:
                raise
            return duplicate

        await audit.record(
            self._session,
            execution.id,
            ExecutionEventType.PROPOSED,
            actor="user",
            metadata={
                "tool": execution.tool_name,
                "authorization": decision.status.value,
                "risk": decision.risk_level.value if decision.risk_level else None,
            },
        )
        return execution

    # --- Approve ------------------------------------------------------------

    async def approve(self, execution_id: uuid.UUID) -> Execution:
        """Bind a human decision to one payload, for a bounded time.

        Approving a forbidden action is refused here rather than at dispatch.
        Both refuse it, but letting a client approve something that can never
        run would be a confusing lie -- and an approval record for a forbidden
        action is exactly the artefact a later reader would misread.
        """
        self._require_enabled()
        execution = await self.get(execution_id)
        self._require_transition(execution, ExecutionState.APPROVED)

        if execution.authorization_status not in (
            AuthorizationStatus.ALLOWED,
            AuthorizationStatus.APPROVAL_REQUIRED,
        ):
            raise InvalidStateTransition(
                detail=f"authorization is {execution.authorization_status.value}"
            )

        if not self._executable.contains(execution.tool_name):
            # Approving something with no implementation would produce a live
            # grant that can never be honoured. Refuse it as a proposal, not
            # as a surprise at dispatch.
            raise NotExecutable(detail=execution.tool_name)

        now = datetime.now(timezone.utc)
        execution.state = ExecutionState.APPROVED
        execution.approved_at = now
        # The binding: this payload, and no other.
        execution.approved_fingerprint = approvals.fingerprint_for(execution)
        execution.approval_expires_at = approvals.expiry_from(
            now, self._settings.EXECUTION_APPROVAL_TTL_SECONDS
        )
        execution.updated_at = now
        await self._session.flush()

        await audit.record(
            self._session,
            execution.id,
            ExecutionEventType.APPROVED,
            actor="user",
            metadata={
                "tool": execution.tool_name,
                "expires_at": execution.approval_expires_at.isoformat(),
            },
        )
        return execution

    # --- Revoke -------------------------------------------------------------

    async def revoke(self, execution_id: uuid.UUID) -> Execution:
        """Withdraw an approval, or refuse a proposal. Immediate.

        Revocation is not itself gated on `EXECUTION_ENABLED`. Taking a
        permission away must work even when the switch that grants permissions
        is off -- refusing to revoke because execution is disabled would be the
        wrong failure direction.
        """
        execution = await self.get(execution_id)
        self._require_transition(execution, ExecutionState.REVOKED)

        now = datetime.now(timezone.utc)
        execution.state = ExecutionState.REVOKED
        execution.completed_at = now
        execution.updated_at = now
        await self._session.flush()

        await audit.record(
            self._session,
            execution.id,
            ExecutionEventType.REVOKED,
            actor="user",
            metadata={"tool": execution.tool_name},
        )
        return execution

    # --- Run ----------------------------------------------------------------

    async def run(self, execution_id: uuid.UUID) -> Execution:
        """Attempt the action, discarding the tool's returned data.

        The data is genuinely discarded rather than quietly persisted: it can
        contain content from outside, and the execution record is not where
        that belongs. A caller who needs it asks for it explicitly.
        """
        execution, _ = await self.run_returning_outcome(execution_id)
        return execution

    async def run_returning_outcome(
        self, execution_id: uuid.UUID
    ) -> Tuple[Execution, Optional[ExecutionOutcome]]:
        """Attempt the action and return what the tool produced.

        Separate from `run` so that wanting the data is a deliberate act. The
        outcome carries external content -- for a search, whole pages of it --
        and a caller that receives it by default is a caller that will
        eventually log it.

        Every gate lives in the dispatcher.

        This method's own job is bookkeeping: it records what happened,
        including the refusals. A refused attempt is a journal entry, not a
        silence -- and an execution refused before the dispatcher claimed it
        stays in the state it was already in, so a revoked action does not
        become 'failed' merely because someone tried it.
        """
        execution = await self.get(execution_id)

        try:
            outcome = await self._dispatcher.dispatch(execution)
        except ExecutionError as refusal:
            await self._record_refusal(execution, refusal)
            raise

        now = datetime.now(timezone.utc)
        execution.state = ExecutionState.SUCCEEDED
        execution.completed_at = now
        execution.updated_at = now
        execution.result_summary = outcome.summary
        await self._session.flush()

        await audit.record(
            self._session,
            execution.id,
            ExecutionEventType.EXECUTION_SUCCEEDED,
            actor="system",
            metadata={
                "tool": execution.tool_name,
                "summary": outcome.summary,
                # Safe operational facts from the executor -- for an
                # integration tool this is the integration, operation,
                # latency, attempt count and provider status. Sanitised
                # again by `audit.record` regardless of who supplied it.
                **dict(outcome.audit_metadata or {}),
            },
        )
        return execution, outcome

    async def _record_refusal(
        self, execution: Execution, refusal: ExecutionError
    ) -> None:
        """Journal a refusal, and mark the record failed only if it ran.

        The distinction is the whole point of separating these two cases:

        - `ToolFailure` / `WorkspaceViolation` mean the dispatcher claimed the
          execution and the tool was reached. The record is EXECUTING and must
          be resolved to FAILED, or it is stuck.
        - Everything else means a gate refused *before* the claim. Nothing ran,
          the record never left its prior state, and moving it to FAILED would
          record an attempt that never happened.
        """
        reached_the_tool = isinstance(refusal, (ToolFailure, WorkspaceViolation))

        if reached_the_tool and execution.state is ExecutionState.EXECUTING:
            now = datetime.now(timezone.utc)
            execution.state = ExecutionState.FAILED
            execution.completed_at = now
            execution.updated_at = now
            execution.error_code = refusal.reason
            await self._session.flush()

        await audit.record(
            self._session,
            execution.id,
            ExecutionEventType.EXECUTION_FAILED if reached_the_tool
            else ExecutionEventType.REFUSED,
            actor="system",
            metadata={
                "tool": execution.tool_name,
                "reason": refusal.reason,
                # `detail` is application text -- a field name, a state name, a
                # tool name -- never a path, a value or an exception message.
                "detail": refusal.detail,
                "state": execution.state.value,
            },
        )

        # Committed here, before the refusal propagates.
        #
        # This is deliberate and it is the whole reason the method exists. The
        # request handler re-raises, and the session dependency rolls back a
        # failed request -- which would take this journal entry with it. An
        # audit trail that disappears whenever something is refused records
        # only the attempts that were permitted, which is the opposite of what
        # it is for: the refused attempt is the interesting one.
        #
        # The same rollback would also have undone the EXECUTING -> FAILED
        # transition above, leaving a record stuck in APPROVED that reads as
        # though it was never tried.
        await self._session.commit()

    # --- Reads --------------------------------------------------------------

    async def get(self, execution_id: uuid.UUID) -> Execution:
        execution = await self._session.get(Execution, execution_id)
        if execution is None:
            raise UnknownExecution(detail=str(execution_id))
        return execution

    async def history(self, execution_id: uuid.UUID):
        """The journal for one execution, oldest first."""
        await self.get(execution_id)
        result = await self._session.execute(
            select(ExecutionEvent)
            .where(ExecutionEvent.execution_id == execution_id)
            .order_by(ExecutionEvent.sequence)
        )
        return list(result.scalars())

    # --- Internals ----------------------------------------------------------

    async def _by_idempotency_key(self, key: str) -> Optional[Execution]:
        result = await self._session.execute(
            select(Execution).where(Execution.idempotency_key == key)
        )
        return result.scalars().first()

    def _require_enabled(self) -> None:
        if not self._settings.EXECUTION_ENABLED:
            raise ExecutionDisabled()

    def _require_transition(
        self, execution: Execution, target: ExecutionState
    ) -> None:
        """One table decides every legal move. See `states.ALLOWED_TRANSITIONS`."""
        if not can_transition(execution.state, target):
            raise InvalidStateTransition(
                detail=f"{execution.state.value} -> {target.value}"
            )


__all__ = ["ExecutionService"]
