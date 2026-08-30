"""Failure isolation for entity extraction.

The governing rule: entity extraction runs after the memory is committed, so
nothing it does may break the chat turn or the memory.
"""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.entities.models import Entity, EntityAlias, MemoryEntity


def memory_payload(*memories) -> str:
    return json.dumps({"should_store_memory": bool(memories), "memories": list(memories)})


def memory(content="User decided to use PostgreSQL for Mai."):
    return {"content": content, "memory_type": "decision",
            "importance_score": 8, "confidence_score": 0.95}


def entity_payload(*entities) -> str:
    return json.dumps({"entities": list(entities)})


def entity(name, entity_type="technology"):
    return {"name": name, "entity_type": entity_type, "confidence_score": 0.95}


async def count(session, model) -> int:
    """Count rows, then release the transaction.

    The in-memory test engine uses StaticPool, so this session shares one
    connection with the request sessions. Leaving a transaction open here
    would block the next API call from starting one. Production pools hand
    out separate connections, so this is a fixture concern only.
    """
    total = int(
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
    )
    await session.rollback()
    return total


# --- Entity failure must not affect chat or memory --------------------------


@pytest.mark.parametrize(
    "error",
    [LLMTimeoutError(), LLMRateLimitError(), LLMAuthError(), LLMResponseError(),
     RuntimeError("entity extraction blew up"), ValueError("unexpected")],
)
async def test_entity_failure_breaks_neither_chat_nor_memory(
    client: AsyncClient, conversation_id, fake_provider, db_session, error
) -> None:
    fake_provider.reply = "Here is your answer."
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_error = error

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Here is your answer."
    # The memory survived; only entities are missing.
    assert (await client.get("/api/memories")).json()["total"] == 1
    assert await count(db_session, Entity) == 0
    assert await count(db_session, MemoryEntity) == 0


@pytest.mark.parametrize(
    "garbage",
    ["", "not json", '{"entities": [', "[1,2,3]", '{"entities":"nope"}', "null"],
)
async def test_malformed_entity_output_stores_nothing(
    client: AsyncClient, conversation_id, fake_provider, db_session, garbage
) -> None:
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = garbage

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 201
    assert (await client.get("/api/memories")).json()["total"] == 1
    assert await count(db_session, Entity) == 0


async def test_invalid_entity_never_reaches_the_database(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(
        {"name": "x", "entity_type": "not_a_type", "confidence_score": 5.0}
    )

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 201
    assert await count(db_session, Entity) == 0
    assert (await client.get("/api/memories")).json()["total"] == 1


async def test_memory_failure_means_no_entity_extraction(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    """No memory, nothing to extract entities from."""
    fake_provider.extraction_error = LLMTimeoutError()
    fake_provider.entity_reply = entity_payload(entity("PostgreSQL"))

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 201
    assert fake_provider.entity_calls == []
    assert await count(db_session, Entity) == 0


async def test_no_memory_means_no_entity_extraction(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Trivial turns produce no memory, so entity extraction never runs."""
    fake_provider.extraction_reply = memory_payload()  # nothing worth storing
    fake_provider.entity_reply = entity_payload(entity("PostgreSQL"))

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Hello!"}
    )

    assert response.status_code == 201
    assert fake_provider.entity_calls == []


async def test_failed_chat_turn_triggers_neither_extraction(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.raise_error = LLMTimeoutError()

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 504
    assert fake_provider.extraction_calls == []
    assert fake_provider.entity_calls == []


# --- Configuration ----------------------------------------------------------


async def test_entity_extraction_can_be_disabled_end_to_end(
    client: AsyncClient, conversation_id, fake_provider, settings, db_session
) -> None:
    settings.ENTITY_EXTRACTION_ENABLED = False
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(entity("PostgreSQL"))

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 201
    # Memory still works; entities are simply not produced.
    assert (await client.get("/api/memories")).json()["total"] == 1
    assert fake_provider.entity_calls == []
    assert await count(db_session, Entity) == 0


async def test_chat_response_is_unchanged_by_entity_extraction(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    fake_provider.reply = "A deterministic reply."
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(entity("PostgreSQL"))
    with_entities = (await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "First."}
    )).json()

    settings.ENTITY_EXTRACTION_ENABLED = False
    without = (await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Second."}
    )).json()

    assert (with_entities["assistant_message"]["content"]
            == without["assistant_message"]["content"])
    assert set(with_entities) == set(without)


# --- Database failure during entity storage ---------------------------------


async def test_database_failure_during_entity_storage_leaves_memory_intact(
    session_factory, fake_provider, settings, monkeypatch
) -> None:
    """The memory is already committed; entity failure must not undo it."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.memory.models import Memory
    from app.memory.tasks import run_memory_extraction
    from app.services.conversation_service import ConversationService

    async with session_factory() as setup:
        conversation = await ConversationService(setup).create_conversation()
        await setup.commit()
        conversation_id = conversation.id

    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(entity("PostgreSQL"))

    real_flush = AsyncSession.flush
    calls = {"n": 0}

    async def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        # Let memory storage through, then fail every entity write.
        if calls["n"] > 2:
            raise OSError("database went away during entity storage")
        return await real_flush(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flaky_flush)

    await run_memory_extraction(
        conversation_id=conversation_id,
        user_message="I decided to use PostgreSQL for Mai.",
        assistant_message="Noted.",
        settings=settings,
        provider=fake_provider,
        session_factory=session_factory,
    )

    monkeypatch.undo()

    async with session_factory() as verify:
        assert await count(verify, Memory) == 1      # memory survived
        assert await count(verify, Entity) == 0      # no partial entity
        assert await count(verify, MemoryEntity) == 0
        assert await count(verify, EntityAlias) == 0
