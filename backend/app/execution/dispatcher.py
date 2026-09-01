"""The bounded dispatcher.

The only code in Mai that causes a side effect, and the narrowest thing that
could do the job.

Every gate, in order, before a tool is touched:

    execution enabled?      configuration, default off
    state is APPROVED?      the only runnable state; PROPOSED has no edge here
    approval still valid?   fingerprint matches, not expired, not revoked
    Stage 4C authorizes?    re-checked now, not trusted from proposal time
    tool executable?        present in the executable registry
    arguments valid?        against the tool's own schema
    claim the execution     one conditional UPDATE; only one attempt wins

Only then does anything run.

What this module deliberately cannot do
---------------------------------------

No `eval`, no `exec`, no `compile`, no `__import__`, no `getattr` on a module,
no subprocess, no shell, no network client. Tool lookup is a dictionary hit on
a name registered in code; there is no path from a string to executable code.
A test asserts all of that structurally.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution import approvals, audit
from app.execution.errors import (
    AlreadyRunning,
    ExecutionDisabled,
    ExecutionError,
    NotAuthorized,
    NotExecutable,
    ToolFailure,
    WorkspaceViolation,
)
from app.execution.models import Execution, ExecutionEventType
from app.execution.schemas import ExecutionOutcome
from app.execution.states import ExecutionState
from app.execution.tools import (
    ExecutableRegistry,
    ExecutionContext,
    get_executable_registry,
)
from app.execution import workspace
from app.tools.authorization import AuthorizationService
from app.tools.schemas import ActionProposal, ActionSource, AuthorizationStatus

logger = get_logger(__name__)

#: Authorization outcomes from which execution may proceed *at all*.
#:
#: `FORBIDDEN` and `UNKNOWN_TOOL` are absent, and no approval adds them: an
#: approval can satisfy a requirement for approval, and cannot lift a denial.
_EXECUTABLE_STATUSES = frozenset(
    {AuthorizationStatus.ALLOWED, AuthorizationStatus.APPROVAL_REQUIRED}
)


class Dispatcher:
    """Runs one approved execution, or refuses and says why."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        authorization: Optional[AuthorizationService] = None,
        registry: Optional[ExecutableRegistry] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._authorization = authorization or AuthorizationService()
        self._registry = registry or get_executable_registry()

    async def dispatch(self, execution: Execution) -> ExecutionOutcome:
        """Run it. Raises `ExecutionError` if any gate refuses.

        A raised error always means *nothing ran*, except `ToolFailure`, which
        means the tool was reached and failed. That distinction is what lets
        the caller report truthfully.
        """
        self._require_enabled()
        approvals.validate(execution)
        self._require_authorized(execution)
        tool = self._require_executable(execution)
        arguments = tool.validate_arguments(dict(execution.arguments or {}))

        await self._claim(execution)

        context = ExecutionContext(
            workspace_root=workspace.ensure_workspace(
                workspace.workspace_root(self._settings.MAI_WORKSPACE_ROOT)
            ),
            max_file_bytes=self._settings.MAX_WORKSPACE_FILE_SIZE_BYTES,
            max_list_results=self._settings.MAX_WORKSPACE_LIST_RESULTS,
            max_list_depth=self._settings.MAX_WORKSPACE_LIST_DEPTH,
        )

        # The one line in Mai that causes a side effect.
        return tool.run(arguments, context)

    # --- Gates --------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._settings.EXECUTION_ENABLED:
            raise ExecutionDisabled()

    def _require_authorized(self, execution: Execution) -> None:
        """Re-ask Stage 4C now, rather than trusting the recorded decision.

        The decision stored at proposal time is history. Policy, the registry
        or the operator switches may have changed since, and the question that
        matters is whether this is permitted *now*.
        """
        decision = self._authorization.authorize(
            ActionProposal(
                tool_name=execution.tool_name,
                arguments=dict(execution.arguments or {}),
                source=ActionSource.USER,
            )
        )
        if decision.status not in _EXECUTABLE_STATUSES:
            raise NotAuthorized(detail=decision.status.value)

    def _require_executable(self, execution: Execution):
        """Registered and enabled are not executable.

        A tool reaches this point having passed Stage 4C, which only proves
        the application declares it and policy permits it. Whether an
        implementation exists is a different question, asked here.
        """
        tool = self._registry.get(execution.tool_name)
        if tool is None:
            raise NotExecutable(detail=execution.tool_name)
        return tool

    async def _claim(self, execution: Execution) -> None:
        """Move APPROVED -> EXECUTING, atomically, exactly once.

        A conditional UPDATE, not a read-then-write. Two workers racing here
        both issue the same statement; the `state = 'approved'` predicate makes
        exactly one of them match a row, and the loser sees `rowcount == 0` and
        refuses. This is why the guarantee survives multiple processes -- an
        in-memory lock would not.
        """
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            update(Execution)
            .where(
                Execution.id == execution.id,
                Execution.state == ExecutionState.APPROVED,
            )
            .values(
                state=ExecutionState.EXECUTING,
                started_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
        )

        if result.rowcount != 1:
            # Someone else claimed it, or it left APPROVED while we were
            # checking. Either way this attempt must not run the tool.
            raise AlreadyRunning(detail=str(execution.id))

        await audit.record(
            self._session,
            execution.id,
            ExecutionEventType.EXECUTION_STARTED,
            actor="system",
            metadata={"tool": execution.tool_name},
        )


__all__ = ["Dispatcher"]
