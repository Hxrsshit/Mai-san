"""The storage pipeline: thresholds, deduplication, provenance, persistence."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.memory.models import Memory, MemoryStatus, MemoryType
from app.memory.service import MemoryService
from app.services.conversation_service import ConversationService


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


@pytest.fixture
async def conversation(db_session):
    return await ConversationService(db_session).create_conversation()


def service(db_session, fake_provider, settings) -> MemoryService:
    return MemoryService(
        session=db_session, provider=fake_provider, settings=settings
    )


# --- Storage ----------------------------------------------------------------


async def test_valid_candidate_is_stored(
    db_session, fake_provider, settings, conversation
) -> None:
    fake_provider.extraction_reply = payload(
        candidate("User prefers concise explanations.", "preference", 8, 0.93)
    )

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation_id=conversation.id,
        user_message="I prefer concise explanations.",
        assistant_message="Noted.",
    )

    assert len(stored) == 1
    memory = stored[0]
    assert memory.content == "User prefers concise explanations."
    assert memory.memory_type is MemoryType.PREFERENCE
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.importance_score == 8
    assert memory.confidence_score == pytest.approx(0.93)
    assert memory.source_conversation_id == conversation.id
    assert memory.id is not None


async def test_memory_persists_and_is_readable(
    db_session, fake_provider, settings, conversation
) -> None:
    fake_provider.extraction_reply = payload(candidate("User prefers tea over coffee."))
    await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "I prefer tea over coffee.", "Noted."
    )
    await db_session.commit()

    rows = (await db_session.execute(select(Memory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].content == "User prefers tea over coffee."


# --- Thresholds -------------------------------------------------------------


@pytest.mark.parametrize("importance", [1, 2, 3, 4])
async def test_low_importance_is_discarded(
    db_session, fake_provider, settings, conversation, importance
) -> None:
    fake_provider.extraction_reply = payload(
        candidate("User mentioned something minor today.", "semantic", importance, 0.95)
    )
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "Minor thing.", "Noted."
    )
    assert stored == []


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.5, 0.69])
async def test_low_confidence_is_discarded(
    db_session, fake_provider, settings, conversation, confidence
) -> None:
    fake_provider.extraction_reply = payload(
        candidate("User might possibly like something.", "semantic", 9, confidence)
    )
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "Maybe.", "Noted."
    )
    assert stored == []


async def test_thresholds_are_configurable(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.MEMORY_MIN_IMPORTANCE = 3
    settings.MEMORY_MIN_CONFIDENCE = 0.5
    fake_provider.extraction_reply = payload(
        candidate("User mentioned a small preference.", "preference", 4, 0.6)
    )

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "Small thing.", "Noted."
    )
    assert len(stored) == 1


async def test_candidate_exactly_at_threshold_is_kept(
    db_session, fake_provider, settings, conversation
) -> None:
    """>= not >."""
    fake_provider.extraction_reply = payload(
        candidate(
            "User prefers boundary conditions tested.",
            "preference",
            settings.MEMORY_MIN_IMPORTANCE,
            settings.MEMORY_MIN_CONFIDENCE,
        )
    )
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "Boundary.", "Noted."
    )
    assert len(stored) == 1


# --- Deduplication in the pipeline ------------------------------------------


async def test_reworded_duplicate_is_not_stored_twice(
    db_session, fake_provider, settings, conversation
) -> None:
    memory_service = service(db_session, fake_provider, settings)

    fake_provider.extraction_reply = payload(
        candidate("User prefers practical explanations.")
    )
    first = await memory_service.extract_and_store(
        conversation.id, "I prefer practical explanations.", "Noted."
    )
    assert len(first) == 1

    fake_provider.extraction_reply = payload(
        candidate("The user likes practical explanations.")
    )
    second = await memory_service.extract_and_store(
        conversation.id, "I like practical explanations.", "Noted."
    )

    assert second == []
    assert await memory_service.count_memories() == 1


async def test_existing_memory_is_left_untouched_by_a_duplicate(
    db_session, fake_provider, settings, conversation
) -> None:
    """Stage 2A must not overwrite: no supersession logic yet."""
    memory_service = service(db_session, fake_provider, settings)
    fake_provider.extraction_reply = payload(
        candidate("User prefers practical explanations.", "preference", 6, 0.8)
    )
    original = (
        await memory_service.extract_and_store(conversation.id, "a", "b")
    )[0]
    original_id, original_score = original.id, original.importance_score

    fake_provider.extraction_reply = payload(
        candidate("The user likes practical explanations.", "preference", 10, 0.99)
    )
    await memory_service.extract_and_store(conversation.id, "c", "d")

    kept = await memory_service.get_memory(original_id)
    assert kept.importance_score == original_score
    assert kept.content == "User prefers practical explanations."


async def test_distinct_memories_both_stored(
    db_session, fake_provider, settings, conversation
) -> None:
    memory_service = service(db_session, fake_provider, settings)
    fake_provider.extraction_reply = payload(
        candidate("User prefers practical explanations.")
    )
    await memory_service.extract_and_store(conversation.id, "a", "b")

    fake_provider.extraction_reply = payload(
        candidate("User prefers working late at night.")
    )
    await memory_service.extract_and_store(conversation.id, "c", "d")

    assert await memory_service.count_memories() == 2


async def test_same_wording_different_type_is_not_a_duplicate(
    db_session, fake_provider, settings, conversation
) -> None:
    """A goal and a preference are different kinds of fact."""
    memory_service = service(db_session, fake_provider, settings)
    fake_provider.extraction_reply = payload(
        candidate("User values shipping software quickly.", "preference")
    )
    await memory_service.extract_and_store(conversation.id, "a", "b")

    fake_provider.extraction_reply = payload(
        candidate("User values shipping software quickly.", "goal")
    )
    await memory_service.extract_and_store(conversation.id, "c", "d")

    assert await memory_service.count_memories() == 2


# --- Provenance -------------------------------------------------------------


async def test_source_message_is_linked(
    db_session, fake_provider, settings, conversation
) -> None:
    from app.database.models import MessageRole

    conversations = ConversationService(db_session)
    message = await conversations.add_message(
        conversation.id, MessageRole.USER, "I prefer concise explanations."
    )

    fake_provider.extraction_reply = payload(candidate("User prefers concise answers."))
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation_id=conversation.id,
        user_message="I prefer concise explanations.",
        assistant_message="Noted.",
        source_message_id=message.id,
    )

    assert stored[0].source_message_id == message.id
    assert stored[0].source_conversation_id == conversation.id


async def test_hallucinated_source_message_id_falls_back(
    db_session, fake_provider, settings, conversation
) -> None:
    """A model-invented id must not break the foreign key."""
    entry = candidate("User prefers concise answers.")
    entry["source_message_id"] = str(uuid.uuid4())  # does not exist
    fake_provider.extraction_reply = payload(entry)

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation_id=conversation.id,
        user_message="I prefer concise answers.",
        assistant_message="Noted.",
        source_message_id=None,
    )
    await db_session.commit()

    assert len(stored) == 1
    assert stored[0].source_message_id is None


@pytest.mark.parametrize("placeholder", ["", "null", "none", "..."])
async def test_placeholder_source_ids_are_treated_as_absent(
    db_session, fake_provider, settings, conversation, placeholder
) -> None:
    entry = candidate("User prefers concise answers.")
    entry["source_message_id"] = placeholder
    fake_provider.extraction_reply = payload(entry)

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "I prefer concise answers.", "Noted."
    )
    assert len(stored) == 1


async def test_deleting_a_conversation_removes_its_memories(
    db_session, fake_provider, settings, conversation
) -> None:
    memory_service = service(db_session, fake_provider, settings)
    fake_provider.extraction_reply = payload(candidate("User prefers concise answers."))
    await memory_service.extract_and_store(conversation.id, "a", "b")
    await db_session.commit()
    assert await memory_service.count_memories() == 1

    await ConversationService(db_session).delete_conversation(conversation.id)
    await db_session.commit()

    assert await memory_service.count_memories() == 0


# --- Switches ---------------------------------------------------------------


async def test_extraction_can_be_disabled(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.MEMORY_EXTRACTION_ENABLED = False
    fake_provider.extraction_reply = payload(candidate("User prefers concise answers."))

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "I prefer concise answers.", "Noted."
    )

    assert stored == []
    assert fake_provider.extraction_calls == []
