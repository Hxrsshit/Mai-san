"""Stage 4F-A: the whole pipeline, with a fake provider at the far end.

    tool -> authorization -> approval -> dispatcher -> integration adapter
         -> fake provider -> structured result -> audit

The point of running the full path rather than unit-testing the adapter is
Part 31: replacing the fake provider with a real one must not require touching
`ChatService`, the authorization policy, the approval system, the dispatcher
contract or `RuntimeFacts`. These tests pass with a fake at the end, and the
only thing a real integration changes is which class `build_integrations`
registers.
"""

import pytest

from app.execution.errors import ExecutionError, NotExecutable, ToolFailure
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.integrations.errors import ProviderTimeout, ProviderUnauthorized
from app.integrations.result import (
    DataClassification,
    ExternalData,
    ExternalResult,
    ExternalResultState,
)
from app.tools.authorization import AuthorizationService
from app.tools.schemas import AuthorizationStatus
from tests.support.fake_integration import integration_environment


@pytest.fixture
def environment():
    return integration_environment()


def _service(db_session, settings, environment, dispatcher_overrides=None):
    """An ExecutionService wired to the isolated registries."""
    from app.execution.dispatcher import Dispatcher

    tools, executable, integrations, integration = environment
    authorization = AuthorizationService(registry=tools)
    dispatcher = Dispatcher(
        db_session,
        settings=settings,
        authorization=authorization,
        registry=executable,
        integrations=integrations,
        **(dispatcher_overrides or {}),
    )
    return ExecutionService(
        db_session,
        settings=settings,
        authorization=authorization,
        dispatcher=dispatcher,
        executable=executable,
    ), integration


async def _run(service, key="pipe-1", query="weather"):
    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": query},
            idempotency_key=key,
        )
    )
    await service.approve(execution.id)
    await service.run(execution.id)
    return execution


# --- The full path ----------------------------------------------------------


async def test_the_whole_pipeline_reaches_the_provider_and_records_it(
    db_session, execution_settings, environment
) -> None:
    service, integration = _service(db_session, execution_settings, environment)

    execution = await _run(service)

    assert execution.state is ExecutionState.SUCCEEDED
    # The provider was reached, with exactly the operation arguments.
    assert integration.calls == [{"query": "weather"}]

    events = await service.history(execution.id)
    assert [event.event_type.value for event in events] == [
        "proposed", "approved", "execution_started", "execution_succeeded",
    ]


async def test_the_audit_records_the_integration_and_operation(
    db_session, execution_settings, environment
) -> None:
    """Part 17: safe operational metadata reaches the journal."""
    service, _ = _service(db_session, execution_settings, environment)

    execution = await _run(service, key="audit-1")
    events = await service.history(execution.id)
    succeeded = next(
        event for event in events if event.event_type.value == "execution_succeeded"
    )

    assert succeeded.event_metadata["integration"] == "fake_provider"
    assert succeeded.event_metadata["operation"] == "lookup"
    assert succeeded.event_metadata["result"] == "success"
    assert succeeded.event_metadata["provider_status"] == 200
    assert succeeded.event_metadata["attempts"] == 1
    assert isinstance(succeeded.event_metadata["latency_ms"], int)


async def test_proposing_alone_never_reaches_the_provider(
    db_session, execution_settings, environment
) -> None:
    service, integration = _service(db_session, execution_settings, environment)

    await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key="propose-only",
        )
    )

    assert integration.calls == []


async def test_approving_alone_never_reaches_the_provider(
    db_session, execution_settings, environment
) -> None:
    service, integration = _service(db_session, execution_settings, environment)

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key="approve-only",
        )
    )
    await service.approve(execution.id)

    assert integration.calls == []


async def test_an_unapproved_execution_never_reaches_the_provider(
    db_session, execution_settings, environment
) -> None:
    """Part 33 #7: the approval requirement is not bypassed by integrations."""
    service, integration = _service(db_session, execution_settings, environment)

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key="unapproved",
        )
    )

    with pytest.raises(ExecutionError):
        await service.run(execution.id)

    assert integration.calls == []


async def test_execution_disabled_stops_the_pipeline_before_the_provider(
    db_session, settings, environment, workspace
) -> None:
    """The Stage 4E switch still governs everything, integrations included."""
    settings.MAI_WORKSPACE_ROOT = str(workspace)
    service, integration = _service(db_session, settings, environment)

    with pytest.raises(ExecutionError):
        await service.create(
            ExecutionRequest(
                tool_name="fake_lookup",
                arguments={"query": "x"},
                idempotency_key="disabled",
            )
        )

    assert integration.calls == []


# --- Availability is not permission -----------------------------------------


async def test_an_unavailable_integration_fails_without_claiming_success(
    db_session, execution_settings, workspace
) -> None:
    """Part 20: a tool can be authorized and still unusable."""
    environment = integration_environment(credential_value=None)
    service, integration = _service(db_session, execution_settings, environment)

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key="nocred-1",
        )
    )
    # Authorization is unaffected: policy permits it, the service does not.
    assert execution.authorization_status is AuthorizationStatus.APPROVAL_REQUIRED

    await service.approve(execution.id)
    with pytest.raises(ToolFailure) as failure:
        await service.run(execution.id)

    assert failure.value.reason == "integration_unavailable"
    assert integration.calls == []

    refreshed = await service.get(execution.id)
    assert refreshed.state is ExecutionState.FAILED


async def test_a_tool_naming_an_unregistered_integration_is_refused(
    db_session, execution_settings, workspace
) -> None:
    """Part 33 #4: unknown integrations cannot execute."""
    from app.integrations.registry import IntegrationRegistry

    tools, executable, _, _ = integration_environment()
    empty = IntegrationRegistry()
    empty.seal()

    service, _ = _service(
        db_session, execution_settings, (tools, executable, empty, None)
    )

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key="unregistered",
        )
    )
    await service.approve(execution.id)

    with pytest.raises(NotExecutable):
        await service.run(execution.id)


# --- Failures are structured, not collapsed ---------------------------------


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (ProviderUnauthorized(), "provider_unauthorized"),
        (ProviderTimeout(), "provider_timeout"),
    ],
)
async def test_a_provider_failure_reaches_the_journal_with_its_reason(
    db_session, execution_settings, workspace, error, expected_reason
) -> None:
    def failing(arguments):
        raise error

    environment = integration_environment(responder=failing)
    service, _ = _service(db_session, execution_settings, environment)

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key=f"fail-{expected_reason}",
        )
    )
    await service.approve(execution.id)

    with pytest.raises(ToolFailure) as failure:
        await service.run(execution.id)

    assert failure.value.reason == expected_reason

    refreshed = await service.get(execution.id)
    assert refreshed.state is ExecutionState.FAILED
    assert refreshed.succeeded is False if hasattr(refreshed, "succeeded") else True

    events = await service.history(execution.id)
    assert events[-1].event_type.value == "execution_failed"


async def test_a_failed_external_call_never_reports_success(
    db_session, execution_settings, workspace
) -> None:
    """Part 33 #29: success requires a verified executor result."""
    def failing(arguments):
        return ExternalResult(
            state=ExternalResultState.FORBIDDEN,
            integration="fake_provider",
            operation="lookup",
            reason="provider_forbidden",
        )

    environment = integration_environment(responder=failing)
    service, _ = _service(db_session, execution_settings, environment)

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "x"},
            idempotency_key="notsuccess",
        )
    )
    await service.approve(execution.id)
    with pytest.raises(ToolFailure):
        await service.run(execution.id)

    refreshed = await service.get(execution.id)
    assert refreshed.state is not ExecutionState.SUCCEEDED
    assert refreshed.result_summary is None


# --- Approval binding still applies -----------------------------------------


async def test_changing_the_query_after_approval_invalidates_it(
    db_session, execution_settings, environment
) -> None:
    """Stage 4E's fingerprint governs integration tools identically."""
    from app.execution.errors import ApprovalInvalid

    service, integration = _service(db_session, execution_settings, environment)

    execution = await service.create(
        ExecutionRequest(
            tool_name="fake_lookup",
            arguments={"query": "harmless"},
            idempotency_key="swap-1",
        )
    )
    await service.approve(execution.id)
    execution.arguments = {"query": "something else entirely"}

    with pytest.raises(ApprovalInvalid):
        await service.run(execution.id)

    assert integration.calls == []


# --- Capability reporting follows integration availability ------------------


def test_a_tool_with_an_unavailable_integration_is_not_reported_usable(
    execution_settings,
) -> None:
    """Part 19: capability facts account for the integration, automatically."""
    from app.runtime.capabilities import CapabilityState, build

    tools, executable, integrations, _ = integration_environment()
    unavailable = integration_environment(credential_value=None)

    ready = build(
        settings=execution_settings, registry=tools, executable=executable,
        integrations=integrations,
    )
    missing = build(
        settings=execution_settings, registry=unavailable[0],
        executable=unavailable[1], integrations=unavailable[2],
    )

    def state_of(capabilities):
        return next(
            item.state for item in capabilities if item.identifier == "fake_lookup"
        )

    assert state_of(ready) is CapabilityState.AVAILABLE_WITH_APPROVAL
    assert state_of(missing) is CapabilityState.IMPLEMENTED_UNAVAILABLE


def test_nothing_is_added_to_the_prompt_by_hand(execution_settings) -> None:
    """The capability appears because it was registered, not because it was written.

    No prompt text mentions `fake_lookup` anywhere -- it reaches the rendered
    section purely by being in the registries.
    """
    from app.prompt.formatter import render_capabilities
    from app.runtime.capabilities import build

    tools, executable, integrations, _ = integration_environment()
    block = "\n".join(
        render_capabilities(
            build(
                settings=execution_settings, registry=tools,
                executable=executable, integrations=integrations,
            )
        )
    )

    assert "Fake lookup" in block

    from pathlib import Path

    formatter = Path("app/prompt/formatter.py").read_text()
    assert "fake_lookup" not in formatter
