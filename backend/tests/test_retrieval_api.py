"""Retrieval debug endpoints and query-count / bounded-pool behaviour."""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.entities.models import Entity, EntityAlias, EntityStatus, EntityType, MemoryEntity
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipStatus,
    RelationshipType,
)
from app.services.conversation_service import ConversationService

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})


async def seed(session_factory, memory_count: int = 4):
    async with session_factory() as session:
        conversation = await ConversationService(session).create_conversation()
        mai = Entity(canonical_name="Mai", normalized_name="mai",
                     entity_type=EntityType.PROJECT, status=EntityStatus.ACTIVE)
        postgres = Entity(canonical_name="PostgreSQL", normalized_name="postgresql",
                          entity_type=EntityType.TECHNOLOGY, status=EntityStatus.ACTIVE)
        session.add_all([mai, postgres])
        await session.flush()
        session.add(EntityAlias(entity_id=postgres.id, alias="Postgres",
                                normalized_alias="postgres"))
        session.add(Relationship(
            source_entity_id=mai.id, relationship_type=RelationshipType.USES,
            target_entity_id=postgres.id, confidence_score=0.95,
            status=RelationshipStatus.ACTIVE))

        for index in range(memory_count):
            memory = Memory(
                content=f"Mai uses PostgreSQL for purpose {index}.",
                normalized_content=f"mai uses postgresql for purpose {index}",
                memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                importance_score=7, confidence_score=0.9,
                source_conversation_id=conversation.id,
            )
            session.add(memory)
            await session.flush()
            session.add(MemoryEntity(memory_id=memory.id, entity_id=mai.id))
        await session.commit()
        return conversation.id


# --- Debug endpoint ---------------------------------------------------------


async def test_debug_endpoint_explains_the_whole_pipeline(
    client: AsyncClient, session_factory
) -> None:
    await seed(session_factory)

    response = await client.post(
        "/api/retrieval/debug",
        json={"query": "What database does Mai use?"},
    )

    assert response.status_code == 200
    body = response.json()

    assert body["query"] == "What database does Mai use?"
    assert body["normalized_query"] == "what database does mai use"
    assert "database" in body["keywords"] and "mai" in body["keywords"]
    assert [e["canonical_name"] for e in body["matched_entities"]] == ["Mai"]
    assert body["candidate_memories"], "no candidates surfaced"
    assert body["selected_memories"]
    assert body["assembled_context"].startswith("PERSONAL KNOWLEDGE CONTEXT")
    assert body["metadata"]["context_chars"] > 0
    assert body["weights"]["text_relevance"] == pytest.approx(0.30)


async def test_debug_shows_scores_and_signals(
    client: AsyncClient, session_factory
) -> None:
    await seed(session_factory)

    body = (await client.post(
        "/api/retrieval/debug", json={"query": "Mai PostgreSQL"}
    )).json()

    candidate = body["candidate_memories"][0]
    score = candidate["score"]
    assert set(score) >= {
        "text_relevance", "entity_relevance", "relationship_relevance",
        "importance", "confidence", "recency", "final_score",
    }
    assert 0.0 <= score["final_score"] <= 1.0
    assert candidate["signals"], "no signals recorded"
    assert isinstance(candidate["selected"], bool)


async def test_debug_marks_which_candidates_were_selected(
    client: AsyncClient, session_factory, settings
) -> None:
    settings.RETRIEVAL_MAX_MEMORIES = 2
    await seed(session_factory, memory_count=6)

    body = (await client.post(
        "/api/retrieval/debug", json={"query": "Mai PostgreSQL purpose"}
    )).json()

    selected = [c for c in body["candidate_memories"] if c["selected"]]
    assert len(selected) == 2
    assert len(body["candidate_memories"]) > len(selected)


async def test_debug_rejects_an_empty_query(client: AsyncClient) -> None:
    response = await client.post("/api/retrieval/debug", json={"query": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_debug_on_an_empty_knowledge_base(client: AsyncClient) -> None:
    body = (await client.post(
        "/api/retrieval/debug", json={"query": "anything at all"}
    )).json()
    assert body["selected_memories"] == []
    assert body["assembled_context"] == ""


async def test_debug_exposes_no_secrets(client: AsyncClient, session_factory) -> None:
    await seed(session_factory)
    text = (await client.post(
        "/api/retrieval/debug", json={"query": "Mai"}
    )).text
    for secret in ["gsk_", "api_key", "GROQ_API_KEY", "password"]:
        assert secret not in text


# --- Context preview --------------------------------------------------------


async def test_context_preview_uses_the_last_user_message(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    body = (await client.get(
        f"/api/conversations/{conversation_id}/context-preview"
    )).json()

    assert body["query"] == "What database does Mai use?"
    assert [e["canonical_name"] for e in body["matched_entities"]] == ["Mai"]


async def test_context_preview_accepts_an_explicit_query(
    client: AsyncClient, session_factory
) -> None:
    conversation_id = await seed(session_factory)
    body = (await client.get(
        f"/api/conversations/{conversation_id}/context-preview?query=Postgres"
    )).json()
    assert [e["canonical_name"] for e in body["matched_entities"]] == ["PostgreSQL"]


async def test_context_preview_on_an_empty_conversation(
    client: AsyncClient, conversation_id
) -> None:
    body = (await client.get(
        f"/api/conversations/{conversation_id}/context-preview"
    )).json()
    assert body["query"] == ""
    assert body["assembled_context"] == ""


async def test_context_preview_unknown_conversation_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.get(
        f"/api/conversations/{uuid.uuid4()}/context-preview"
    )
    assert response.status_code == 404


async def test_context_preview_malformed_id_returns_422(client: AsyncClient) -> None:
    response = await client.get("/api/conversations/not-a-uuid/context-preview")
    assert response.status_code == 422


# --- Performance: bounded work ----------------------------------------------


async def test_retrieval_issues_a_bounded_number_of_queries(
    db_session, settings, session_factory
) -> None:
    """No N+1: query count must not grow with the knowledge base."""
    from app.retrieval.service import RetrievalService

    await seed(session_factory, memory_count=60)

    statements = []
    # get_bind() on an AsyncSession returns the *sync* Engine underneath, which
    # is what the event system listens on.
    engine = db_session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    try:
        await RetrievalService(db_session, settings).retrieve(
            "What database does Mai use for purpose 3?"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Eight: entity-by-name, entity-by-alias, relationships, endpoint names,
    # evidence, and the three memory sources. A fixed set, never one per row.
    assert len(statements) <= 8, f"too many queries: {len(statements)}"


async def test_a_large_knowledge_base_stays_bounded(
    db_session, settings, session_factory
) -> None:
    from app.retrieval.service import RetrievalService

    settings.RETRIEVAL_CANDIDATE_POOL_SIZE = 25
    await seed(session_factory, memory_count=150)

    package = await RetrievalService(db_session, settings).retrieve(
        "Mai PostgreSQL purpose"
    )

    assert package.metadata.candidate_memories <= 25
    assert len(package.memories) <= settings.RETRIEVAL_MAX_MEMORIES
    assert package.metadata.context_chars <= settings.RETRIEVAL_MAX_CONTEXT_CHARS


async def add_memories(session_factory, conversation_id, start: int, count: int):
    """Add more memories linked to the existing Mai entity."""
    async with session_factory() as session:
        mai = (
            await session.execute(
                Entity.__table__.select().where(Entity.normalized_name == "mai")
            )
        ).first()
        for index in range(start, start + count):
            memory = Memory(
                content=f"Mai uses PostgreSQL for extra purpose {index}.",
                normalized_content=f"mai uses postgresql for extra purpose {index}",
                memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                importance_score=7, confidence_score=0.9,
                source_conversation_id=conversation_id,
            )
            session.add(memory)
            await session.flush()
            session.add(MemoryEntity(memory_id=memory.id, entity_id=mai.id))
        await session.commit()


async def test_query_count_does_not_grow_with_the_knowledge_base(
    db_session, settings, session_factory
) -> None:
    """The N+1 guarantee, stated as a comparison rather than a constant."""
    from app.retrieval.service import RetrievalService

    engine = db_session.get_bind()

    async def count_queries() -> int:
        # StaticPool shares one connection with the seeding sessions, so this
        # session must not be holding a transaction while they run.
        await db_session.rollback()
        statements = []

        @event.listens_for(engine, "before_cursor_execute")
        def record(conn, cursor, statement, params, context, executemany):
            if statement.strip().upper().startswith("SELECT"):
                statements.append(statement)

        try:
            await RetrievalService(db_session, settings).retrieve(
                "What database does Mai use?"
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        await db_session.rollback()
        return len(statements)

    conversation_id = await seed(session_factory, memory_count=5)
    small = await count_queries()

    await add_memories(session_factory, conversation_id, start=100, count=200)
    large = await count_queries()

    assert small == large, f"query count grew with data: {small} -> {large}"
