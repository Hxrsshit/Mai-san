"""Executable tools backed by an external integration.

The join between Stage 4E's dispatcher and Stage 4F-A's integration layer, and
the only place the two meet.

An `IntegrationTool` declares two things in code: which integration it uses
and which named operation it calls. Both are class attributes, fixed at
definition. Neither is an argument, so nothing in a request, a plan or a model
reply can redirect a tool at a different integration or a different operation
-- which is the difference between a bounded capability and an arbitrary API
client with extra steps.

What a tool passes to its integration is the validated arguments for that one
operation. Not the `ContextPackage`, not the conversation, not memories, not
settings, not a database session -- an integration receives what the operation
needs and has no way to ask for more, because nothing else is in scope.
"""

from abc import abstractmethod
from typing import Any, Dict, Type

from app.core.logging import get_logger
from app.execution.errors import ToolFailure
from app.execution.schemas import ExecutionOutcome
from app.execution.tools import ExecutableTool, ExecutionContext
from app.integrations.base import Integration
from app.integrations.errors import IntegrationError, UnsupportedOperation
from app.integrations.result import ExternalResult, ExternalResultState

logger = get_logger(__name__)


class IntegrationTool(ExecutableTool):
    """An executable tool whose work happens at an external service.

    Subclasses supply `integration_name`, `operation`, an arguments model, and
    a mapping from validated arguments to operation arguments. They do not
    supply a URL, a method, or a client.
    """

    #: Which registered integration. A constant, never an argument.
    integration_name: str = ""
    #: Which named operation on it. Also a constant.
    operation: str = ""

    @abstractmethod
    def build_operation_arguments(self, arguments) -> Dict[str, Any]:
        """Map validated tool arguments onto operation arguments.

        Written per tool rather than passing the whole argument object
        through, so what crosses into the integration is an explicit,
        readable list. Data minimisation as a function signature instead of a
        rule someone has to remember.
        """

    def run(self, arguments, context: ExecutionContext) -> ExecutionOutcome:
        """Call the integration, and convert whatever comes back.

        The integration is resolved by the dispatcher and handed over on the
        context -- exactly one, this tool's own. There is no registry here, so
        a tool cannot reach an integration other than the one it declares.
        """
        integration = context.integration
        if integration is None:
            # The dispatcher could not resolve it: unregistered, or the tool
            # names one that does not exist. Refused rather than attempted.
            raise ToolFailure(
                reason="integration_unavailable", detail=self.integration_name
            )

        if not integration.supports(self.operation):
            raise ToolFailure(
                reason="unsupported_operation",
                detail=f"{self.integration_name}:{self.operation}",
            )

        try:
            result = integration.invoke(
                self.operation, self.build_operation_arguments(arguments)
            )
        except IntegrationError as error:
            # A refusal from the integration layer -- an unknown operation, a
            # missing credential. The reason code crosses; nothing else does.
            raise ToolFailure(reason=error.reason, detail=error.detail) from error

        if not result.succeeded:
            raise ToolFailure(
                reason=result.reason or result.state.value,
                detail=f"{result.integration}:{result.operation}",
            )

        return self.summarise(result)

    def summarise(self, result: ExternalResult) -> ExecutionOutcome:
        """Turn a successful external result into an execution outcome.

        `audit_metadata` carries the safe operational facts -- integration,
        operation, latency, attempts, provider status -- into the journal.
        `data` carries the content back to the caller and is *not* persisted,
        because content from outside does not belong in an audit table.

        Any content is passed through as the `ExternalData` wrapper it
        arrived in, so its source and untrusted status travel with it rather
        than being reapplied by whoever renders it next.
        """
        return ExecutionOutcome(
            summary=result.summary or f"{result.operation} completed.",
            data=(
                {"external": result.data.model_dump(mode="json")}
                if result.data is not None
                else {}
            ),
            audit_metadata=result.audit_metadata(),
        )


class AsyncIntegrationTool(IntegrationTool):
    """An integration tool whose operation does real I/O.

    Identical to `IntegrationTool` except that it awaits. The split exists
    because both are genuinely needed: an adapter over an in-process resource
    is naturally synchronous, and one that opens a socket must not block the
    event loop for the seconds a network round trip can take.

    Stage 4E's dispatcher awaits an awaitable result, so choosing between
    these two changes nothing above the tool -- not the gates, not the
    approval system, not the audit.
    """

    async def run(self, arguments, context: ExecutionContext) -> ExecutionOutcome:
        integration = context.integration
        if integration is None:
            raise ToolFailure(
                reason="integration_unavailable", detail=self.integration_name
            )

        if not integration.supports(self.operation):
            raise ToolFailure(
                reason="unsupported_operation",
                detail=f"{self.integration_name}:{self.operation}",
            )

        try:
            result = await integration.ainvoke(
                self.operation, self.build_operation_arguments(arguments)
            )
        except IntegrationError as error:
            raise ToolFailure(reason=error.reason, detail=error.detail) from error

        if not result.succeeded:
            # The failure crosses as a failure. Nothing here can turn a
            # timeout or a refusal into an outcome that reads as a completed
            # search -- which is what stops Mai saying "I searched the web"
            # when it did not.
            raise ToolFailure(
                reason=result.reason or result.state.value,
                detail=f"{result.integration}:{result.operation}",
            )

        return self.summarise(result)


__all__ = ["AsyncIntegrationTool", "IntegrationTool"]
