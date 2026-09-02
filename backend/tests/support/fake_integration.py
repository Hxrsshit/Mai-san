"""A fake external service, and a tool that reaches it.

Stands in for the real integrations Stage 4F-B and later will add. It makes no
network call: the "provider" is a Python function whose behaviour the test
sets. That is the point -- the architecture is what is under test, and a real
provider would make these tests slow, flaky, and dependent on someone else's
uptime.

The end-to-end path it exercises is the whole reason the stage exists:

    tool -> authorization -> approval -> dispatcher -> integration -> provider
         -> structured result -> audit
"""

from typing import Any, Dict, Optional, Tuple, Type

from pydantic import Field

from app.execution.integration_tools import IntegrationTool
from app.integrations.base import Integration, IntegrationState, OperationSpec
from app.integrations.credentials import (
    CredentialRequirement,
    CredentialType,
    EnvironmentCredentialResolver,
)
from app.integrations.policy import NetworkPolicy, RetryPolicy
from app.integrations.result import (
    DataClassification,
    ExternalData,
    ExternalResult,
    ExternalResultState,
)
from app.tools.base import Tool, ToolArguments
from app.tools.schemas import (
    ExecutionMode,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
)

#: The hosts the fake integration is allowed to reach. Never contacted -- the
#: policy exists so the SSRF boundary can be exercised against a realistic
#: allow-list rather than an empty one.
FAKE_HOSTS = frozenset({"api.fake-provider.test"})


class FakeLookupArguments(ToolArguments):
    """The tool's argument schema. Note what is absent.

    No `api_key`, no `url`, no `endpoint`, no `method`, no `headers`. A user
    cannot supply a credential and cannot choose a destination, because there
    is no field for either -- the same "a model cannot set what it cannot
    name" reasoning the earlier stages used.
    """

    query: str = Field(..., min_length=1, max_length=200)


class FakeIntegration(Integration):
    """A provider that returns whatever the test told it to.

    `responder` is a callable taking the operation arguments and returning an
    `ExternalResult`, or raising an `IntegrationError` to exercise a failure
    path. Sleeping is stubbed out so backoff can be asserted without waiting.
    """

    name = "fake_provider"
    description = "A fake external service used to exercise the architecture."
    provider = "fake"

    def __init__(
        self,
        responder=None,
        credentials=None,
        enabled: bool = True,
        retries: Optional[RetryPolicy] = None,
        side_effect_operation: bool = False,
    ) -> None:
        # A usable credential by default, so a test about retries or result
        # states does not have to configure one. Tests about *unavailability*
        # pass an empty environ explicitly. Without this the fake falls back
        # to the process resolver and every call returns UNAVAILABLE, which
        # would quietly turn most of these tests into a check that an
        # unconfigured integration refuses -- true, but not what they claim.
        credentials = credentials or EnvironmentCredentialResolver(
            environ={"FAKE_PROVIDER_API_KEY": "fake-key"}
        )
        self._responder = responder or self._default_responder
        self._retries = retries or RetryPolicy()
        self._side_effect_operation = side_effect_operation
        #: Every backoff this integration took, so tests can assert bounds
        #: without the wall clock being involved.
        self.slept: list = []
        self.calls: list = []
        super().__init__(credentials=credentials, enabled=enabled)

    def declare_operations(self) -> Tuple[OperationSpec, ...]:
        return (
            OperationSpec(
                name="lookup",
                handler=self._lookup,
                has_side_effect=self._side_effect_operation,
                description="Look something up. Read-only unless configured otherwise.",
            ),
        )

    @property
    def credential_requirement(self) -> CredentialRequirement:
        return CredentialRequirement(
            identifier="fake_provider.api_key",
            provider="fake",
            credential_type=CredentialType.API_KEY,
            setting_name="FAKE_PROVIDER_API_KEY",
            required_scopes=frozenset({"lookup.read"}),
        )

    @property
    def network_policy(self) -> NetworkPolicy:
        return NetworkPolicy(allowed_hosts=FAKE_HOSTS, retries=self._retries)

    def _lookup(self, arguments: Dict[str, Any]) -> ExternalResult:
        self.calls.append(dict(arguments))
        return self._responder(arguments)

    def _default_responder(self, arguments: Dict[str, Any]) -> ExternalResult:
        return ExternalResult(
            state=ExternalResultState.SUCCESS,
            integration=self.name,
            operation="lookup",
            summary="Lookup completed.",
            data=ExternalData(
                source=self.name,
                content=f"Results for {arguments.get('query', '')}.",
                classification=DataClassification.PUBLIC,
            ),
            provider_status=200,
        )

    def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class FakeLookupTool(IntegrationTool):
    """The executable side: names one integration and one operation."""

    name = "fake_lookup"
    integration_name = "fake_provider"
    operation = "lookup"

    @property
    def arguments_model(self) -> Type[ToolArguments]:
        return FakeLookupArguments

    def build_operation_arguments(self, arguments) -> Dict[str, Any]:
        # One field crosses the boundary. Not the argument object, not a
        # context, not a conversation -- the query, and nothing else.
        return {"query": arguments.query}


class FakeLookupDeclaration(Tool):
    """The Stage 4C declaration the executable tool is matched to by name."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fake_lookup",
            description="Look something up using a fake external provider.",
            category=ToolCategory.INFORMATION,
            risk_level=RiskLevel.LOW,
            requires_approval=True,
            execution_mode=ExecutionMode.SYNCHRONOUS,
            enabled=True,
        )

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        return FakeLookupArguments


def integration_environment(
    responder=None,
    credential_value: Optional[str] = "fake-key",
    enabled: bool = True,
    retries: Optional[RetryPolicy] = None,
    side_effect_operation: bool = False,
):
    """Build an isolated tool registry, executable registry and integration.

    Returns `(tool_registry, executable_registry, integration_registry,
    integration)`. Every registry is fresh, so no test touches the process
    catalogues -- which is also how these tests prove a tool can be added
    without editing any shipped registry.
    """
    from app.execution.tools import ExecutableRegistry
    from app.integrations.credentials import EnvironmentCredentialResolver
    from app.integrations.registry import IntegrationRegistry
    from app.tools.catalog import build_catalog
    from app.tools.registry import ToolRegistry

    environ = {}
    if credential_value is not None:
        environ["FAKE_PROVIDER_API_KEY"] = credential_value

    integration = FakeIntegration(
        responder=responder,
        credentials=EnvironmentCredentialResolver(environ=environ),
        enabled=enabled,
        retries=retries,
        side_effect_operation=side_effect_operation,
    )

    integrations = IntegrationRegistry()
    integrations.register(integration)
    integrations.seal()

    tools = build_catalog(ToolRegistry())
    tools.register(FakeLookupDeclaration())

    executable = ExecutableRegistry()
    executable.register(FakeLookupTool())

    return tools, executable, integrations, integration
