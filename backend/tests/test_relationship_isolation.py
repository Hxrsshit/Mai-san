"""Failure isolation for relationship extraction.

Relationship extraction runs after the memory and its entities are committed,
so nothing it does may break the chat turn, the memory, or the entities.
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
from app.entities.models import Entity
from app.relationships.models import Relationship, RelationshipEvidence


def memory_payload(*memories) -> str:
    return json.dumps({"should_store_memory": bool(memories), "memories": list(memories)})


def memory(content="Mai uses PostgreSQL for storage."):
    return {"content": content, "memory_type": "decision",
            "importance_score": 8, "confidence_score": 0.95}


def entity_payload(*entities) -> str:
    return json.dumps({"entities": list(entities)})


def entity(name, kind="technology"):
    return {"name": name, "entity_type": kind, "confidence_score": 0.95}


def relationship_payload(*relationships) -> str:
    return json.dumps({"relationships": list(relationships)})


def relationship(source, kind, target):
    return {"source_entity": source, "relationship_type": kind,
            "target_entity": target, "confidence_score": 0.93}


ENTITIES = [entity("Mai", "project"), entity("PostgreSQL", "technology")]


async def count(session, model) -> int:
    total = int((await session.execute(select(func.count()).select_from(model))).scalar_one())
    # StaticPool shares one connection with request sessions; release it.
    await session.rollback()
    return total


async def send(client, conversation_id, provider, text="I use PostgreSQL for Mai."):
    return await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": text}
    )


# --- Relationship failure must not affect earlier stages --------------------


@pytest.mark.parametrize(
    "error",
    [LLMTimeoutError(), LLMRateLimitError(), LLMAuthError(), LLMResponseError(),
     RuntimeError("relationship extraction blew up"), ValueError("unexpected")],
)
async def test_failure_breaks_neither_chat_memory_nor_entities(
    client: AsyncClient, conversation_id, fake_provider, db_session, error
) -> None:
    fake_provider.reply = "Here is your answer."
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_error = error

    response = await send(client, conversation_id, fake_provider)

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Here is your answer."
    assert (await client.get("/api/memories")).json()["total"] == 1
    assert (await client.get("/api/entities")).json()["total"] == 2
    assert await count(db_session, Relationship) == 0
    assert await count(db_session, RelationshipEvidence) == 0


@pytest.mark.parametrize(
    "garbage",
    ["", "not json", '{"relationships": [', "[1,2,3]", '{"relationships":"nope"}', "null"],
)
async def test_malformed_output_stores_nothing(
    client: AsyncClient, conversation_id, fake_provider, db_session, garbage
) -> None:
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = garbage

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    assert (await client.get("/api/entities")).json()["total"] == 2
    assert await count(db_session, Relationship) == 0


async def test_invalid_relationship_never_reaches_the_database(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = relationship_payload(
        {"source_entity": "Mai", "relationship_type": "FROBNICATES",
         "target_entity": "PostgreSQL", "confidence_score": 5.0}
    )

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    assert await count(db_session, Relationship) == 0


async def test_relationship_naming_an_unknown_entity_is_rejected(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    """No entity may be created by the relationship system."""
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = relationship_payload(
        relationship("Mai", "USES", "MongoDB")
    )

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    assert await count(db_session, Relationship) == 0
    # Only the two extracted entities exist -- MongoDB was not created.
    names = {e["canonical_name"] for e in (await client.get("/api/entities")).json()["items"]}
    assert "MongoDB" not in names


# --- Upstream failures skip relationship extraction -------------------------


async def test_entity_failure_means_no_relationship_extraction(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_error = LLMTimeoutError()
    fake_provider.relationship_reply = relationship_payload(
        relationship("Mai", "USES", "PostgreSQL")
    )

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    # No entities, so fewer than two are available and the model is not asked.
    assert fake_provider.relationship_calls == []
    assert await count(db_session, Relationship) == 0
    assert (await client.get("/api/memories")).json()["total"] == 1


async def test_memory_failure_means_no_relationship_extraction(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    fake_provider.extraction_error = LLMTimeoutError()

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []
    assert await count(db_session, Relationship) == 0


async def test_failed_chat_turn_triggers_no_extraction_at_all(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    fake_provider.raise_error = LLMTimeoutError()

    assert (await send(client, conversation_id, fake_provider)).status_code == 504
    assert fake_provider.extraction_calls == []
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []
    assert await count(db_session, Relationship) == 0


async def test_single_entity_memory_skips_extraction(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """"User prefers concise answers." -- nothing to relate."""
    fake_provider.extraction_reply = memory_payload(
        memory("User prefers concise answers.")
    )
    fake_provider.entity_reply = entity_payload(entity("Mai", "project"))

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    assert fake_provider.relationship_calls == []


# --- Configuration ----------------------------------------------------------


async def test_relationship_extraction_can_be_disabled(
    client: AsyncClient, conversation_id, fake_provider, settings, db_session
) -> None:
    settings.RELATIONSHIP_EXTRACTION_ENABLED = False
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = relationship_payload(
        relationship("Mai", "USES", "PostgreSQL")
    )

    assert (await send(client, conversation_id, fake_provider)).status_code == 201
    # Everything upstream still works.
    assert (await client.get("/api/memories")).json()["total"] == 1
    assert (await client.get("/api/entities")).json()["total"] == 2
    assert fake_provider.relationship_calls == []
    assert await count(db_session, Relationship) == 0


async def test_chat_response_is_unchanged_by_relationship_extraction(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    fake_provider.reply = "A deterministic reply."
    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = relationship_payload(
        relationship("Mai", "USES", "PostgreSQL")
    )
    with_rel = (await send(client, conversation_id, fake_provider, "First.")).json()

    settings.RELATIONSHIP_EXTRACTION_ENABLED = False
    without = (await send(client, conversation_id, fake_provider, "Second.")).json()

    assert (with_rel["assistant_message"]["content"]
            == without["assistant_message"]["content"])
    assert set(with_rel) == set(without)


# --- Database failure -------------------------------------------------------


@pytest.mark.parametrize("fail_after_flush", [4, 5, 6, 7, 8])
async def test_database_failure_never_leaves_a_relationship_without_evidence(
    session_factory, fake_provider, settings, monkeypatch, fail_after_flush
) -> None:
    """The core integrity invariant, checked at every point it can break.

    A relationship and its first evidence row are written in one savepoint, so
    whatever the database does, the pair is all-or-nothing. Failing at
    different flushes walks the failure through memory storage, entity
    storage and relationship storage in turn.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.memory.tasks import run_memory_extraction
    from app.services.conversation_service import ConversationService

    async with session_factory() as setup:
        conversation = await ConversationService(setup).create_conversation()
        await setup.commit()
        conversation_id = conversation.id

    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = relationship_payload(
        relationship("Mai", "USES", "PostgreSQL")
    )

    real_flush = AsyncSession.flush
    calls = {"n": 0}

    async def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > fail_after_flush:
            raise OSError("database went away")
        return await real_flush(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flaky_flush)

    # Must not raise, whatever the database does.
    await run_memory_extraction(
        conversation_id=conversation_id,
        user_message="I use PostgreSQL for Mai.",
        assistant_message="Noted.",
        settings=settings,
        provider=fake_provider,
        session_factory=session_factory,
    )

    monkeypatch.undo()

    async with session_factory() as verify:
        relationships = await count(verify, Relationship)
        evidence = await count(verify, RelationshipEvidence)

        # Never a relationship without the evidence that justifies it.
        assert not (relationships and not evidence), (
            f"orphan relationship: {relationships} relationships, "
            f"{evidence} evidence rows"
        )
        # Never evidence pointing at a relationship that does not exist.
        assert not (evidence and not relationships)


async def test_a_mid_relationship_failure_stores_nothing(
    session_factory, fake_provider, settings, monkeypatch
) -> None:
    """Failing exactly during relationship storage leaves earlier stages intact."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.memory.models import Memory
    from app.memory.tasks import run_memory_extraction
    from app.services.conversation_service import ConversationService

    async with session_factory() as setup:
        conversation = await ConversationService(setup).create_conversation()
        await setup.commit()
        conversation_id = conversation.id

    fake_provider.extraction_reply = memory_payload(memory())
    fake_provider.entity_reply = entity_payload(*ENTITIES)
    fake_provider.relationship_reply = relationship_payload(
        relationship("Mai", "USES", "PostgreSQL")
    )

    real_flush = AsyncSession.flush
    calls = {"n": 0}

    async def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        # Memory and entity writes take six flushes; fail on the seventh,
        # which is the relationship insert.
        if calls["n"] > 6:
            raise OSError("database went away during relationship storage")
        return await real_flush(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flaky_flush)

    await run_memory_extraction(
        conversation_id=conversation_id,
        user_message="I use PostgreSQL for Mai.",
        assistant_message="Noted.",
        settings=settings,
        provider=fake_provider,
        session_factory=session_factory,
    )

    monkeypatch.undo()

    async with session_factory() as verify:
        assert await count(verify, Memory) == 1        # memory survived
        assert await count(verify, Entity) == 2        # entities survived
        assert await count(verify, Relationship) == 0  # nothing partial
        assert await count(verify, RelationshipEvidence) == 0
