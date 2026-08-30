"""The context debug endpoint."""

import json
import uuid

from httpx import AsyncClient
from sqlalchemy import event, func, select

from app.entities.models import Entity
from app.memory.models import Memory
from app.relationships.models import Relationship

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})
ENTITIES = json.dumps({"entities": [
    {"name": "Mai", "entity_type": "project", "confidence_score": 0.95},
    {"name": "PostgreSQL", "entity_type": "technology", "confidence_score": 0.95},
]})
RELATIONSHIPS = json.dumps({"relationships": [
    {"source_entity": "Mai", "relationship_type": "USES",
     "target_entity": "PostgreSQL", "confidence_score": 0.95},
]})


async def seed_via_chat(client, conversation_id, provider):
    """Build real knowledge through the normal pipeline."""
    provider.extraction_reply = json.dumps({
        "should_store_memory": True,
        "memories": [{
            "content": "User decided to use PostgreSQL for Mai.",
            "memory_type": "decision", "importance_score": 8,
            "confidence_score": 0.95,
        }],
    })
    provider.entity_reply = ENTITIES
    provider.relationship_reply = RELATIONSHIPS
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )
    assert response.status_code == 201
    provider.extraction_reply = NOTHING_TO_STORE


# --- Assembly through the endpoint ------------------------------------------


async def test_debug_returns_the_assembled_package(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)

    response = await client.post(
        "/api/context/debug",
        json={
            "conversation_id": str(conversation_id),
            "message": "What technology stack am I using for Mai?",
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert body["current_message"] == "What technology stack am I using for Mai?"
    assert body["recent_conversation"], "no recent conversation selected"
    assert body["memories"], "no memories selected"
    assert body["entities"], "no entities selected"
    # Categories are separate, not flattened.
    assert set(body) >= {
        "current_message", "recent_conversation", "memories", "entities",
        "relationships", "metadata",
    }


async def test_debug_reports_counts_budget_and_characters(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)

    body = (await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id),
        "message": "What does Mai use?",
    })).json()

    metadata = body["metadata"]
    assert metadata["recent_message_count"] == len(body["recent_conversation"])
    assert metadata["memory_count"] == len(body["memories"])
    assert metadata["entity_count"] == len(body["entities"])
    assert metadata["relationship_count"] == len(body["relationships"])

    characters = metadata["characters"]
    assert characters["total"] > 0
    assert characters["current_message"] == len("What does Mai use?")

    budget = metadata["budget"]
    assert budget["max_total_chars"] > 0
    assert budget["recent_message_limit"] > 0
    assert "dropped_items" in metadata
    assert metadata["duration_ms"] >= 0


async def test_debug_preserves_retrieval_rank(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)

    body = (await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id),
        "message": "What does Mai use?",
    })).json()

    ranks = [m["retrieval_rank"] for m in body["memories"]]
    assert ranks == sorted(ranks)
    for item in body["memories"] + body["entities"] + body["relationships"]:
        assert item["context_role"] == "reference"


async def test_debug_records_dropped_items(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)
    settings.CONTEXT_MAX_MEMORY_ITEMS = 0

    body = (await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id),
        "message": "What does Mai use?",
    })).json()

    assert body["memories"] == []
    dropped = [d for d in body["metadata"]["dropped_items"] if d["category"] == "memory"]
    assert dropped
    assert dropped[0]["reason"] == "category_limit"


async def test_debug_without_a_conversation_id(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Long-term knowledge only -- no conversation to draw on."""
    await seed_via_chat(client, conversation_id, fake_provider)

    body = (await client.post("/api/context/debug", json={
        "message": "What does Mai use?",
    })).json()

    assert body["recent_conversation"] == []
    assert body["memories"]


async def test_debug_for_an_unknown_conversation_still_returns_a_package(
    client: AsyncClient
) -> None:
    """Degrades rather than failing -- the message always survives."""
    response = await client.post("/api/context/debug", json={
        "conversation_id": str(uuid.uuid4()),
        "message": "Does this still work?",
    })

    assert response.status_code == 200
    assert response.json()["current_message"] == "Does this still work?"


# --- Validation -------------------------------------------------------------


async def test_debug_rejects_an_empty_message(client: AsyncClient) -> None:
    response = await client.post("/api/context/debug", json={"message": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_debug_rejects_a_malformed_conversation_id(client: AsyncClient) -> None:
    response = await client.post("/api/context/debug", json={
        "conversation_id": "not-a-uuid", "message": "hello",
    })
    assert response.status_code == 422


# --- The endpoint's guarantees ----------------------------------------------


async def test_debug_makes_no_model_call(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)
    before = (
        len(fake_provider.calls), len(fake_provider.extraction_calls),
        len(fake_provider.entity_calls), len(fake_provider.relationship_calls),
    )

    await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id), "message": "What does Mai use?",
    })

    after = (
        len(fake_provider.calls), len(fake_provider.extraction_calls),
        len(fake_provider.entity_calls), len(fake_provider.relationship_calls),
    )
    assert before == after


async def test_debug_mutates_nothing(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)

    async def counts():
        out = {}
        for model in (Memory, Entity, Relationship):
            result = await db_session.execute(select(func.count()).select_from(model))
            out[model.__name__] = result.scalar_one()
        await db_session.rollback()
        return out

    before = await counts()
    await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id), "message": "What does Mai use?",
    })
    assert await counts() == before


async def test_debug_issues_no_write_statements(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)

    writes = []
    engine = db_session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    try:
        await client.post("/api/context/debug", json={
            "conversation_id": str(conversation_id), "message": "What does Mai use?",
        })
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert writes == [], f"debug endpoint issued writes: {writes}"


async def test_debug_exposes_no_secrets(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)
    text = (await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id), "message": "What does Mai use?",
    })).text

    for secret in ["gsk_", "api_key", "GROQ_API_KEY", "password", "test-key"]:
        assert secret not in text


async def test_debug_output_contains_no_database_internals(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed_via_chat(client, conversation_id, fake_provider)
    body = (await client.post("/api/context/debug", json={
        "conversation_id": str(conversation_id), "message": "What does Mai use?",
    })).json()

    for entity in body["entities"]:
        assert "normalized_name" not in entity
        assert "aliases" not in entity
    for relationship in body["relationships"]:
        assert "evidence" not in relationship
        assert "source_entity_id" not in relationship
    for memory in body["memories"]:
        assert "normalized_content" not in memory
        assert "source_conversation_id" not in memory
