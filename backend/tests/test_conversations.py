"""Conversation CRUD endpoints."""

import uuid

from httpx import AsyncClient


async def test_create_conversation_uses_a_default_title(client: AsyncClient) -> None:
    response = await client.post("/api/conversations", json={})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "New conversation"
    uuid.UUID(body["id"])  # must be a valid UUID
    assert body["created_at"] and body["updated_at"]


async def test_create_conversation_accepts_a_title(client: AsyncClient) -> None:
    response = await client.post("/api/conversations", json={"title": "Roadmap"})

    assert response.status_code == 201
    assert response.json()["title"] == "Roadmap"


async def test_list_conversations_is_empty_initially(client: AsyncClient) -> None:
    response = await client.get("/api/conversations")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_list_conversations_returns_all_created(client: AsyncClient) -> None:
    for title in ("First", "Second", "Third"):
        await client.post("/api/conversations", json={"title": title})

    body = (await client.get("/api/conversations")).json()

    assert body["total"] == 3
    assert {item["title"] for item in body["items"]} == {"First", "Second", "Third"}


async def test_list_conversations_paginates(client: AsyncClient) -> None:
    for index in range(5):
        await client.post("/api/conversations", json={"title": f"C{index}"})

    body = (await client.get("/api/conversations?limit=2&offset=0")).json()

    assert len(body["items"]) == 2
    assert body["total"] == 5


async def test_get_conversation_returns_it_with_no_messages(
    client: AsyncClient, conversation_id
) -> None:
    response = await client.get(f"/api/conversations/{conversation_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(conversation_id)
    assert body["messages"] == []


async def test_get_unknown_conversation_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/conversations/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


async def test_get_conversation_with_a_malformed_id_returns_422(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/conversations/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_rename_conversation(client: AsyncClient, conversation_id) -> None:
    response = await client.patch(
        f"/api/conversations/{conversation_id}", json={"title": "Renamed"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"


async def test_delete_conversation_removes_it(
    client: AsyncClient, conversation_id
) -> None:
    assert (
        await client.delete(f"/api/conversations/{conversation_id}")
    ).status_code == 204

    assert (
        await client.get(f"/api/conversations/{conversation_id}")
    ).status_code == 404


async def test_delete_unknown_conversation_returns_404(client: AsyncClient) -> None:
    response = await client.delete(f"/api/conversations/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


async def test_deleting_a_conversation_cascades_to_its_messages(
    client: AsyncClient, conversation_id, db_session
) -> None:
    from sqlalchemy import func, select

    from app.database.models import Message

    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Hi"}
    )
    await client.delete(f"/api/conversations/{conversation_id}")

    remaining = await db_session.execute(
        select(func.count()).select_from(Message).where(
            Message.conversation_id == conversation_id
        )
    )
    assert remaining.scalar_one() == 0
