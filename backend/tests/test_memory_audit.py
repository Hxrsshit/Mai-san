"""Regressions found during the Stage 2A acceptance audit.

Each test here corresponds to something the original Stage 2A suite did not
cover, or to a defect the audit uncovered.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.memory.service import MemoryService
from app.services.conversation_service import ConversationService


def payload(*memories) -> str:
    return json.dumps(
        {"should_store_memory": bool(memories), "memories": list(memories)}
    )


def candidate(content, memory_type="preference", importance=8, confidence=0.9) -> dict:
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
    return MemoryService(session=db_session, provider=fake_provider, settings=settings)


# --- BUG: exact duplicates escaped the bounded deduplication window ---------
# Fuzzy matching compares text pairwise, so it is necessarily limited to a
# recent window. Exact matching is an indexed equality lookup and must not be:
# an identical memory from months ago is still a duplicate.


async def test_exact_duplicate_is_caught_beyond_the_dedup_window(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.MEMORY_DEDUP_CANDIDATES = 5
    memory_service = service(db_session, fake_provider, settings)
    target = "User strongly prefers detailed written documentation."

    fake_provider.extraction_reply = payload(candidate(target))
    assert len(await memory_service.extract_and_store(conversation.id, "a", "b")) == 1

    # Push it well outside the window with unrelated same-type memories.
    for index in range(7):
        fake_provider.extraction_reply = payload(
            candidate(f"User owns gadget number {index} of some kind.")
        )
        await memory_service.extract_and_store(conversation.id, "a", "b")

    fake_provider.extraction_reply = payload(candidate(target))
    stored = await memory_service.extract_and_store(conversation.id, "a", "b")

    assert stored == []
    copies = [
        m for m in await memory_service.list_memories(limit=100) if m.content == target
    ]
    assert len(copies) == 1


async def test_exact_duplicate_lookup_is_scoped_to_the_same_type(
    db_session, fake_provider, settings, conversation
) -> None:
    """Identical wording under a different type is a different fact."""
    memory_service = service(db_session, fake_provider, settings)
    text = "User values shipping software quickly."

    fake_provider.extraction_reply = payload(candidate(text, "preference"))
    await memory_service.extract_and_store(conversation.id, "a", "b")
    fake_provider.extraction_reply = payload(candidate(text, "goal"))
    await memory_service.extract_and_store(conversation.id, "a", "b")

    assert await memory_service.count_memories() == 2


async def test_normalized_content_is_persisted_for_lookup(
    db_session, fake_provider, settings, conversation
) -> None:
    """The indexed column must actually be populated, not left dead."""
    fake_provider.extraction_reply = payload(
        candidate("User Prefers   Practical, Explanations!")
    )
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "a", "b"
    )

    assert stored[0].normalized_content == "user prefers practical explanations"


# --- Intra-batch deduplication ----------------------------------------------
# One turn can yield several candidates; they must be compared against each
# other, not only against what was already stored.


async def test_identical_candidates_in_one_turn_store_once(
    db_session, fake_provider, settings, conversation
) -> None:
    text = "User prefers practical explanations."
    fake_provider.extraction_reply = payload(candidate(text), candidate(text))

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "a", "b"
    )
    assert len(stored) == 1


async def test_reworded_candidates_in_one_turn_store_once(
    db_session, fake_provider, settings, conversation
) -> None:
    fake_provider.extraction_reply = payload(
        candidate("User prefers practical explanations."),
        candidate("The user likes practical explanations."),
    )

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "a", "b"
    )
    assert len(stored) == 1


async def test_distinct_candidates_in_one_turn_are_both_stored(
    db_session, fake_provider, settings, conversation
) -> None:
    fake_provider.extraction_reply = payload(
        candidate("User prefers tea in the afternoon."),
        candidate("User works in financial technology.", "semantic"),
    )

    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "a", "b"
    )
    assert len(stored) == 2


# --- Repeated turns must not accumulate duplicates ---------------------------


async def test_resending_the_same_message_creates_one_memory(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """A retried or repeated message must not duplicate the memory."""
    fake_provider.extraction_reply = payload(
        candidate("User prefers concise written summaries.")
    )

    for _ in range(3):
        response = await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "I prefer concise written summaries."},
        )
        assert response.status_code == 201

    assert (await client.get("/api/memories")).json()["total"] == 1


# --- Database failure during memory storage ---------------------------------


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError(61, "Connection refused"), OSError("disk failure")],
)
async def test_database_failure_during_storage_persists_nothing(
    session_factory, fake_provider, settings, monkeypatch, error
) -> None:
    """Exercised through the real task path, which owns the rollback.

    Extraction must fail closed: no partial or corrupt memory record survives,
    and the failure never escapes the task.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.memory.tasks import run_memory_extraction

    async with session_factory() as setup_session:
        conversation = await ConversationService(setup_session).create_conversation()
        await setup_session.commit()
        conversation_id = conversation.id

    fake_provider.extraction_reply = payload(
        candidate("User prefers concise explanations.")
    )

    async def failing_flush(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(AsyncSession, "flush", failing_flush)

    # Must not raise, regardless of what the database does.
    await run_memory_extraction(
        conversation_id=conversation_id,
        user_message="I prefer concise explanations.",
        assistant_message="Noted.",
        settings=settings,
        provider=fake_provider,
        session_factory=session_factory,
    )

    monkeypatch.undo()

    async with session_factory() as verify_session:
        remaining = await verify_session.execute(
            select(func.count()).select_from(Memory)
        )
        assert remaining.scalar_one() == 0


async def test_a_failed_candidate_leaves_no_half_written_batch(
    session_factory, fake_provider, settings, monkeypatch
) -> None:
    """The second candidate fails; the first must not survive."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.memory.tasks import run_memory_extraction

    async with session_factory() as setup_session:
        conversation = await ConversationService(setup_session).create_conversation()
        await setup_session.commit()
        conversation_id = conversation.id

    fake_provider.extraction_reply = payload(
        candidate("User prefers tea in the afternoon."),
        candidate("User works in financial technology.", "semantic"),
    )

    real_flush = AsyncSession.flush
    calls = {"n": 0}

    async def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("database went away mid-batch")
        return await real_flush(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flaky_flush)

    await run_memory_extraction(
        conversation_id=conversation_id,
        user_message="Two things about me.",
        assistant_message="Noted.",
        settings=settings,
        provider=fake_provider,
        session_factory=session_factory,
    )

    monkeypatch.undo()

    async with session_factory() as verify_session:
        remaining = await verify_session.execute(
            select(func.count()).select_from(Memory)
        )
        assert remaining.scalar_one() == 0


# --- MEMORY_ENABLED master switch -------------------------------------------


def test_memory_routes_are_not_registered_when_disabled() -> None:
    """MEMORY_ENABLED=false must remove the inspection surface entirely."""
    disabled = Settings(_env_file=None, GROQ_API_KEY="k", MEMORY_ENABLED=False)

    # create_app reads settings when the app is built, not per request.
    import app.main as main_module

    original = main_module.get_settings
    main_module.get_settings = lambda: disabled
    try:
        built = main_module.create_app()
    finally:
        main_module.get_settings = original

    paths = {getattr(r, "path", "") for r in built.routes}
    assert not any(p.startswith("/api/memories") for p in paths)
    # Chat surface is untouched.
    assert "/api/conversations" in paths
    assert "/health" in paths


async def test_extraction_task_is_a_noop_when_memory_disabled(
    db_session, fake_provider, settings, conversation
) -> None:
    from app.memory.tasks import run_memory_extraction

    settings.MEMORY_ENABLED = False
    fake_provider.extraction_reply = payload(candidate("User prefers X always."))

    await run_memory_extraction(
        conversation_id=conversation.id,
        user_message="I prefer X.",
        assistant_message="Noted.",
        settings=settings,
        provider=fake_provider,
        session_factory=None,
    )

    assert fake_provider.extraction_calls == []


# --- Status defaults ---------------------------------------------------------


async def test_new_memories_default_to_active(
    db_session, fake_provider, settings, conversation
) -> None:
    fake_provider.extraction_reply = payload(candidate("User prefers active status."))
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "a", "b"
    )
    assert stored[0].status is MemoryStatus.ACTIVE


@pytest.mark.parametrize("value", ["random_type", "entity", "relationship", ""])
async def test_invalid_memory_types_cannot_be_stored(
    db_session, fake_provider, settings, conversation, value
) -> None:
    fake_provider.extraction_reply = payload(
        {
            "content": "User prefers something specific.",
            "memory_type": value,
            "importance_score": 8,
            "confidence_score": 0.9,
        }
    )
    stored = await service(db_session, fake_provider, settings).extract_and_store(
        conversation.id, "a", "b"
    )
    assert stored == []


def test_memory_type_enum_is_exactly_the_five_stage_2a_types() -> None:
    assert {t.value for t in MemoryType} == {
        "semantic",
        "preference",
        "goal",
        "decision",
        "episodic",
    }


def test_memory_status_enum_is_exactly_the_three_stage_2a_statuses() -> None:
    assert {s.value for s in MemoryStatus} == {"active", "superseded", "archived"}


# --- Traceability ------------------------------------------------------------


async def test_memory_traces_back_to_its_conversation_and_message(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """The data needed to answer "where did you learn this?" must resolve."""
    fake_provider.extraction_reply = payload(
        candidate("User prefers traceable provenance.")
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer traceable provenance."},
    )

    memory = (await client.get("/api/memories")).json()["items"][0]

    # conversation_id resolves to a real conversation
    conversation = await client.get(
        f"/api/conversations/{memory['source_conversation_id']}"
    )
    assert conversation.status_code == 200

    # source_message_id resolves to a real message in that conversation
    message_ids = {m["id"] for m in conversation.json()["messages"]}
    assert memory["source_message_id"] in message_ids

    # and that message is the user's, not the assistant's
    source = next(
        m for m in conversation.json()["messages"] if m["id"] == memory["source_message_id"]
    )
    assert source["role"] == "user"


async def test_foreign_key_integrity_is_enforced(
    db_session, fake_provider, settings
) -> None:
    """A memory cannot reference a conversation that does not exist."""
    memory = Memory(
        content="User prefers dangling references.",
        normalized_content="user prefers dangling references",
        memory_type=MemoryType.PREFERENCE,
        importance_score=7,
        confidence_score=0.9,
        source_conversation_id=uuid.uuid4(),  # nonexistent
    )
    db_session.add(memory)

    with pytest.raises(Exception):
        await db_session.flush()
    await db_session.rollback()
