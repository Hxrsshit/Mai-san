"""Stage 7B unit 8b: executions and workflows belong to their member.

An execution or workflow carries its creator's scope. Another member, a
member of another organisation, and an unattributed (legacy) row are
"unknown" to a scope -- for reading, its journal, approving, revoking,
running and finalising. The idempotency key is per scope, so one member's
payload never returns, or collides with, another's record. References stay
inside the scope. Task executions are bound to the owner's scope as the
owner is *now*, through the composition root's resolver. Rows are synthetic.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.database.models import scope as scope_module
from app.execution.errors import InvalidStateTransition, UnknownExecution
from app.execution.models import Execution
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.services.conversation_service import ConversationService
from app.workflows.models import Workflow
from app.workflows.schemas import WorkflowOutcome
from app.workflows.service import WorkflowService
from app.workflows.states import WorkflowState
from tests.scopes import (
    ADMIN_SCOPE,
    COLLEAGUE_PRINCIPAL,
    COLLEAGUE_SCOPE,
    OUTSIDER_SCOPE,
    make_active_member,
)

pytestmark = pytest.mark.anyio

OTHERS = [pytest.param(COLLEAGUE_SCOPE, id="colleague"), pytest.param(OUTSIDER_SCOPE, id="outsider")]
REQUEST = ExecutionRequest(tool_name="create_text_file",
                           arguments={"path": "notes.txt", "content": "hello"})


def _service(session, settings, scope):
    return ExecutionService(session, settings=settings, scope=scope)


async def _admin_execution(session_factory, settings):
    async with session_factory() as session:
        execution = await _service(session, settings, ADMIN_SCOPE).create(REQUEST)
        await session.commit()
        return execution.id


def test_the_service_refuses_anything_but_a_scope_binding() -> None:
    for bogus in (None, "personal", ADMIN_SCOPE.write_columns(), uuid.uuid4(), object()):
        with pytest.raises(TypeError):
            ExecutionService(object(), scope=bogus)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            WorkflowService(object(), scope=bogus)  # type: ignore[arg-type]


async def test_a_new_execution_carries_its_creators_scope(db_session, execution_settings) -> None:
    execution = await _service(db_session, execution_settings, ADMIN_SCOPE).create(REQUEST)
    assert ADMIN_SCOPE.owns(execution)


@pytest.mark.parametrize("other", OTHERS)
async def test_another_scope_cannot_read_approve_revoke_or_run_it(
    session_factory, execution_settings, other
) -> None:
    execution_id = await _admin_execution(session_factory, execution_settings)
    async with session_factory() as session:
        theirs = _service(session, execution_settings, other)
        for operation in (theirs.get, theirs.history, theirs.approve, theirs.revoke, theirs.run):
            with pytest.raises(UnknownExecution):
                await operation(execution_id)
        await session.commit()
    async with session_factory() as session:
        row = (await session.execute(select(Execution).where(Execution.id == execution_id))).scalar_one()
        assert row.state is ExecutionState.PROPOSED and row.approved_at is None


async def test_rows_already_in_the_identity_map_are_still_refused(db_session, execution_settings) -> None:
    execution = await _service(db_session, execution_settings, ADMIN_SCOPE).create(REQUEST)
    assert await db_session.get(Execution, execution.id) is not None
    with pytest.raises(UnknownExecution):
        await _service(db_session, execution_settings, COLLEAGUE_SCOPE).get(execution.id)


async def test_the_same_payload_is_one_execution_per_scope_not_one_globally(
    db_session, execution_settings
) -> None:
    """Before unit 8b, the colleague's identical proposal returned the
    Admin's record (the payload-derived key was global)."""
    mine = await _service(db_session, execution_settings, ADMIN_SCOPE).create(REQUEST)
    theirs = await _service(db_session, execution_settings, COLLEAGUE_SCOPE).create(REQUEST)
    again = await _service(db_session, execution_settings, ADMIN_SCOPE).create(REQUEST)
    assert theirs.id != mine.id and again.id == mine.id
    assert COLLEAGUE_SCOPE.owns(theirs) and mine.idempotency_key == theirs.idempotency_key


async def test_references_stay_inside_the_scope(db_session, execution_settings) -> None:
    mine = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    theirs = await ConversationService(db_session, COLLEAGUE_SCOPE).create_conversation()
    service = _service(db_session, execution_settings, ADMIN_SCOPE)
    linked = await service.create(REQUEST.model_copy(update={"idempotency_key": "a"}),
                                  conversation_id=mine.id)
    unlinked = await service.create(REQUEST.model_copy(update={"idempotency_key": "b"}),
                                    conversation_id=theirs.id)
    assert linked.conversation_id == mine.id and unlinked.conversation_id is None
    workflow = Workflow(conversation_id=theirs.id, kind="research_document",
                        state=WorkflowState.PENDING, plan={}, **COLLEAGUE_SCOPE.write_columns())
    db_session.add(workflow)
    await db_session.flush()
    with pytest.raises(InvalidStateTransition):
        await service.create(REQUEST.model_copy(update={"idempotency_key": "c"}),
                             workflow_id=workflow.id, step_index=0)


async def test_a_legacy_execution_is_quarantined(db_session, execution_settings) -> None:
    legacy = Execution(tool_name="create_text_file", arguments={}, state=ExecutionState.PROPOSED,
                       authorization_status="allowed", idempotency_key="legacy-key")
    db_session.add(legacy)
    await db_session.flush()
    with pytest.raises(UnknownExecution):
        await _service(db_session, execution_settings, ADMIN_SCOPE).get(legacy.id)
    fresh = await _service(db_session, execution_settings, ADMIN_SCOPE).create(
        REQUEST.model_copy(update={"idempotency_key": "legacy-key"}))
    assert fresh.id != legacy.id                       # never returned as "the original"


async def test_the_api_never_reaches_a_colleagues_execution(
    execution_client: AsyncClient, session_factory, execution_settings
) -> None:
    async with session_factory() as session:
        theirs = await _service(session, execution_settings, COLLEAGUE_SCOPE).create(REQUEST)
        await session.commit()
    for method, path in (("GET", f"/api/executions/{theirs.id}"),
                         ("GET", f"/api/executions/{theirs.id}/history"),
                         ("POST", f"/api/executions/{theirs.id}/approve"),
                         ("POST", f"/api/executions/{theirs.id}/revoke"),
                         ("POST", f"/api/executions/{theirs.id}/execute")):
        response = await execution_client.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 404, (path, response.status_code)
    async with session_factory() as session:
        row = (await session.execute(select(Execution).where(Execution.id == theirs.id))).scalar_one()
        assert row.state is ExecutionState.PROPOSED


# --- Workflows -------------------------------------------------------------------


async def test_a_workflow_of_another_scope_is_never_found(db_session, execution_settings) -> None:
    conversation = await ConversationService(db_session, COLLEAGUE_SCOPE).create_conversation()
    workflow = Workflow(conversation_id=conversation.id, kind="research_document",
                        state=WorkflowState.RUNNING, plan={}, **COLLEAGUE_SCOPE.write_columns())
    db_session.add(workflow)
    await db_session.flush()
    mine = WorkflowService(db_session, settings=execution_settings, scope=ADMIN_SCOPE)
    assert (await mine.finalise(workflow.id, "body")).outcome is WorkflowOutcome.NOT_WORKFLOW
    assert await mine._pending_for(conversation.id) is None


async def test_a_turn_in_another_scopes_conversation_is_not_a_workflow(
    db_session, execution_settings
) -> None:
    from app.intent.schemas import IntentResult

    theirs = await ConversationService(db_session, COLLEAGUE_SCOPE).create_conversation()
    service = WorkflowService(db_session, settings=execution_settings, scope=ADMIN_SCOPE)
    result = await service.handle(theirs.id, "research quantum batteries and write a document",
                                  IntentResult.model_construct())
    assert result.outcome is WorkflowOutcome.NOT_WORKFLOW
    assert (await db_session.execute(select(func.count()).select_from(Workflow))).scalar_one() == 0


async def test_an_injected_execution_service_of_another_scope_is_refused(
    db_session, execution_settings
) -> None:
    with pytest.raises(TypeError):
        WorkflowService(db_session, settings=execution_settings, scope=ADMIN_SCOPE,
                        executions=_service(db_session, execution_settings, COLLEAGUE_SCOPE))


# --- Task executions: the owner's scope, as the owner is now -------------------


async def _step_task(session_factory, settings, owner_id):
    from app.planning.schemas import Plan
    from app.tasks.service import TaskService
    from tests.test_background_runtime import goal, step

    async with session_factory() as session:
        tasks = TaskService(session, settings=settings, owner_id=owner_id)
        created = await tasks.create_for_user("Write a note")
        await tasks.attach_plan(created.task_id, Plan(goal=goal(), tasks=[step("a", 1)]))
        await tasks.authorize_plan(created.task_id)
        await session.commit()
        return created.task_id


async def _run_step(session_factory, settings, owner_id, task_id):
    from app.tasks.service import TaskService

    async with session_factory() as session:
        result = await TaskService(session, settings=settings, owner_id=owner_id).create_step_execution(
            task_id, "a")
        await session.commit()
        return result


async def test_a_task_step_runs_in_its_owners_scope(session_factory, execution_settings) -> None:
    await make_active_member(session_factory, COLLEAGUE_PRINCIPAL.member_id)
    task_id = await _step_task(session_factory, execution_settings, COLLEAGUE_PRINCIPAL.member_id)
    result = await _run_step(session_factory, execution_settings, COLLEAGUE_PRINCIPAL.member_id, task_id)
    assert result.reason != "owner_not_active", result
    async with session_factory() as session:
        [execution] = (await session.execute(select(Execution))).scalars().all()
        assert COLLEAGUE_SCOPE.owns(execution)


async def test_an_owner_who_is_not_an_active_member_gets_no_execution(
    session_factory, execution_settings
) -> None:
    owner = uuid.uuid4()                                  # no membership row
    task_id = await _step_task(session_factory, execution_settings, owner)
    result = await _run_step(session_factory, execution_settings, owner, task_id)
    assert result.reason == "owner_not_active"
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Execution))).scalar_one() == 0


async def test_without_a_registered_resolver_task_work_is_refused(
    session_factory, execution_settings, monkeypatch
) -> None:
    """Fail closed: a process that never registered the resolver runs no
    task execution at all."""
    monkeypatch.setattr(scope_module, "_member_scope_resolver", None)
    task_id = await _step_task(session_factory, execution_settings, ADMIN_SCOPE.member_id)
    result = await _run_step(session_factory, execution_settings, ADMIN_SCOPE.member_id, task_id)
    assert result.reason == "owner_not_active"


async def test_the_background_runner_creates_nothing_for_an_inactive_owner(
    session_factory, execution_settings
) -> None:
    from datetime import datetime, timezone

    from app.background.runtime import run_due_tasks
    from tests.test_background_runtime import scheduled_task

    now = datetime.now(timezone.utc)

    owner = uuid.uuid4()                                  # no membership row
    await scheduled_task(session_factory, execution_settings, owner=owner, at=now, grant=True)
    await run_due_tasks(session_factory, execution_settings, now=now)
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Execution))).scalar_one() == 0


# --- Integrations are the owner's until unit 9 (review M2) -----------------------


async def test_an_integration_backed_tool_is_the_owners_alone(db_session, execution_settings) -> None:
    """A member's calendar/mail/search execution would act through the one
    connected account -- the owner's. Refused before anything is recorded."""
    from app.execution.errors import NotExecutable
    from app.execution.tools import ExecutableRegistry

    class IntegrationTool:
        name = "calendar_list_events"
        integration_name = "google_calendar"

    registry = ExecutableRegistry()
    registry.register(IntegrationTool())  # type: ignore[arg-type]
    request = ExecutionRequest(tool_name="calendar_list_events", arguments={})
    theirs = ExecutionService(db_session, settings=execution_settings, scope=COLLEAGUE_SCOPE,
                              executable=registry)
    with pytest.raises(NotExecutable):
        await theirs.create(request)
    assert (await db_session.execute(select(func.count()).select_from(Execution))).scalar_one() == 0
    mine = ExecutionService(db_session, settings=execution_settings, scope=ADMIN_SCOPE,
                            executable=registry)
    assert ADMIN_SCOPE.owns(await mine.create(request))


# --- Review L5: more inactive owners; the chat proposal lookups ----------------


@pytest.mark.parametrize("change", ["revoked", "suspended_organisation"])
async def test_a_revoked_or_suspended_owner_gets_no_task_execution(
    session_factory, execution_settings, change
) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.identity.constants import DEFAULT_ORGANISATION_ID, MemberStatus, OrganisationStatus
    from app.identity.models import Organisation, OrganisationMember

    await make_active_member(session_factory, COLLEAGUE_PRINCIPAL.member_id)
    task_id = await _step_task(session_factory, execution_settings, COLLEAGUE_PRINCIPAL.member_id)
    async with session_factory() as session:
        if change == "revoked":
            await session.execute(update(OrganisationMember)
                                  .where(OrganisationMember.id == COLLEAGUE_PRINCIPAL.member_id)
                                  .values(status=MemberStatus.REVOKED.value,
                                          revoked_at=datetime.now(timezone.utc)))
        else:
            await session.execute(update(Organisation).where(Organisation.id == DEFAULT_ORGANISATION_ID)
                                  .values(status=OrganisationStatus.SUSPENDED.value))
        await session.commit()
    result = await _run_step(session_factory, execution_settings, COLLEAGUE_PRINCIPAL.member_id, task_id)
    assert result.reason == "owner_not_active"
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Execution))).scalar_one() == 0


@pytest.mark.parametrize("other", OTHERS)
async def test_chat_proposal_lookups_never_find_another_scopes_execution(
    db_session, execution_settings, other
) -> None:
    from app.mail.service import MAIL_TOOL, MailService
    from app.research.service import ResearchService

    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    for tool in ("web_search", MAIL_TOOL):
        db_session.add(Execution(tool_name=tool, arguments={}, state=ExecutionState.PROPOSED,
                                 authorization_status="allowed", idempotency_key=f"k-{tool}",
                                 conversation_id=conversation.id, **ADMIN_SCOPE.write_columns()))
    await db_session.flush()
    assert await ResearchService(db_session, settings=execution_settings,
                                 scope=ADMIN_SCOPE)._pending_for(conversation.id) is not None
    assert await ResearchService(db_session, settings=execution_settings,
                                 scope=other)._pending_for(conversation.id) is None
    assert await MailService(db_session, settings=execution_settings,
                             scope=ADMIN_SCOPE)._pending_for(conversation.id) is not None
    assert await MailService(db_session, settings=execution_settings,
                             scope=other)._pending_for(conversation.id) is None
