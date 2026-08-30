"""Memory inspection endpoints."""

import json
import uuid

import pytest
from httpx import AsyncClient


def payload(*memories) -> str:
    return json.dumps(
        {"should_store_memory": bool(memories), "memories": list(memories)}
    )


def candidate(content, memory_type="preference", importance=8, confidence=0.92) -> dict:
    return {
        "content": content,
        "memory_type": memory_type,
        "importance_score": importance,
        "confidence_score": confidence,
    }


async def seed(client: AsyncClient, conversation_id, fake_provider, *entries) -> None:
    """Drive a real chat turn so the background task stores the memories."""
    fake_provider.extraction_reply = payload(*entries)
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Something worth remembering."},
    )
    assert response.status_code == 201


# --- Listing ----------------------------------------------------------------


async def test_list_is_empty_initially(client: AsyncClient) -> None:
    response = await client.get("/api/memories")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_stored_memory_appears_in_the_list(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client,
        conversation_id,
        fake_provider,
        candidate("User prefers concise explanations.", "preference", 8, 0.93),
    )

    body = (await client.get("/api/memories")).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["content"] == "User prefers concise explanations."
    assert item["memory_type"] == "preference"
    assert item["status"] == "active"
    assert item["importance_score"] == 8
    assert item["confidence_score"] == pytest.approx(0.93)
    assert item["source_conversation_id"] == str(conversation_id)
    assert item["source_message_id"] is not None
    assert item["created_at"] and item["updated_at"]


async def test_filter_by_memory_type(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client,
        conversation_id,
        fake_provider,
        candidate("User prefers concise explanations.", "preference"),
        candidate("User wants to build a personal AI environment.", "goal"),
    )

    goals = (await client.get("/api/memories?memory_type=goal")).json()
    assert goals["total"] == 1
    assert goals["items"][0]["memory_type"] == "goal"

    prefs = (await client.get("/api/memories?memory_type=preference")).json()
    assert prefs["total"] == 1


async def test_filter_by_status(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        candidate("User prefers concise explanations."),
    )

    assert (await client.get("/api/memories?status=active")).json()["total"] == 1
    assert (await client.get("/api/memories?status=archived")).json()["total"] == 0


async def test_invalid_filter_values_are_rejected(client: AsyncClient) -> None:
    assert (await client.get("/api/memories?memory_type=nonsense")).status_code == 422
    assert (await client.get("/api/memories?status=nonsense")).status_code == 422


async def test_pagination(client: AsyncClient, conversation_id, fake_provider) -> None:
    await seed(
        client,
        conversation_id,
        fake_provider,
        *[
            candidate(f"User knows fact number {i} about their work.", "semantic")
            for i in range(4)
        ],
    )

    page = (await client.get("/api/memories?limit=2&offset=0")).json()
    assert len(page["items"]) == 2
    assert page["total"] == 4


# --- Retrieval --------------------------------------------------------------


async def test_get_one_memory(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        candidate("User prefers concise explanations."),
    )
    memory_id = (await client.get("/api/memories")).json()["items"][0]["id"]

    response = await client.get(f"/api/memories/{memory_id}")

    assert response.status_code == 200
    assert response.json()["id"] == memory_id


async def test_get_unknown_memory_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/memories/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "memory_not_found"


async def test_malformed_memory_id_returns_422(client: AsyncClient) -> None:
    response = await client.get("/api/memories/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- Deletion ---------------------------------------------------------------


async def test_delete_memory(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        candidate("User prefers concise explanations."),
    )
    memory_id = (await client.get("/api/memories")).json()["items"][0]["id"]

    assert (await client.delete(f"/api/memories/{memory_id}")).status_code == 204
    assert (await client.get(f"/api/memories/{memory_id}")).status_code == 404
    assert (await client.get("/api/memories")).json()["total"] == 0


async def test_delete_unknown_memory_returns_404(client: AsyncClient) -> None:
    response = await client.delete(f"/api/memories/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "memory_not_found"


async def test_deleting_a_conversation_removes_its_memories_via_api(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        candidate("User prefers concise explanations."),
    )
    assert (await client.get("/api/memories")).json()["total"] == 1

    await client.delete(f"/api/conversations/{conversation_id}")

    assert (await client.get("/api/memories")).json()["total"] == 0
