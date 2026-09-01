"""Stage 4E: the execution lifecycle, end to end and at its edges.

The shape of these tests follows the shape of the guarantee. Proposing does
not run. Approving does not run. Only an explicit run request runs, and only
when every gate agrees -- so most of what follows checks that something did
*not* happen, and checks it against the filesystem rather than against a
return value.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.execution.states import ExecutionState

CREATE = {
    "tool_name": "create_text_file",
    "arguments": {"path": "notes.txt", "content": "hello"},
    "idempotency_key": "key-1",
}


async def _propose(client: AsyncClient, **overrides) -> dict:
    payload = {**CREATE, **overrides}
    response = await client.post("/api/executions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- The happy path ---------------------------------------------------------


async def test_the_full_lifecycle_creates_a_file(
    execution_client: AsyncClient, workspace
) -> None:
    """Propose, approve, execute -- and only then does a file exist."""
    proposed = await _propose(execution_client)
    assert proposed["state"] == "proposed"
    assert proposed["requires_approval"] is True
    assert not (workspace / "notes.txt").exists()

    approved = await execution_client.post(
        f"/api/executions/{proposed['id']}/approve", json={}
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    # Approval is not execution. The file still does not exist.
    assert not (workspace / "notes.txt").exists()

    executed = await execution_client.post(
        f"/api/executions/{proposed['id']}/execute", json={}
    )
    assert executed.status_code == 200, executed.text
    body = executed.json()["execution"]
    assert body["state"] == "succeeded"
    assert body["succeeded"] is True
    assert body["statement"] == "This action was performed successfully."

    assert (workspace / "notes.txt").read_text() == "hello"


async def test_reading_back_what_was_written(
    execution_client: AsyncClient, workspace
) -> None:
    """The read tool sees the write tool's file, and nothing outside."""
    (workspace / "existing.txt").write_text("from the workspace")

    proposed = await _propose(
        execution_client,
        tool_name="read_text_file",
        arguments={"path": "existing.txt"},
        idempotency_key="read-1",
    )
    await execution_client.post(f"/api/executions/{proposed['id']}/approve", json={})
    executed = await execution_client.post(
        f"/api/executions/{proposed['id']}/execute", json={}
    )

    assert executed.status_code == 200, executed.text
    assert executed.json()["execution"]["state"] == "succeeded"


# --- Proposing runs nothing -------------------------------------------------


async def test_proposing_alone_never_touches_the_filesystem(
    execution_client: AsyncClient, workspace
) -> None:
    """The strongest form of the claim: the directory is still empty."""
    await _propose(execution_client)
    await _propose(execution_client, idempotency_key="key-2",
                   arguments={"path": "other.txt", "content": "x"})

    assert list(workspace.iterdir()) == []


async def test_approving_alone_never_touches_the_filesystem(
    execution_client: AsyncClient, workspace
) -> None:
    proposed = await _propose(execution_client)
    response = await execution_client.post(
        f"/api/executions/{proposed['id']}/approve", json={}
    )

    assert response.status_code == 200
    assert list(workspace.iterdir()) == []


# --- Execution requires approval --------------------------------------------


async def test_executing_without_approval_is_refused(
    execution_client: AsyncClient, workspace
) -> None:
    """A proposed execution cannot run. There is no edge to EXECUTING."""
    proposed = await _propose(execution_client)

    response = await execution_client.post(
        f"/api/executions/{proposed['id']}/execute", json={}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "approval_required"
    assert not (workspace / "notes.txt").exists()


async def test_a_revoked_approval_cannot_be_used(
    execution_client: AsyncClient, workspace
) -> None:
    proposed = await _propose(execution_client)
    await execution_client.post(f"/api/executions/{proposed['id']}/approve", json={})

    revoked = await execution_client.post(
        f"/api/executions/{proposed['id']}/revoke", json={}
    )
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"

    response = await execution_client.post(
        f"/api/executions/{proposed['id']}/execute", json={}
    )
    assert response.status_code == 403
    assert not (workspace / "notes.txt").exists()


async def test_an_execution_cannot_run_twice(
    execution_client: AsyncClient, workspace
) -> None:
    """SUCCEEDED is terminal, and terminal means terminal."""
    proposed = await _propose(execution_client)
    await execution_client.post(f"/api/executions/{proposed['id']}/approve", json={})
    first = await execution_client.post(
        f"/api/executions/{proposed['id']}/execute", json={}
    )
    assert first.status_code == 200

    (workspace / "notes.txt").write_text("changed by someone else")

    second = await execution_client.post(
        f"/api/executions/{proposed['id']}/execute", json={}
    )
    assert second.status_code == 403
    # The second attempt did not re-run the tool over the file.
    assert (workspace / "notes.txt").read_text() == "changed by someone else"


# --- Approval is bound to the payload ---------------------------------------


async def test_changing_the_payload_invalidates_the_approval(
    executions, workspace
) -> None:
    """The fingerprint is the binding, and mutating arguments breaks it.

    Below the API deliberately: the HTTP surface offers no way to edit a
    recorded execution, so this exercises what would happen if some future
    code path did.
    """
    from app.execution.errors import ApprovalInvalid
    from app.execution.schemas import ExecutionRequest

    execution = await executions.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "approved.txt", "content": "original"},
            idempotency_key="swap-1",
        )
    )
    await executions.approve(execution.id)

    # Swap the payload after approval.
    execution.arguments = {"path": "elsewhere.txt", "content": "substituted"}

    with pytest.raises(ApprovalInvalid):
        await executions.run(execution.id)

    assert not (workspace / "elsewhere.txt").exists()
    assert not (workspace / "approved.txt").exists()


async def test_an_expired_approval_does_not_run(executions, workspace) -> None:
    from datetime import datetime, timedelta, timezone

    from app.execution.errors import ApprovalExpired
    from app.execution.schemas import ExecutionRequest

    execution = await executions.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "late.txt", "content": "x"},
            idempotency_key="expiry-1",
        )
    )
    await executions.approve(execution.id)
    execution.approval_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(ApprovalExpired):
        await executions.run(execution.id)

    assert not (workspace / "late.txt").exists()


# --- Idempotency ------------------------------------------------------------


async def test_the_same_key_returns_the_same_execution(
    execution_client: AsyncClient,
) -> None:
    first = await _propose(execution_client)
    second = await _propose(execution_client)

    assert first["id"] == second["id"]


async def test_an_omitted_key_is_derived_from_the_payload(
    execution_client: AsyncClient,
) -> None:
    """Derived, not random: a retry of the same action stays one action."""
    payload = {"tool_name": "create_text_file",
               "arguments": {"path": "derived.txt", "content": "x"}}

    first = await execution_client.post("/api/executions", json=payload)
    second = await execution_client.post("/api/executions", json=payload)

    assert first.json()["id"] == second.json()["id"]


async def test_a_different_payload_is_a_different_execution(
    execution_client: AsyncClient,
) -> None:
    first = await execution_client.post(
        "/api/executions",
        json={"tool_name": "create_text_file",
              "arguments": {"path": "a.txt", "content": "x"}},
    )
    second = await execution_client.post(
        "/api/executions",
        json={"tool_name": "create_text_file",
              "arguments": {"path": "b.txt", "content": "x"}},
    )

    assert first.json()["id"] != second.json()["id"]


# --- Unknown and unavailable ------------------------------------------------


async def test_an_unknown_execution_is_a_404(execution_client: AsyncClient) -> None:
    response = await execution_client.post(
        f"/api/executions/{uuid.uuid4()}/execute", json={}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_such_execution"


async def test_a_declared_but_unimplemented_tool_cannot_be_approved(
    execution_client: AsyncClient,
) -> None:
    """`future_send_email` is registered, permitted by risk, and has no body.

    It reaches the approval step and stops there. Approving it would create a
    grant that could never be honoured.
    """
    proposed = await _propose(
        execution_client,
        tool_name="future_send_email",
        arguments={},
        idempotency_key="email-1",
    )

    response = await execution_client.post(
        f"/api/executions/{proposed['id']}/approve", json={}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "tool_is_not_executable"


async def test_a_forbidden_tool_cannot_be_approved(
    execution_client: AsyncClient,
) -> None:
    """`future_delete_file` is disabled and above the risk ceiling."""
    proposed = await _propose(
        execution_client,
        tool_name="future_delete_file",
        arguments={},
        idempotency_key="delete-1",
    )
    assert proposed["authorization_status"] == "forbidden"

    response = await execution_client.post(
        f"/api/executions/{proposed['id']}/approve", json={}
    )
    assert response.status_code == 409


async def test_an_unknown_tool_is_recorded_and_refused(
    execution_client: AsyncClient,
) -> None:
    proposed = await _propose(
        execution_client,
        tool_name="shell_command",
        arguments={"cmd": "rm -rf /"},
        idempotency_key="shell-1",
    )

    assert proposed["authorization_status"] == "unknown_tool"
    response = await execution_client.post(
        f"/api/executions/{proposed['id']}/approve", json={}
    )
    assert response.status_code == 409


# --- The switch -------------------------------------------------------------


async def test_no_execution_endpoint_exists_when_execution_is_off(
    client: AsyncClient,
) -> None:
    """The default deployment does not expose these routes at all."""
    for path, method in (
        ("/api/executions", "post"),
        (f"/api/executions/{uuid.uuid4()}/execute", "post"),
        (f"/api/executions/{uuid.uuid4()}", "get"),
    ):
        request = getattr(client, method)
        response = (
            await request(path, json={}) if method == "post" else await request(path)
        )
        assert response.status_code == 404, path


# --- Gates that only a later change would exercise --------------------------
# Each of these covers a guard that the mutation run found unprotected: the
# happy path reaches the same outcome through an earlier check, so removing
# the guard changed nothing observable. A guard nothing would miss is not a
# guard, so these exercise each one directly.


async def test_authorization_is_re_checked_at_dispatch_not_trusted(
    executions, workspace, monkeypatch
) -> None:
    """Policy that changes after approval is honoured, not the stored decision.

    The record carries the decision taken at proposal time, and that copy is
    history rather than authority. Here the category is forbidden *after* a
    valid approval exists -- the run must refuse.
    """
    from app.execution.errors import NotAuthorized
    from app.execution.schemas import ExecutionRequest
    from app.tools import policy
    from app.tools.schemas import ToolCategory

    execution = await executions.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "policy.txt", "content": "x"},
            idempotency_key="policy-1",
        )
    )
    await executions.approve(execution.id)

    monkeypatch.setattr(
        policy, "FORBIDDEN_CATEGORIES", frozenset({ToolCategory.FILE_OPERATION})
    )

    with pytest.raises(NotAuthorized):
        await executions.run(execution.id)

    assert not (workspace / "policy.txt").exists()


async def test_approving_twice_is_refused(executions) -> None:
    """The transition table, exercised where nothing else covers it.

    Re-approving would silently extend the expiry window -- a way to keep a
    grant alive indefinitely without anyone approving anything new.
    """
    from app.execution.errors import InvalidStateTransition
    from app.execution.schemas import ExecutionRequest

    execution = await executions.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "twice.txt", "content": "x"},
            idempotency_key="twice-1",
        )
    )
    await executions.approve(execution.id)
    first_expiry = execution.approval_expires_at

    with pytest.raises(InvalidStateTransition):
        await executions.approve(execution.id)

    assert execution.approval_expires_at == first_expiry


async def test_a_completed_execution_cannot_be_revoked_or_re_approved(
    executions, workspace
) -> None:
    """Terminal states accept nothing, including tidying-up operations."""
    from app.execution.errors import InvalidStateTransition
    from app.execution.schemas import ExecutionRequest

    execution = await executions.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "done.txt", "content": "x"},
            idempotency_key="done-1",
        )
    )
    await executions.approve(execution.id)
    await executions.run(execution.id)

    with pytest.raises(InvalidStateTransition):
        await executions.revoke(execution.id)
    with pytest.raises(InvalidStateTransition):
        await executions.approve(execution.id)


async def test_the_dispatcher_validates_arguments_itself(
    db_session, execution_settings, workspace
) -> None:
    """Defence in depth, exercised with the outer layer removed.

    Authorization already validates arguments against the tool's schema, and
    against the *same* class the dispatcher uses -- so in normal operation a
    malformed payload is refused before it gets here, and removing this check
    changes no observable behaviour. That is precisely why it needs its own
    test: a guard whose only proof is another guard is not independently
    verified.

    So this drives the dispatcher directly, with an authorization service that
    permits everything, and shows the dispatcher refuses on its own.
    """
    from app.execution.dispatcher import Dispatcher
    from app.execution.errors import ToolFailure
    from app.execution.schemas import ExecutionRequest, payload_fingerprint
    from app.execution.service import ExecutionService
    from app.tools.schemas import AuthorizationDecision, AuthorizationStatus, RiskLevel

    class PermitsEverything:
        """Stands in for a Stage 4C layer that has been bypassed or broken."""

        def authorize(self, proposal, intent=None):
            return AuthorizationDecision(
                status=AuthorizationStatus.APPROVAL_REQUIRED,
                reason="allowed",
                tool_name=proposal.tool_name,
                risk_level=RiskLevel.LOW,
                requires_approval=True,
            )

    permissive = PermitsEverything()
    service = ExecutionService(
        db_session,
        settings=execution_settings,
        authorization=permissive,
        dispatcher=Dispatcher(
            db_session, settings=execution_settings, authorization=permissive
        ),
    )

    execution = await service.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": "valid.txt", "content": "x"},
            idempotency_key="argcheck-1",
        )
    )
    await service.approve(execution.id)

    # A malformed payload, approved for its own fingerprint -- so neither the
    # approval check nor the (bypassed) authorization check will stop it.
    execution.arguments = {"path": "valid.txt", "sudo": True}
    execution.approved_fingerprint = payload_fingerprint(
        "create_text_file", execution.arguments
    )

    with pytest.raises(ToolFailure) as failure:
        await service.run(execution.id)

    assert failure.value.reason == "invalid_arguments"
    assert not (workspace / "valid.txt").exists()
