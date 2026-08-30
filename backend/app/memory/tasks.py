"""Post-turn analysis, run as a FastAPI background task.

Memory extraction runs first, then entity extraction over whatever memories
were stored, then relationship extraction over those same memories. All three
live in this one task deliberately: a second concurrent background writer
would race the first on the same tables.

Starlette runs background tasks after the response has been sent, so the user
never waits on extraction and never sees an error from it. No external queue
is involved: Stage 2A favours something simple and reliable.

The task opens its **own** database session. The request-scoped session is
already closed by the time this runs, so reusing it would fail.
"""

import uuid
from typing import List, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.session import get_session_factory
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.entities.service import EntityService
from app.memory.models import Memory
from app.relationships.service import RelationshipService
from app.memory.service import MemoryService

logger = get_logger(__name__)


async def run_memory_extraction(
    conversation_id: uuid.UUID,
    user_message: str,
    assistant_message: str,
    source_message_id: Optional[uuid.UUID] = None,
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    session_factory=None,
) -> None:
    """Extract and store memories for one completed turn.

    `provider` and `session_factory` are passed in by the route from its own
    dependencies rather than resolved here. Resolving them internally would
    bypass FastAPI dependency overrides, which in tests meant reaching the
    real model API and the real database. They fall back to the process-wide
    instances when omitted.

    This function never raises. A failure here must never affect the chat
    turn that produced it -- that turn has already been answered and
    committed.
    """
    settings = settings or get_settings()
    if not (settings.MEMORY_ENABLED and settings.MEMORY_EXTRACTION_ENABLED):
        return

    try:
        if session_factory is None:
            session_factory = get_session_factory()
        if provider is None:
            provider = get_llm_provider()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Memory extraction could not start",
            extra={"conversation_id": str(conversation_id), "error": str(exc)},
        )
        return

    stored_memory_ids: List[uuid.UUID] = []
    try:
        async with session_factory() as session:
            service = MemoryService(
                session=session, provider=provider, settings=settings
            )
            stored = await service.extract_and_store(
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
                source_message_id=source_message_id,
            )
            if stored:
                await session.commit()
                # Captured before the session closes; entity extraction runs
                # on a fresh session against the now-committed rows.
                stored_memory_ids = [memory.id for memory in stored]
            else:
                # Nothing to persist; drop any read transaction cleanly.
                await session.rollback()
    except Exception as exc:  # noqa: BLE001 - the whole point of this wrapper
        logger.error(
            "Memory extraction task failed",
            extra={"conversation_id": str(conversation_id), "error": str(exc)},
            exc_info=exc,
        )
        return

    if stored_memory_ids:
        await run_entity_extraction(
            memory_ids=stored_memory_ids,
            settings=settings,
            provider=provider,
            session_factory=session_factory,
        )


async def run_entity_extraction(
    memory_ids: List[uuid.UUID],
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    session_factory=None,
) -> None:
    """Extract entities for memories that are already committed.

    Runs on its own session after the memories are durable, so a failure here
    can never roll back a memory -- let alone the chat turn.

    Never raises.
    """
    settings = settings or get_settings()
    if not (settings.MEMORY_ENABLED and settings.ENTITY_EXTRACTION_ENABLED):
        return

    try:
        if session_factory is None:
            session_factory = get_session_factory()
        if provider is None:
            provider = get_llm_provider()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Entity extraction could not start", extra={"error": str(exc)}
        )
        return

    for memory_id in memory_ids:
        try:
            async with session_factory() as session:
                memory = await session.get(Memory, memory_id)
                if memory is None:
                    # Its conversation was deleted between the two steps.
                    continue

                service = EntityService(
                    session=session, provider=provider, settings=settings
                )
                linked = await service.extract_for_memory(memory)
                if linked:
                    await session.commit()
                else:
                    await session.rollback()
        except Exception as exc:  # noqa: BLE001 - contain per memory
            logger.error(
                "Entity extraction task failed",
                extra={"memory_id": str(memory_id), "error": str(exc)},
                exc_info=exc,
            )

    # Relationships are extracted only after entities are committed, so both
    # ends of every relationship already exist.
    await run_relationship_extraction(
        memory_ids=memory_ids,
        settings=settings,
        provider=provider,
        session_factory=session_factory,
    )


async def run_relationship_extraction(
    memory_ids: List[uuid.UUID],
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    session_factory=None,
) -> None:
    """Extract relationships for memories whose entities are committed.

    Runs on its own session after entity extraction, so a failure here can
    never roll back an entity, a memory, or the chat turn.

    Never raises.
    """
    settings = settings or get_settings()
    if not (settings.MEMORY_ENABLED and settings.RELATIONSHIP_EXTRACTION_ENABLED):
        return

    try:
        if session_factory is None:
            session_factory = get_session_factory()
        if provider is None:
            provider = get_llm_provider()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Relationship extraction could not start", extra={"error": str(exc)}
        )
        return

    for memory_id in memory_ids:
        try:
            async with session_factory() as session:
                memory = await session.get(Memory, memory_id)
                if memory is None:
                    continue

                service = RelationshipService(
                    session=session, provider=provider, settings=settings
                )
                stored = await service.extract_for_memory(memory)
                if stored:
                    await session.commit()
                else:
                    await session.rollback()
        except Exception as exc:  # noqa: BLE001 - contain per memory
            logger.error(
                "Relationship extraction task failed",
                extra={"memory_id": str(memory_id), "error": str(exc)},
                exc_info=exc,
            )
