"""Stage 4E: what a deployment with execution switched off can do.

This is the configuration Mai ships in, so it deserves its own file rather
than a footnote. The switch defaults to false, and with it false there is no
sequence of requests that causes a side effect.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.execution.errors import ExecutionDisabled
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService


def test_execution_is_off_by_default() -> None:
    """The default is a decision, and this is where it is recorded.

    Read from a `Settings` built with no environment file at all, so this
    tests the declared default rather than whatever a local `.env` happens to
    say.
    """
    assert Settings(_env_file=None, GROQ_API_KEY="x").EXECUTION_ENABLED is False


async def test_the_service_refuses_to_propose_when_disabled(
    db_session, settings: Settings, workspace
) -> None:
    settings.MAI_WORKSPACE_ROOT = str(workspace)
    service = ExecutionService(db_session, settings=settings)

    with pytest.raises(ExecutionDisabled):
        await service.create(
            ExecutionRequest(
                tool_name="create_text_file",
                arguments={"path": "a.txt", "content": "x"},
                idempotency_key="disabled-1",
            )
        )

    assert list(workspace.iterdir()) == []


async def test_the_dispatcher_refuses_even_a_fully_approved_execution(
    db_session, execution_settings: Settings, workspace
) -> None:
    """Approved under a switch that is then turned off. Still nothing runs.

    The scenario the switch exists for: a grant made while execution was on
    must not survive the switch being turned off. The dispatcher checks the
    switch first, before it looks at the approval at all.
    """
    service = ExecutionService(db_session, settings=execution_settings)
    execution = await service.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "kill-switch.txt", "content": "x"},
            idempotency_key="kill-1",
        )
    )
    await service.approve(execution.id)

    # The operator flips the switch.
    execution_settings.EXECUTION_ENABLED = False

    with pytest.raises(ExecutionDisabled):
        await service.run(execution.id)

    assert not (workspace / "kill-switch.txt").exists()


async def test_no_route_exists_when_execution_is_off(client: AsyncClient) -> None:
    """Two doors, and this is the outer one: the endpoints are not registered."""
    response = await client.post(
        "/api/executions",
        json={"tool_name": "create_text_file",
              "arguments": {"path": "a.txt", "content": "x"}},
    )

    assert response.status_code == 404


async def test_the_tools_endpoint_still_describes_them_when_disabled(
    client: AsyncClient,
) -> None:
    """Describing a capability is not offering it.

    The declarations stay visible with execution off, which is correct: they
    are what the application *could* do if switched on, and hiding them would
    make the catalogue depend on a runtime switch it has nothing to do with.
    """
    body = (await client.get("/api/tools")).json()
    names = {item["name"] for item in body["items"]}

    assert "create_text_file" in names
    assert {
        item["name"] for item in body["items"]
        if item["execution_mode"] == "synchronous"
    } == {
        "calendar_list_events", "create_text_file", "read_text_file",
        "list_workspace_files", "web_search",
    }


def test_runtime_facts_report_no_execution_capability_by_default() -> None:
    """What Mai would tell a user who asked, in the default deployment."""
    from app.runtime.facts import build

    facts = build(Settings(_env_file=None, GROQ_API_KEY="x"))

    assert facts.execution_enabled is False
    assert facts.can_execute_actions is False


def test_runtime_facts_need_both_the_switch_and_an_executor() -> None:
    """Neither conjunct alone is enough, and each is checked separately.

    Testing them together would pass if the property were `execution_enabled`
    alone, which is a materially weaker guarantee.
    """
    from app.runtime.facts import build

    switch_only = build(
        Settings(_env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=True),
        executable_tool_count=0,
    )
    executors_only = build(
        Settings(_env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=False),
        executable_tool_count=3,
    )
    both = build(
        Settings(_env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=True),
        executable_tool_count=3,
    )

    assert switch_only.can_execute_actions is False
    assert executors_only.can_execute_actions is False
    assert both.can_execute_actions is True


def test_the_capability_is_derived_and_cannot_be_set() -> None:
    """C3's surviving half: a property, not a field.

    The guarantee weakened in Stage 4E -- it is configuration-gated now rather
    than structurally impossible -- but this part did not. Nothing can assert
    the capability; the application computes it.
    """
    from pydantic import ValidationError

    from app.runtime.schemas import RuntimeFacts

    assert "can_execute_actions" not in RuntimeFacts.model_fields

    with pytest.raises(ValidationError):
        RuntimeFacts(
            assistant_name="Mai",
            environment="test",
            llm_provider="groq",
            llm_model="test",
            database="sqlite",
            can_execute_actions=True,
        )
