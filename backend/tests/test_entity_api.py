"""Entity inspection endpoints, driven through real chat turns."""

import json
import uuid

from httpx import AsyncClient


def memory_payload(*memories) -> str:
    return json.dumps({"should_store_memory": bool(memories), "memories": list(memories)})


def memory(content, memory_type="decision"):
    return {"content": content, "memory_type": memory_type,
            "importance_score": 8, "confidence_score": 0.95}


def entity_payload(*entities) -> str:
    return json.dumps({"entities": list(entities)})


def entity(name, entity_type="technology", aliases=None, description=None):
    entry = {"name": name, "entity_type": entity_type, "confidence_score": 0.95}
    if aliases is not None:
        entry["aliases"] = aliases
    if description is not None:
        entry["description"] = description
    return entry


async def turn(client, conversation_id, text, memories, entities) -> None:
    """Drive one full turn so the background task stores memory + entities."""
    client.__dict__.setdefault("_p", None)
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": text}
    )
    assert response.status_code == 201


async def seed(client, conversation_id, fake_provider, memories, entities) -> None:
    fake_provider.extraction_reply = memory_payload(*memories)
    fake_provider.entity_reply = entity_payload(*entities)
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Something worth remembering."},
    )
    assert response.status_code == 201


# --- Listing ----------------------------------------------------------------


async def test_list_is_empty_initially(client: AsyncClient) -> None:
    response = await client.get("/api/entities")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_entities_appear_after_a_turn(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology"), entity("Mai", "project")],
    )

    body = (await client.get("/api/entities")).json()
    assert body["total"] == 2
    names = {item["canonical_name"] for item in body["items"]}
    assert names == {"PostgreSQL", "Mai"}
    item = next(i for i in body["items"] if i["canonical_name"] == "PostgreSQL")
    assert item["entity_type"] == "technology"
    assert item["status"] == "active"
    assert item["normalized_name"] == "postgresql"


async def test_filter_by_entity_type(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology"), entity("Mai", "project")],
    )

    tech = (await client.get("/api/entities?entity_type=technology")).json()
    assert tech["total"] == 1
    assert tech["items"][0]["canonical_name"] == "PostgreSQL"
    assert (await client.get("/api/entities?entity_type=person")).json()["total"] == 0


async def test_filter_by_status(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology")],
    )
    assert (await client.get("/api/entities?status=active")).json()["total"] == 1
    assert (await client.get("/api/entities?status=archived")).json()["total"] == 0


async def test_invalid_filters_are_rejected(client: AsyncClient) -> None:
    assert (await client.get("/api/entities?entity_type=database")).status_code == 422
    assert (await client.get("/api/entities?status=deleted")).status_code == 422
    assert (await client.get("/api/entities?limit=0")).status_code == 422
    assert (await client.get("/api/entities?limit=9999")).status_code == 422


async def test_pagination(client: AsyncClient, conversation_id, fake_provider) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User uses many tools.")],
        [entity(f"Tool Number {i}", "technology") for i in range(4)],
    )
    page = (await client.get("/api/entities?limit=2&offset=0")).json()
    assert len(page["items"]) == 2
    assert page["total"] == 4


# --- Detail -----------------------------------------------------------------


async def test_entity_detail_includes_aliases_and_memory_count(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology", aliases=["Postgres"],
                description="Database used in Mai.")],
    )
    entity_id = (await client.get("/api/entities")).json()["items"][0]["id"]

    detail = (await client.get(f"/api/entities/{entity_id}")).json()

    assert detail["canonical_name"] == "PostgreSQL"
    assert detail["description"] == "Database used in Mai."
    assert [a["alias"] for a in detail["aliases"]] == ["Postgres"]
    assert detail["memory_count"] == 1


async def test_get_unknown_entity_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/entities/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "entity_not_found"


async def test_malformed_entity_id_returns_422(client: AsyncClient) -> None:
    response = await client.get("/api/entities/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- Linked memories --------------------------------------------------------


async def test_entity_memories_endpoint(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology")],
    )
    entity_id = (await client.get("/api/entities")).json()["items"][0]["id"]

    memories = (await client.get(f"/api/entities/{entity_id}/memories")).json()

    assert len(memories) == 1
    assert memories[0]["content"] == "User decided to use PostgreSQL for Mai."
    assert memories[0]["memory_type"] == "decision"


async def test_memories_for_unknown_entity_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/entities/{uuid.uuid4()}/memories")
    assert response.status_code == 404


# --- Deletion ---------------------------------------------------------------


async def test_delete_entity_keeps_the_memory(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology", aliases=["Postgres"])],
    )
    entity_id = (await client.get("/api/entities")).json()["items"][0]["id"]
    assert (await client.get("/api/memories")).json()["total"] == 1

    assert (await client.delete(f"/api/entities/{entity_id}")).status_code == 204

    assert (await client.get(f"/api/entities/{entity_id}")).status_code == 404
    assert (await client.get("/api/entities")).json()["total"] == 0
    # The memory is untouched -- only the link went.
    assert (await client.get("/api/memories")).json()["total"] == 1


async def test_delete_unknown_entity_returns_404(client: AsyncClient) -> None:
    response = await client.delete(f"/api/entities/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "entity_not_found"


async def test_deleting_a_conversation_removes_its_entities_links_not_entities(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Memories cascade with the conversation; entities outlive it."""
    await seed(
        client, conversation_id, fake_provider,
        [memory("User decided to use PostgreSQL for Mai.")],
        [entity("PostgreSQL", "technology")],
    )
    assert (await client.get("/api/entities")).json()["total"] == 1

    await client.delete(f"/api/conversations/{conversation_id}")

    assert (await client.get("/api/memories")).json()["total"] == 0
    # The entity itself survives -- it is knowledge, not conversation content.
    entity_id = (await client.get("/api/entities")).json()["items"][0]["id"]
    assert (await client.get(f"/api/entities/{entity_id}")).json()["memory_count"] == 0
