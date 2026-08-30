"""Regression test for write contention between chat and memory extraction.

Post-turn extraction runs on its own database connection while the chat
request is still writing messages on another. SQLite allows a single writer,
so the two collided and the *chat* request lost -- returning 503
"database is locked" to the user. That breaks the core Stage 2A rule that
memory extraction must never affect the chat turn.

These tests use a file-backed SQLite database on purpose: the in-memory
fixture uses StaticPool and therefore a single shared connection, which cannot
reproduce contention.
"""

import asyncio
import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.database.metadata import Base
from app.database.session import dispose_engine, init_engine
from app.llm.factory import get_llm_provider
from app.main import create_app

from tests.conftest import FakeLLMProvider

ENTITY_REPLY = json.dumps(
    {
        "entities": [
            {
                "name": "PostgreSQL",
                "entity_type": "technology",
                "aliases": ["Postgres"],
                "confidence_score": 0.95,
            },
            {
                "name": "Mai",
                "entity_type": "project",
                "confidence_score": 0.95,
            },
        ]
    }
)

RELATIONSHIP_REPLY = json.dumps(
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

EXTRACTION_REPLY = json.dumps(
    {
        "should_store_memory": True,
        "memories": [
            {
                "content": "User is running a database concurrency probe.",
                "memory_type": "semantic",
                "importance_score": 7,
                "confidence_score": 0.9,
            }
        ],
    }
)


@pytest_asyncio.fixture
async def file_backed_app(tmp_path):
    """A real engine against a temporary database file."""
    database_path = tmp_path / "contention.db"
    settings = Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite+aiosqlite:///{database_path}",
        GROQ_API_KEY="test-key",
    )

    engine = init_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    provider = FakeLLMProvider()
    provider.extraction_reply = EXTRACTION_REPLY
    provider.entity_reply = ENTITY_REPLY
    provider.relationship_reply = RELATIONSHIP_REPLY

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm_provider] = lambda: provider

    yield app, provider, engine

    app.dependency_overrides.clear()
    await dispose_engine()


async def test_sqlite_pragmas_are_applied(file_backed_app) -> None:
    """Without these, concurrent writers fail instead of waiting."""
    _, _, engine = file_backed_app

    async with engine.connect() as connection:
        journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
        busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar()
        foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout >= 5000
    assert foreign_keys == 1


async def test_concurrent_turns_are_not_broken_by_extraction(
    file_backed_app,
) -> None:
    """The regression: a chat turn must never fail because of extraction."""
    app, provider, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversations = []
        for _ in range(8):
            response = await client.post("/api/conversations", json={})
            conversations.append(response.json()["id"])

        async def turn(conversation_id: str, index: int):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"Concurrency probe number {index} for Mai."},
            )

        results = await asyncio.gather(
            *[turn(cid, i) for i, cid in enumerate(conversations)],
            return_exceptions=True,
        )

        failures = [r for r in results if getattr(r, "status_code", None) != 201]
        assert not failures, f"chat turns broken by contention: {failures}"

        # Extraction still did its job on the contended database.
        stored = (await client.get("/api/memories")).json()
        assert stored["total"] >= 1


async def test_extraction_writes_do_not_block_the_response(file_backed_app) -> None:
    """A single turn still succeeds end to end on a file-backed database."""
    app, provider, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversation_id = (
            await client.post("/api/conversations", json={})
        ).json()["id"]

        response = await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "I am probing the database."},
        )

        assert response.status_code == 201
        assert response.json()["assistant_message"]["content"] == provider.reply

        memories = (await client.get("/api/memories")).json()
        assert memories["total"] == 1
        assert memories["items"][0]["source_conversation_id"] == conversation_id


async def test_rapid_turns_in_one_conversation_do_not_duplicate(
    file_backed_app,
) -> None:
    """Concurrent turns in the SAME conversation must not race deduplication.

    Each turn's extraction opens its own connection, so several can be
    deduplicating against the same memory at once.
    """
    app, provider, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversation_id = (
            await client.post("/api/conversations", json={})
        ).json()["id"]

        async def turn(index: int):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"Rapid probe message number {index}."},
            )

        results = await asyncio.gather(
            *[turn(i) for i in range(6)], return_exceptions=True
        )

        assert all(getattr(r, "status_code", None) == 201 for r in results)

        # Every turn proposes the identical memory; at most one may be stored.
        memories = (await client.get("/api/memories")).json()
        assert memories["total"] <= 1, (
            f"deduplication raced: {memories['total']} copies stored"
        )

        # The conversation itself is intact: 6 turns x 2 messages.
        conversation = (
            await client.get(f"/api/conversations/{conversation_id}")
        ).json()
        assert len(conversation["messages"]) == 12


# --- Entities under concurrency ---------------------------------------------
# The Stage 2A audit found that application-level uniqueness checks lose races:
# concurrent tasks each query before either commits. Entities carry a UNIQUE
# constraint on normalized_name for exactly this reason.


async def test_concurrent_turns_do_not_duplicate_entities(file_backed_app) -> None:
    """Every turn proposes the same entity; only one may be created."""
    app, _, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversations = []
        for _ in range(6):
            response = await client.post("/api/conversations", json={})
            conversations.append(response.json()["id"])

        async def turn(conversation_id: str, index: int):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"Entity concurrency probe number {index}."},
            )

        results = await asyncio.gather(
            *[turn(cid, i) for i, cid in enumerate(conversations)],
            return_exceptions=True,
        )
        assert all(getattr(r, "status_code", None) == 201 for r in results)

        entities = (await client.get("/api/entities")).json()
        postgres = [
            e for e in entities["items"] if e["normalized_name"] == "postgresql"
        ]
        assert len(postgres) == 1, f"entity duplicated: {len(postgres)} copies"


async def test_concurrent_turns_do_not_duplicate_aliases(file_backed_app) -> None:
    """The alias unique constraint must hold under the same pressure."""
    app, _, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversations = []
        for _ in range(6):
            response = await client.post("/api/conversations", json={})
            conversations.append(response.json()["id"])

        async def turn(conversation_id: str, index: int):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"Alias concurrency probe number {index}."},
            )

        await asyncio.gather(
            *[turn(cid, i) for i, cid in enumerate(conversations)],
            return_exceptions=True,
        )

        entities = (await client.get("/api/entities")).json()["items"]
        postgres = [e for e in entities if e["normalized_name"] == "postgresql"]
        assert len(postgres) == 1
        detail = (await client.get(f"/api/entities/{postgres[0]['id']}")).json()
        normalized = [a["normalized_alias"] for a in detail["aliases"]]
        assert normalized.count("postgres") <= 1


# --- Relationships under concurrency ----------------------------------------


async def test_concurrent_turns_do_not_duplicate_relationships(
    file_backed_app,
) -> None:
    """Every turn proposes the same triple; only one relationship may exist."""
    app, _, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversations = []
        for _ in range(6):
            response = await client.post("/api/conversations", json={})
            conversations.append(response.json()["id"])

        async def turn(conversation_id: str, index: int):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"Relationship concurrency probe number {index}."},
            )

        results = await asyncio.gather(
            *[turn(cid, i) for i, cid in enumerate(conversations)],
            return_exceptions=True,
        )
        assert all(getattr(r, "status_code", None) == 201 for r in results)

        relationships = (await client.get("/api/relationships")).json()
        uses = [
            r for r in relationships["items"]
            if r["relationship_type"] == "USES"
            and r["source_entity"]["canonical_name"] == "Mai"
            and r["target_entity"]["canonical_name"] == "PostgreSQL"
        ]
        assert len(uses) == 1, f"relationship duplicated: {len(uses)} copies"


async def test_concurrent_turns_accumulate_evidence_not_duplicates(
    file_backed_app,
) -> None:
    """Each turn's memory should become another evidence row on the one claim."""
    app, _, _ = file_backed_app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        conversations = []
        for _ in range(4):
            response = await client.post("/api/conversations", json={})
            conversations.append(response.json()["id"])

        async def turn(conversation_id: str, index: int):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"Evidence concurrency probe number {index}."},
            )

        await asyncio.gather(
            *[turn(cid, i) for i, cid in enumerate(conversations)],
            return_exceptions=True,
        )

        items = (await client.get("/api/relationships")).json()["items"]
        assert len(items) == 1
        # Evidence never exceeds the number of memories, and never duplicates.
        evidence = (
            await client.get(f"/api/relationships/{items[0]['id']}/evidence")
        ).json()
        memory_ids = [e["memory_id"] for e in evidence]
        assert len(memory_ids) == len(set(memory_ids))
        assert items[0]["evidence_count"] == len(memory_ids)
