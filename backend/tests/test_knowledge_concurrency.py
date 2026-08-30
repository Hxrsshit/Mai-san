"""Stage 3C: concurrency and failure isolation.

Conflict evaluation is the fourth writer in the background pipeline. These
tests use a file-backed SQLite database on purpose -- the in-memory fixture
shares one connection through StaticPool and cannot reproduce contention.
"""

import asyncio
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.database.metadata import Base
from app.database.session import dispose_engine, init_engine
from app.knowledge.models import ConflictResolution, KnowledgeConflict
from app.llm.factory import get_llm_provider
from app.main import create_app
from app.memory.models import Memory, MemoryStatus
from app.relationships.models import Relationship, RelationshipStatus

from tests.conftest import FakeLLMProvider

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})

MIGRATION_MEMORY = json.dumps(
    {
        "should_store_memory": True,
        "memories": [
            {
                "content": "User migrated Mai from OpenRouter to Groq.",
                "memory_type": "decision",
                "importance_score": 9,
                "confidence_score": 0.95,
            }
        ],
    }
)
ENTITIES = json.dumps(
    {
        "entities": [
            {"name": "Mai", "entity_type": "project", "confidence_score": 0.95},
            {"name": "Groq", "entity_type": "company", "confidence_score": 0.95},
            {"name": "OpenRouter", "entity_type": "company", "confidence_score": 0.95},
        ]
    }
)
RELATIONSHIPS = json.dumps(
    {
        "relationships": [
            {
                "source_entity": "Mai",
                "relationship_type": "USES",
                "target_entity": "Groq",
                "confidence_score": 0.93,
            }
        ]
    }
)


@pytest_asyncio.fixture
async def file_backed(tmp_path):
    """A real engine against a temporary database file."""
    settings = Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'conflicts.db'}",
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


async def counts(engine):
    async with engine.connect() as connection:
        links = (
            await connection.execute(
                select(func.count()).select_from(KnowledgeConflict)
            )
        ).scalar_one()
        superseded = (
            await connection.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.status == MemoryStatus.SUPERSEDED)
            )
        ).scalar_one()
        active = (
            await connection.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.status == MemoryStatus.ACTIVE)
            )
        ).scalar_one()
    return links, superseded, active


# --- Concurrency ------------------------------------------------------------


async def test_concurrent_turns_do_not_duplicate_lifecycle_links(
    file_backed,
) -> None:
    """Several turns each propose the same supersession; one link may exist."""
    app, provider, engine = file_backed

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Establish the knowledge that will be retired.
        provider.extraction_reply = json.dumps(
            {
                "should_store_memory": True,
                "memories": [
                    {
                        "content": "Mai uses OpenRouter for inference.",
                        "memory_type": "semantic",
                        "importance_score": 9,
                        "confidence_score": 0.95,
                    }
                ],
            }
        )
        provider.entity_reply = ENTITIES
        provider.relationship_reply = json.dumps(
            {
                "relationships": [
                    {
                        "source_entity": "Mai",
                        "relationship_type": "USES",
                        "target_entity": "OpenRouter",
                        "confidence_score": 0.93,
                    }
                ]
            }
        )
        first = (await client.post("/api/conversations", json={})).json()["id"]
        await client.post(
            f"/api/conversations/{first}/messages",
            json={"content": "Mai uses OpenRouter for inference."},
        )

        # Now several concurrent turns all reporting the same migration.
        provider.extraction_reply = MIGRATION_MEMORY
        provider.relationship_reply = RELATIONSHIPS

        conversations = []
        for _ in range(6):
            conversations.append(
                (await client.post("/api/conversations", json={})).json()["id"]
            )

        async def turn(conversation_id, index):
            return await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={"content": f"I migrated Mai from OpenRouter to Groq ({index})."},
            )

        results = await asyncio.gather(
            *[turn(cid, i) for i, cid in enumerate(conversations)],
            return_exceptions=True,
        )
        assert all(getattr(r, "status_code", None) == 201 for r in results), results

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(
                    KnowledgeConflict.older_memory_id,
                    KnowledgeConflict.newer_memory_id,
                )
            )
        ).all()

    # No duplicate ordered pair survived. The UNIQUE index is what guarantees
    # this: concurrent evaluations cannot see each other's uncommitted rows.
    assert len(rows) == len(set(rows))


async def test_concurrent_evaluation_leaves_a_deterministic_final_state(
    file_backed,
) -> None:
    """Whatever the interleaving, the OpenRouter memory ends up historical."""
    app, provider, engine = file_backed

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        provider.extraction_reply = json.dumps(
            {
                "should_store_memory": True,
                "memories": [
                    {
                        "content": "Mai uses OpenRouter for inference.",
                        "memory_type": "semantic",
                        "importance_score": 9,
                        "confidence_score": 0.95,
                    }
                ],
            }
        )
        provider.entity_reply = ENTITIES
        provider.relationship_reply = json.dumps({"relationships": []})
        first = (await client.post("/api/conversations", json={})).json()["id"]
        await client.post(
            f"/api/conversations/{first}/messages",
            json={"content": "Mai uses OpenRouter."},
        )

        provider.extraction_reply = MIGRATION_MEMORY
        conversations = [
            (await client.post("/api/conversations", json={})).json()["id"]
            for _ in range(4)
        ]

        await asyncio.gather(
            *[
                client.post(
                    f"/api/conversations/{cid}/messages",
                    json={"content": f"I migrated Mai from OpenRouter to Groq ({i})."},
                )
                for i, cid in enumerate(conversations)
            ],
            return_exceptions=True,
        )

    async with engine.connect() as connection:
        rows = (
            await connection.execute(select(Memory.content, Memory.status))
        ).all()
    by_content = {content: status for content, status in rows}

    assert by_content["Mai uses OpenRouter for inference."] == "superseded"
    assert by_content["User migrated Mai from OpenRouter to Groq."] == "active"


async def test_no_orphaned_or_self_referential_links_are_created(
    file_backed,
) -> None:
    app, provider, engine = file_backed

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        provider.entity_reply = ENTITIES
        provider.relationship_reply = json.dumps({"relationships": []})
        for index, content in enumerate(
            ["Mai uses OpenRouter.", "User migrated Mai from OpenRouter to Groq."]
        ):
            provider.extraction_reply = json.dumps(
                {
                    "should_store_memory": True,
                    "memories": [
                        {
                            "content": content,
                            "memory_type": "decision",
                            "importance_score": 9,
                            "confidence_score": 0.95,
                        }
                    ],
                }
            )
            conversation = (
                await client.post("/api/conversations", json={})
            ).json()["id"]
            await client.post(
                f"/api/conversations/{conversation}/messages",
                json={"content": f"turn {index}: {content}"},
            )

    async with engine.connect() as connection:
        links = (await connection.execute(select(KnowledgeConflict))).all()
        memory_ids = set(
            (await connection.execute(select(Memory.id))).scalars().all()
        )
        relationship_ids = set(
            (await connection.execute(select(Relationship.id))).scalars().all()
        )

    assert links, "the migration produced no lifecycle link at all"
    for link in links:
        # No self-supersession.
        if link.older_memory_id is not None:
            assert link.older_memory_id != link.newer_memory_id
            assert link.older_memory_id in memory_ids
            if link.newer_memory_id is not None:
                assert link.newer_memory_id in memory_ids
        else:
            assert link.older_relationship_id != link.newer_relationship_id
            assert link.older_relationship_id in relationship_ids
            if link.newer_relationship_id is not None:
                assert link.newer_relationship_id in relationship_ids
        # Exactly one kind per row.
        assert (link.older_memory_id is None) != (
            link.older_relationship_id is None
        )


# --- Failure isolation ------------------------------------------------------


async def test_conflict_evaluation_failure_does_not_break_chat(
    client: AsyncClient, fake_provider, monkeypatch, caplog
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    async def boom(*args, **kwargs):
        raise RuntimeError("conflict evaluation is down")

    monkeypatch.setattr(
        "app.knowledge.service.KnowledgeService.evaluate_memory", boom
    )

    fake_provider.reply = "Answering regardless."
    fake_provider.extraction_reply = MIGRATION_MEMORY
    fake_provider.entity_reply = ENTITIES
    fake_provider.relationship_reply = RELATIONSHIPS

    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    response = await client.post(
        f"/api/conversations/{conversation}/messages",
        json={"content": "I migrated Mai from OpenRouter to Groq."},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Answering regardless."

    # The earlier stages still committed their work.
    assert (await client.get("/api/memories")).json()["total"] == 1
    assert (await client.get("/api/entities")).json()["total"] == 3

    # And the failure is visible rather than swallowed.
    assert any(
        "Conflict evaluation task failed" in record.getMessage()
        for record in caplog.records
    )


async def test_a_failed_evaluation_leaves_knowledge_active_not_corrupted(
    client: AsyncClient, fake_provider, monkeypatch
) -> None:
    """Prefer ACTIVE + unresolved over a partially mutated state."""

    async def boom(*args, **kwargs):
        raise RuntimeError("detector is down")

    monkeypatch.setattr(
        "app.knowledge.conflicts.ConflictDetector.detect", boom
    )

    fake_provider.entity_reply = ENTITIES
    fake_provider.relationship_reply = json.dumps({"relationships": []})
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "Mai uses OpenRouter.",
                    "memory_type": "semantic",
                    "importance_score": 9,
                    "confidence_score": 0.95,
                }
            ],
        }
    )
    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages", json={"content": "Mai uses OpenRouter."}
    )

    fake_provider.extraction_reply = MIGRATION_MEMORY
    second = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{second}/messages",
        json={"content": "I migrated Mai from OpenRouter to Groq."},
    )

    memories = (await client.get("/api/memories")).json()["items"]
    assert len(memories) == 2
    assert all(memory["status"] == "active" for memory in memories), (
        "a failed evaluation left knowledge half-retired"
    )


async def test_a_partial_failure_does_not_stop_the_other_decisions(
    db_session, settings, conversation_factory=None
) -> None:
    """One undecidable outcome must not take the whole pass down."""
    from app.knowledge.lifecycle import LifecycleWriter
    from app.knowledge.models import ConflictReason
    from app.knowledge.schemas import ConflictOutcome
    from app.services.conversation_service import ConversationService
    from tests.test_knowledge_lifecycle import make_memory

    conversation = (
        await ConversationService(db_session).create_conversation()
    ).id
    a = await make_memory(db_session, conversation, "Memory A.", day=0)
    b = await make_memory(db_session, conversation, "Memory B.", day=1)
    c = await make_memory(db_session, conversation, "Memory C.", day=2)

    writer = LifecycleWriter(db_session)
    report = await writer.apply(
        [
            # Self-supersession: refused.
            ConflictOutcome(
                resolution=ConflictResolution.SUPERSEDED,
                reason=ConflictReason.EXPLICIT_REPLACEMENT,
                older_memory_id=a.id, newer_memory_id=a.id,
            ),
            # Valid: must still be applied.
            ConflictOutcome(
                resolution=ConflictResolution.SUPERSEDED,
                reason=ConflictReason.EXPLICIT_REPLACEMENT,
                older_memory_id=b.id, newer_memory_id=c.id,
            ),
        ]
    )

    assert report.cycles_prevented == 1
    assert report.links_created == 1
    assert (await db_session.get(Memory, a.id)).status is MemoryStatus.ACTIVE
    assert (await db_session.get(Memory, b.id)).status is MemoryStatus.SUPERSEDED


async def test_disabling_conflict_detection_stops_new_decisions(
    client: AsyncClient, fake_provider, settings
) -> None:
    settings.CONFLICT_DETECTION_ENABLED = False

    fake_provider.entity_reply = ENTITIES
    fake_provider.relationship_reply = json.dumps({"relationships": []})
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "Mai uses OpenRouter.",
                    "memory_type": "semantic",
                    "importance_score": 9,
                    "confidence_score": 0.95,
                }
            ],
        }
    )
    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages", json={"content": "Mai uses OpenRouter."}
    )

    fake_provider.extraction_reply = MIGRATION_MEMORY
    second = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{second}/messages",
        json={"content": "I migrated Mai from OpenRouter to Groq."},
    )

    memories = (await client.get("/api/memories")).json()["items"]
    assert all(memory["status"] == "active" for memory in memories)


# --- Background pipeline ordering -------------------------------------------


async def test_conflict_evaluation_runs_after_the_other_three_stages(
    client: AsyncClient, fake_provider, caplog
) -> None:
    """Detection resolves entity names, so it must run last."""
    import logging

    caplog.set_level(logging.INFO)

    fake_provider.entity_reply = ENTITIES
    fake_provider.relationship_reply = json.dumps({"relationships": []})
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "Mai uses OpenRouter.",
                    "memory_type": "semantic",
                    "importance_score": 9,
                    "confidence_score": 0.95,
                }
            ],
        }
    )
    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages", json={"content": "Mai uses OpenRouter."}
    )

    caplog.clear()
    fake_provider.extraction_reply = MIGRATION_MEMORY
    second = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{second}/messages",
        json={"content": "I migrated Mai from OpenRouter to Groq."},
    )

    messages = [record.getMessage() for record in caplog.records]
    assert "Knowledge conflicts evaluated" in messages
    # Extraction logged its work before the lifecycle decision was taken.
    assert messages.index("Memory stored") < messages.index(
        "Knowledge conflicts evaluated"
    )


async def test_lifecycle_logging_carries_no_memory_text(
    client: AsyncClient, fake_provider, caplog
) -> None:
    import logging

    caplog.set_level(logging.INFO)

    fake_provider.entity_reply = ENTITIES
    fake_provider.relationship_reply = json.dumps({"relationships": []})
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "Mai uses OpenRouter.",
                    "memory_type": "semantic",
                    "importance_score": 9,
                    "confidence_score": 0.95,
                }
            ],
        }
    )
    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages", json={"content": "Mai uses OpenRouter."}
    )

    fake_provider.extraction_reply = MIGRATION_MEMORY
    second = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{second}/messages",
        json={"content": "I migrated Mai from OpenRouter to Groq."},
    )

    lifecycle_records = [
        record for record in caplog.records
        if record.getMessage() == "Knowledge conflicts evaluated"
    ]
    assert lifecycle_records
    for record in lifecycle_records:
        serialised = str(record.__dict__)
        assert "Mai uses OpenRouter" not in serialised
        assert "migrated" not in serialised
        # Counts and ids only.
        assert record.conflicts_detected >= 1
