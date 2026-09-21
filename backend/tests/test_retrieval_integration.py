"""Chat integration, failure isolation, and the zero-extra-call guarantee.

These assert Stage 2D's *behaviour* -- what is retrieved, what degrades, and
that retrieval costs no model call. The knowledge they look for now arrives in
the prompt through Stage 3B's reference block rather than Stage 2D's retired
inline rendering, so the marker they search for moved accordingly. Nothing
else about what is being verified changed.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.entities.models import Entity, EntityAlias, EntityStatus, EntityType, MemoryEntity
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipStatus,
    RelationshipType,
)
from app.prompt.formatter import REFERENCE_HEADER
from app.prompt.formatter import knowledge_block as reference_block
from app.services.conversation_service import ConversationService

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})


def knowledge_block(provider):
    """The reference block the model actually received, or None.

    Reads the provider's recorded messages through the same helper the rest of
    the application uses, so this checks what was *sent*, not what was meant.
    """
    return reference_block(provider.last_call)


async def seed_knowledge(session_factory):
    """A small knowledge base spanning memories, entities and relationships."""
    async with session_factory() as session:
        conversation = await ConversationService(session).create_conversation()

        def entity(name, kind, aliases=()):
            e = Entity(
                canonical_name=name, normalized_name=name.lower(),
                entity_type=kind, status=EntityStatus.ACTIVE,
            )
            session.add(e)
            return e

        mai = entity("Mai", EntityType.PROJECT)
        postgres = entity("PostgreSQL", EntityType.TECHNOLOGY)
        groq = entity("Groq", EntityType.COMPANY)
        user = entity("User", EntityType.PERSON)
        career = entity("AI Product Development", EntityType.CONCEPT)
        await session.flush()
        session.add(
            EntityAlias(
                entity_id=postgres.id, alias="Postgres", normalized_alias="postgres"
            )
        )

        def memory(content, kind, entities, age_days=0, importance=8):
            created = datetime.now(timezone.utc) - timedelta(days=age_days)
            m = Memory(
                content=content, normalized_content=content.lower().rstrip("."),
                memory_type=kind, status=MemoryStatus.ACTIVE,
                importance_score=importance, confidence_score=0.95,
                source_conversation_id=conversation.id,
                created_at=created, updated_at=created,
            )
            session.add(m)
            return m, entities

        pending = [
            memory("User is building Mai as a personal AI environment.",
                   MemoryType.SEMANTIC, [mai, user], age_days=60),
            memory("User selected PostgreSQL for local storage in Mai.",
                   MemoryType.DECISION, [mai, postgres], age_days=45),
            memory("User switched to Groq for fast inference in Mai.",
                   MemoryType.DECISION, [mai, groq], age_days=30),
            memory("User wants to transition their career toward AI product development.",
                   MemoryType.GOAL, [user, career], age_days=90, importance=9),
        ]
        await session.flush()
        for m, linked in pending:
            for e in linked:
                session.add(MemoryEntity(memory_id=m.id, entity_id=e.id))

        for src, kind, tgt in [
            (user, RelationshipType.BUILDS, mai),
            (mai, RelationshipType.USES, postgres),
            (mai, RelationshipType.USES, groq),
            (user, RelationshipType.HAS_GOAL, career),
        ]:
            session.add(
                Relationship(
                    source_entity_id=src.id, relationship_type=kind,
                    target_entity_id=tgt.id, confidence_score=0.95,
                    status=RelationshipStatus.ACTIVE,
                )
            )
        await session.commit()
        return conversation.id


# --- Zero additional model calls --------------------------------------------


async def test_retrieval_adds_no_model_call(
    client: AsyncClient, conversation_id, fake_provider, session_factory
) -> None:
    """The request path must still make exactly one chat call."""
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    # One chat generation call. Extraction calls are background, not retrieval.
    assert len(fake_provider.calls) == 1
    assert fake_provider.relationship_calls == []


async def test_retrieval_makes_no_call_at_all_when_used_directly(
    db_session, settings, fake_provider
) -> None:
    from app.retrieval.service import RetrievalService

    await RetrievalService(db_session, settings).retrieve("What does Mai use?")

    assert fake_provider.calls == []
    assert fake_provider.extraction_calls == []
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []


# --- Context reaches the model ----------------------------------------------


async def test_retrieved_knowledge_is_sent_to_the_model(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    sent = fake_provider.last_call
    knowledge = [m for m in sent if REFERENCE_HEADER in m.content]
    assert len(knowledge) == 1, "knowledge block missing from the prompt"
    block = knowledge[0].content
    assert "PostgreSQL" in block
    assert "Mai USES PostgreSQL" in block


async def test_context_order_puts_the_user_message_last(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Recent conversation must sit closest to the model's attention."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    sent = fake_provider.last_call
    roles = [m.role for m in sent]
    # instructions, runtime facts, execution state, knowledge block, then the
    # conversation. Stage 4D.1 inserted the facts block above the reference
    # block so authoritative configuration outranks retrieved knowledge;
    # Stage 5D.1 inserted the execution-state block directly after it, for the
    # same reason -- what actually ran this turn is authoritative too, and
    # nothing retrieved may be mistaken for a report of an action.
    assert roles[0] == "system"
    assert roles[1] == "system"
    assert roles[2] == "system"
    assert REFERENCE_HEADER in sent[3].content
    assert sent[-1].role == "user"
    assert sent[-1].content == "What database does Mai use?"


async def test_the_knowledge_block_defers_to_the_current_message(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What does Mai use?"},
    )

    block = knowledge_block(fake_provider)
    assert block is not None, "no knowledge block was assembled"
    assert "background knowledge" in block.lower()
    assert "the user is right" in block.lower()


async def test_no_knowledge_block_when_nothing_matches(
    client: AsyncClient, conversation_id, fake_provider, session_factory
) -> None:
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello there!"},
    )

    assert not any(REFERENCE_HEADER in m.content for m in fake_provider.last_call)


async def test_retrieval_disabled_sends_no_knowledge(
    client: AsyncClient, fake_provider, settings, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    settings.RETRIEVAL_ENABLED = False
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert not any(REFERENCE_HEADER in m.content for m in fake_provider.last_call)
    # The conversation itself still reaches the model.
    assert fake_provider.last_call[-1].content == "What database does Mai use?"


# --- Cross-conversation recall ----------------------------------------------


async def test_knowledge_from_another_conversation_is_retrieved(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """The point of the whole system: recall beyond the current conversation."""
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{fresh}/messages",
        json={"content": "What technology stack am I using for Mai?"},
    )

    block = knowledge_block(fake_provider)
    assert block is not None, "no knowledge block was assembled"
    assert "PostgreSQL" in block
    assert "Groq" in block


async def test_an_old_memory_outside_the_window_is_recalled(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """A 90-day-old goal, in a different conversation, still surfaces."""
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{fresh}/messages",
        json={"content": "What career direction have I mentioned?"},
    )

    block = knowledge_block(fake_provider)
    assert block is not None, "no knowledge block was assembled"
    assert "ai product development" in block.lower()


async def test_alias_query_recalls_the_canonical_entity(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{fresh}/messages",
        json={"content": "Should I keep using Postgres?"},
    )

    block = knowledge_block(fake_provider)
    assert block is not None, "no knowledge block was assembled"
    assert "PostgreSQL" in block


# --- Failure isolation ------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "app.retrieval.entity_matcher.EntityMatcher.match",
        "app.retrieval.retrievers.MemoryRetriever.collect",
        "app.retrieval.retrievers.RelationshipRetriever.collect",
    ],
)
async def test_one_failing_source_does_not_break_chat(
    client: AsyncClient, fake_provider, session_factory, monkeypatch, target
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.reply = "Still answering."
    fake_provider.extraction_reply = NOTHING_TO_STORE

    async def boom(*args, **kwargs):
        raise OSError(f"{target} is down")

    monkeypatch.setattr(target, boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Still answering."


async def test_total_retrieval_failure_falls_back_to_conversation(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.reply = "Answering from recent conversation."
    fake_provider.extraction_reply = NOTHING_TO_STORE

    async def boom(*args, **kwargs):
        raise RuntimeError("retrieval subsystem is down")

    monkeypatch.setattr("app.retrieval.service.RetrievalService.retrieve", boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == (
        "Answering from recent conversation."
    )
    # No knowledge block, but the conversation still reached the model.
    assert not any(REFERENCE_HEADER in m.content for m in fake_provider.last_call)
    assert fake_provider.last_call[-1].content == "What database does Mai use?"


async def test_partial_degradation_is_recorded(
    db_session, settings, monkeypatch
) -> None:
    from app.retrieval.service import RetrievalService

    async def boom(*args, **kwargs):
        raise OSError("relationships unavailable")

    monkeypatch.setattr(
        "app.retrieval.retrievers.RelationshipRetriever.collect", boom
    )

    package = await RetrievalService(db_session, settings).retrieve("Mai PostgreSQL")

    assert "relationships" in package.metadata.degraded_sources


# --- Regression: existing behaviour is unchanged -----------------------------


async def test_conversation_context_still_works(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "My name is RetrievalUser."},
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What is my name?"},
    )

    sent = fake_provider.last_call
    contents = [m.content for m in sent]
    assert "My name is RetrievalUser." in contents
    assert contents[-1] == "What is my name?"


# --- Documented recall limitation -------------------------------------------


async def test_a_query_with_no_lexical_overlap_retrieves_nothing(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """The honest boundary of non-semantic retrieval.

    Retrieval matches words and entity names. A query that shares neither with
    a stored memory finds nothing, however related the two are in meaning.
    "Long-term ambitions" and "career toward AI product development" are the
    same subject to a human and unrelated to a keyword index. Closing this gap
    needs embeddings, which are explicitly out of scope.
    """
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{fresh}/messages",
        json={"content": "What are my professional aspirations?"},
    )

    assert knowledge_block(fake_provider) is None
