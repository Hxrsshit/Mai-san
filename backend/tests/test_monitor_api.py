"""Stage 6N over HTTP: the chat route, the cancel route, and authentication.

The real app with its real authentication seam (`get_owner_id`), on an
in-memory database. A monitoring turn is answered by the application, so the
fake provider must never be asked to generate its reply.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.authorization.models import ApprovalGrant
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService
from app.tasks.models import Task
from app.tasks.requests import MonitorOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from tests.conftest import AUTH_HEADERS

pytestmark = pytest.mark.anyio

REQUEST = (
    "Watch my Gmail for an email from apple.com about invoice and tell me "
    "when it arrives."
)
OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


async def say(client, conversation_id, content):
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": content}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def all_tasks(session_factory):
    async with session_factory() as session:
        return (await session.execute(select(Task))).scalars().all()


# ============================================================================
# A. The chat route
# ============================================================================


async def test_a_request_is_proposed_by_the_application(
    client, conversation_id, fake_provider, session_factory
) -> None:
    generations = len(fake_provider.calls)
    body = await say(client, conversation_id, REQUEST)

    assert body["monitor"]["outcome"] == "proposed"
    assert body["assistant_message"]["content"].startswith("I can set this up.")
    assert len(fake_provider.calls) == generations          # no model wrote it
    [task] = await all_tasks(session_factory)
    assert str(task.id) == body["monitor"]["task_id"]
    assert task.state is TaskState.PLANNED

    detail = (await client.get(f"/api/tasks/{task.id}")).json()
    assert detail["state"] == "planned"


async def test_confirmation_over_http_schedules_and_a_second_yes_falls_through(
    client, conversation_id, fake_provider, session_factory
) -> None:
    await say(client, conversation_id, REQUEST)
    generations = len(fake_provider.calls)
    body = await say(client, conversation_id, "yes, do it")
    assert body["monitor"]["outcome"] == "scheduled"
    assert body["assistant_message"]["content"].startswith("Done. I'm monitoring")
    assert len(fake_provider.calls) == generations

    again = await say(client, conversation_id, "yes")
    assert again["monitor"] is None                           # nothing to replay
    assert len(fake_provider.calls) == generations + 1        # an ordinary turn
    [task] = await all_tasks(session_factory)
    assert task.state is TaskState.QUEUED
    async with session_factory() as session:
        assert len((await session.execute(select(ApprovalGrant))).scalars().all()) == 1


async def test_an_ordinary_turn_reports_no_monitor(client, conversation_id) -> None:
    body = await say(client, conversation_id, "hello there")
    assert body["monitor"] is None


async def test_a_changed_reply_is_invalidated_over_http(
    client, conversation_id, session_factory
) -> None:
    await say(client, conversation_id, REQUEST)
    body = await say(client, conversation_id, "yes, every 30 minutes")
    assert body["monitor"]["outcome"] == "invalidated"
    [task] = await all_tasks(session_factory)
    assert task.state is TaskState.CANCELLED


async def test_the_chat_route_still_requires_authentication(anon_client) -> None:
    response = await anon_client.post(
        f"/api/conversations/{uuid.uuid4()}/messages", json={"content": REQUEST}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


async def test_a_turn_without_an_owner_sets_nothing_up(
    db_session, settings, fake_provider
) -> None:
    """The Telegram path: `send_conversation_turn(owner_id=None)`."""
    conversation = await ConversationService(db_session).create_conversation()
    chat = ChatService(session=db_session, provider=fake_provider, settings=settings)
    *_, monitor = await chat.send_message(conversation.id, REQUEST)
    assert monitor.outcome is MonitorOutcome.UNAVAILABLE
    *_, monitor = await chat.send_message(conversation.id, "yes")
    assert monitor.outcome is MonitorOutcome.NOT_MONITOR
    assert (await db_session.execute(select(Task))).scalars().all() == []


# ============================================================================
# B. The cancel route
# ============================================================================


async def confirmed_over_http(client, conversation_id):
    body = await say(client, conversation_id, REQUEST)
    await say(client, conversation_id, "yes")
    return body["monitor"]["task_id"]


async def test_cancel_cancels_and_releases_the_grant(
    client, conversation_id, session_factory
) -> None:
    task_id = await confirmed_over_http(client, conversation_id)
    response = await client.post(f"/api/tasks/{task_id}/cancel", json={})
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled" and response.json()["id"] == task_id
    async with session_factory() as session:
        [grant] = (await session.execute(select(ApprovalGrant))).scalars().all()
        assert grant.revoked_at is not None

    again = await client.post(f"/api/tasks/{task_id}/cancel", json={})
    assert again.status_code == 200 and again.json()["state"] == "cancelled"


async def test_cancel_requires_exactly_an_empty_object(client, conversation_id) -> None:
    task_id = await confirmed_over_http(client, conversation_id)
    for kwargs in (
        {},                                           # no body
        {"json": {"reason": "x"}},
        {"json": {"owner_id": str(OTHER)}},
        {"json": []},
        {"content": "{}", "headers": {"Content-Type": "text/plain"}},
    ):
        response = await client.post(f"/api/tasks/{task_id}/cancel", **kwargs)
        assert response.status_code == 422, (kwargs, response.status_code)
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert detail["state"] == "queued"


async def test_cancel_is_404_for_unknown_and_for_another_owners_task(
    client, db_session, settings
) -> None:
    theirs = await TaskService(db_session, settings=settings, owner_id=OTHER).create_for_user(
        "Someone else's task"
    )
    await db_session.commit()
    for task_id in (theirs.task_id, uuid.uuid4()):
        response = await client.post(f"/api/tasks/{task_id}/cancel", json={})
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "task_not_found"
    task = (await db_session.execute(
        select(Task).where(Task.id == theirs.task_id)
    )).scalars().one()
    await db_session.refresh(task)
    assert task.state is TaskState.PROPOSED


async def test_cancel_is_409_for_a_finished_task(
    client, conversation_id, session_factory
) -> None:
    task_id = await confirmed_over_http(client, conversation_id)
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.id == uuid.UUID(task_id)).values(
            state=TaskState.COMPLETED, completed_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    response = await client.post(f"/api/tasks/{task_id}/cancel", json={})
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "task_is_terminal"


async def test_cancel_fails_closed_without_credentials(anon_client) -> None:
    response = await anon_client.post(f"/api/tasks/{uuid.uuid4()}/cancel", json={})
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


async def test_cancel_with_a_wrong_token_is_refused(anon_client) -> None:
    response = await anon_client.post(
        f"/api/tasks/{uuid.uuid4()}/cancel", json={},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_auth_headers_are_the_suites_synthetic_token() -> None:
    assert AUTH_HEADERS["Authorization"].startswith("Bearer mai-test-only-")
