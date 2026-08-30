"""Stage 3D: database integrity under failure and concurrency.

Failures are induced, not simulated in prose: the driver is made to fail at
specific points and the resulting state is inspected for partial writes.
"""

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database.models import Conversation, Message
from app.entities.models import Entity, EntityStatus, EntityType, MemoryEntity
from app.knowledge.models import (
    ConflictReason,
    ConflictResolution,
    KnowledgeConflict,
)
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)
from app.services.conversation_service import ConversationService

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})


async def count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
async def concurrent_app(tmp_path):
    """A real engine against a file, for tests that need true concurrency.

    The shared in-memory fixture routes every session through one StaticPool
    connection, so concurrent writers collide inside SQLite itself ("cannot
    start a transaction within a transaction") rather than exercising the
    application's own locking. That is a property of the fixture, not of
    production, which opens a connection per session.
    """
    import pytest_asyncio  # noqa: F401  (fixture style matches the project)

    from app.core.config import Settings, get_settings
    from app.database.metadata import Base
    from app.database.session import dispose_engine, init_engine
    from app.llm.factory import get_llm_provider
    from app.main import create_app

    from tests.conftest import FakeLLMProvider

    settings = Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'security.db'}",
        GROQ_API_KEY="test-key",
        LOG_LEVEL="INFO",
    )
    engine = init_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    provider = FakeLLMProvider()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm_provider] = lambda: provider

    yield app, provider, engine

    app.dependency_overrides.clear()
    await dispose_engine()


# --- Referential integrity --------------------------------------------------


async def test_foreign_keys_are_enforced(db_session) -> None:
    """SQLite ignores foreign keys unless PRAGMA foreign_keys is on."""
    db_session.add(
        Message(
            conversation_id=uuid.uuid4(),  # no such conversation
            role="user",
            content="orphan",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_deleting_a_conversation_cascades_to_everything_derived(
    db_session,
) -> None:
    """No orphaned message, memory, entity link, evidence or lifecycle row."""
    conversation = await ConversationService(db_session).create_conversation()
    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="hello")
    )
    memory = Memory(
        content="A memory.", normalized_content="a memory",
        memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
        importance_score=8, confidence_score=0.9,
        source_conversation_id=conversation.id,
    )
    entity = Entity(
        canonical_name="Thing", normalized_name="thing",
        entity_type=EntityType.OTHER, status=EntityStatus.ACTIVE,
    )
    other = Entity(
        canonical_name="Other", normalized_name="other",
        entity_type=EntityType.OTHER, status=EntityStatus.ACTIVE,
    )
    db_session.add_all([memory, entity, other])
    await db_session.flush()

    relationship = Relationship(
        source_entity_id=entity.id, relationship_type=RelationshipType.USES,
        target_entity_id=other.id, confidence_score=0.9,
        status=RelationshipStatus.ACTIVE,
    )
    db_session.add_all([
        MemoryEntity(memory_id=memory.id, entity_id=entity.id),
        relationship,
    ])
    await db_session.flush()
    db_session.add(
        RelationshipEvidence(relationship_id=relationship.id, memory_id=memory.id)
    )
    second = Memory(
        content="Second.", normalized_content="second",
        memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
        importance_score=8, confidence_score=0.9,
        source_conversation_id=conversation.id,
    )
    db_session.add(second)
    await db_session.flush()
    db_session.add(
        KnowledgeConflict(
            older_memory_id=memory.id, newer_memory_id=second.id,
            resolution=ConflictResolution.SUPERSEDED,
            reason=ConflictReason.EXPLICIT_REPLACEMENT,
        )
    )
    await db_session.flush()

    await db_session.delete(
        await db_session.get(Conversation, conversation.id)
    )
    await db_session.flush()

    assert await count(db_session, Message) == 0
    assert await count(db_session, Memory) == 0
    assert await count(db_session, MemoryEntity) == 0
    assert await count(db_session, RelationshipEvidence) == 0
    assert await count(db_session, KnowledgeConflict) == 0


async def test_evidence_cannot_reference_a_missing_relationship(db_session) -> None:
    conversation = await ConversationService(db_session).create_conversation()
    memory = Memory(
        content="M.", normalized_content="m", memory_type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE, importance_score=8, confidence_score=0.9,
        source_conversation_id=conversation.id,
    )
    db_session.add(memory)
    await db_session.flush()

    db_session.add(
        RelationshipEvidence(relationship_id=uuid.uuid4(), memory_id=memory.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_a_lifecycle_link_cannot_reference_a_missing_memory(
    db_session,
) -> None:
    conversation = await ConversationService(db_session).create_conversation()
    memory = Memory(
        content="M.", normalized_content="m", memory_type=MemoryType.SEMANTIC,
        status=MemoryStatus.ACTIVE, importance_score=8, confidence_score=0.9,
        source_conversation_id=conversation.id,
    )
    db_session.add(memory)
    await db_session.flush()

    db_session.add(
        KnowledgeConflict(
            older_memory_id=memory.id,
            newer_memory_id=uuid.uuid4(),  # no such memory
            resolution=ConflictResolution.SUPERSEDED,
            reason=ConflictReason.EXPLICIT_REPLACEMENT,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_a_relationship_cannot_point_at_itself(db_session) -> None:
    entity = Entity(
        canonical_name="Solo", normalized_name="solo",
        entity_type=EntityType.OTHER, status=EntityStatus.ACTIVE,
    )
    db_session.add(entity)
    await db_session.flush()

    db_session.add(
        Relationship(
            source_entity_id=entity.id, relationship_type=RelationshipType.USES,
            target_entity_id=entity.id, confidence_score=0.9,
            status=RelationshipStatus.ACTIVE,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize(
    "importance,confidence", [(0, 0.9), (11, 0.9), (5, -0.1), (5, 1.5)]
)
async def test_out_of_range_scores_are_refused_by_the_database(
    db_session, importance, confidence
) -> None:
    """The last line of defence against bad model output."""
    conversation = await ConversationService(db_session).create_conversation()
    db_session.add(
        Memory(
            content="M.", normalized_content=f"m{importance}{confidence}",
            memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
            importance_score=importance, confidence_score=confidence,
            source_conversation_id=conversation.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --- Transaction failure ----------------------------------------------------


async def test_a_failure_mid_turn_leaves_no_partial_conversation(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    from app.core.errors import LLMTimeoutError

    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    fake_provider.raise_error = LLMTimeoutError("timed out")

    response = await client.post(
        f"/api/conversations/{conversation}/messages",
        json={"content": "will fail"},
    )
    assert response.status_code >= 400

    async with session_factory() as session:
        assert await count(session, Message) == 0
        # The conversation itself survives -- it was committed earlier.
        assert await count(session, Conversation) == 1


async def test_a_failure_during_extraction_leaves_no_partial_knowledge(
    client: AsyncClient, conversation_id, fake_provider, session_factory
) -> None:
    """Entity extraction fails after memories are committed."""
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "Mai uses PostgreSQL.",
                    "memory_type": "semantic",
                    "importance_score": 8,
                    "confidence_score": 0.9,
                }
            ],
        }
    )
    fake_provider.entity_error = RuntimeError("entity extraction is down")

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I use PostgreSQL."},
    )
    assert response.status_code == 201

    async with session_factory() as session:
        # The memory committed; entities did not. No half-written link.
        assert await count(session, Memory) == 1
        assert await count(session, MemoryEntity) == 0
        assert await count(session, Relationship) == 0


async def test_a_rolled_back_turn_does_not_advance_the_knowledge_base(
    client: AsyncClient, session_factory, fake_provider
) -> None:
    from app.core.errors import LLMRateLimitError

    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    fake_provider.raise_error = LLMRateLimitError("slow down")

    for _ in range(3):
        await client.post(
            f"/api/conversations/{conversation}/messages",
            json={"content": "repeated failure"},
        )

    async with session_factory() as session:
        assert await count(session, Message) == 0
        assert await count(session, Memory) == 0


# --- Concurrency ------------------------------------------------------------


async def test_concurrent_identical_turns_do_not_duplicate_knowledge(
    concurrent_app,
) -> None:
    """The uniqueness constraints, not application checks, decide this."""
    from httpx import ASGITransport

    app, provider, engine = concurrent_app
    provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "Mai uses PostgreSQL for storage.",
                    "memory_type": "semantic",
                    "importance_score": 8,
                    "confidence_score": 0.9,
                }
            ],
        }
    )
    provider.entity_reply = json.dumps(
        {
            "entities": [
                {"name": "Mai", "entity_type": "project", "confidence_score": 0.95},
                {
                    "name": "PostgreSQL",
                    "entity_type": "technology",
                    "confidence_score": 0.95,
                },
            ]
        }
    )
    provider.relationship_reply = json.dumps(
        {
            "relationships": [
                {
                    "source_entity": "Mai",
                    "relationship_type": "USES",
                    "target_entity": "PostgreSQL",
                    "confidence_score": 0.93,
                }
            ]
        }
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversations = [
            (await client.post("/api/conversations", json={})).json()["id"]
            for _ in range(6)
        ]
        results = await asyncio.gather(
            *[
                client.post(
                    f"/api/conversations/{cid}/messages",
                    json={"content": f"I use PostgreSQL for Mai ({index})."},
                )
                for index, cid in enumerate(conversations)
            ],
            return_exceptions=True,
        )
        assert all(getattr(r, "status_code", None) == 201 for r in results), results

        assert (await client.get("/api/memories")).json()["total"] == 1
        entities = (await client.get("/api/entities")).json()["items"]
        names = [entity["canonical_name"] for entity in entities]
        assert len(names) == len(set(names)), f"duplicate entities: {names}"
        assert len((await client.get("/api/relationships")).json()["items"]) <= 1


async def test_concurrent_deletes_of_the_same_record_are_safe(
    concurrent_app,
) -> None:
    from httpx import ASGITransport

    app, _, engine = concurrent_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversation = (
            await client.post("/api/conversations", json={})
        ).json()["id"]

        results = await asyncio.gather(
            *[client.delete(f"/api/conversations/{conversation}") for _ in range(5)],
            return_exceptions=True,
        )
        codes = [getattr(r, "status_code", None) for r in results]

    # Exactly one deletion succeeds; the rest see it already gone. No 500.
    assert codes.count(204) == 1, codes
    assert all(code in (204, 404) for code in codes), codes

    async with engine.connect() as connection:
        remaining = (
            await connection.execute(select(func.count()).select_from(Conversation))
        ).scalar_one()
    assert remaining == 0


async def test_a_delete_racing_a_read_does_not_corrupt_state(
    concurrent_app,
) -> None:
    from httpx import ASGITransport

    app, provider, engine = concurrent_app
    provider.extraction_reply = NOTHING_TO_STORE

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversation = (
            await client.post("/api/conversations", json={})
        ).json()["id"]
        await client.post(
            f"/api/conversations/{conversation}/messages",
            json={"content": "hello"},
        )

        await asyncio.gather(
            client.delete(f"/api/conversations/{conversation}"),
            client.get(f"/api/conversations/{conversation}"),
            client.get(f"/api/conversations/{conversation}/messages"),
            return_exceptions=True,
        )

    async with engine.connect() as connection:
        conversations = (
            await connection.execute(select(func.count()).select_from(Conversation))
        ).scalar_one()
        messages = (
            await connection.execute(select(func.count()).select_from(Message))
        ).scalar_one()

    # Whatever the interleaving, no message outlives its conversation.
    assert conversations == 0
    assert messages == 0
