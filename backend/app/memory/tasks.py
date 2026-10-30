"""Post-turn analysis, run as a FastAPI background task.

Memory extraction runs first, then entity extraction over whatever memories
were stored, then relationship extraction over those same memories, then
Stage 3C conflict evaluation over the same memories once their entities and
relationships are durable. All four live in this one task deliberately: a
second concurrent background writer would race the first on the same tables.

Starlette runs background tasks after the response has been sent, so the user
never waits on extraction and never sees an error from it. No external queue
is involved: Stage 2A favours something simple and reliable.

The task opens its **own** database session. The request-scoped session is
already closed by the time this runs, so reusing it would fail.

Stage 7B units 4-6: every step runs in the turn's scope, a frozen value the
route hands over. Each step re-reads its memories with a scoped SELECT (never
`session.get`), so a memory id outside the scope is simply not found, and
builds its service with the same scope.

Stage 7B unit 7: the turn's member may be revoked while this runs. Every
step's transaction therefore starts by re-reading the member behind the
scope with a share lock (`lock_active_principal`), and does nothing -- and
stops the chain -- unless that member is still active with exactly this
scope. Revocation locks the same row, so a step either sees the revocation
or finishes before it commits; nothing is written for a member after their
revocation. A failed check is a stop, never a pass.
"""

import uuid
from typing import List, Optional

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.session import get_session_factory
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.entities.service import EntityService
from app.memory.models import Memory
from app.knowledge.service import KnowledgeService
from app.relationships.service import RelationshipService
from app.identity.principal import lock_active_principal
from app.identity.scope import KnowledgeScope
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
    *,
    scope: KnowledgeScope,
) -> None:
    """Extract and store memories for one completed turn.

    `scope` is the scope of the authenticated turn (Stage 7B unit 4): a frozen
    value handed over by the route, so the background task needs no request
    and trusts nothing it reads.

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
            if not await _member_still_active(session, scope, "memory"):
                return
            service = MemoryService(
                session=session, provider=provider, settings=settings, scope=scope
            )
            stored = await service.extract_and_store(
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
                source_message_id=source_message_id,
                scope=scope,
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
            scope=scope,
        )


async def _member_still_active(session, scope: KnowledgeScope, step: str) -> bool:
    """Whether the member behind `scope` is still active with exactly this
    scope, holding the membership row's share lock for the rest of the
    step's transaction. False rolls the transaction back and is logged with
    the step and the member id only."""
    principal = await lock_active_principal(session, scope.member_id)
    if principal is not None and KnowledgeScope.personal(principal) == scope:
        return True
    await session.rollback()
    logger.info(
        "Background knowledge step stopped: the member is no longer active",
        extra={"step": step, "member_id": str(scope.member_id)},
    )
    return False


async def _scoped_memory(session, memory_id: uuid.UUID, scope: KnowledgeScope) -> Optional[Memory]:
    """The memory, if it exists in `scope`; a filtered SELECT, so neither a
    deleted memory nor another scope's is found."""
    return (
        await session.execute(
            select(Memory).where(Memory.id == memory_id, scope.clause(Memory))
        )
    ).scalars().first()


async def run_entity_extraction(
    memory_ids: List[uuid.UUID],
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    session_factory=None,
    *,
    scope: KnowledgeScope,
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
                if not await _member_still_active(session, scope, "entities"):
                    return
                memory = await _scoped_memory(session, memory_id, scope)
                if memory is None:
                    # Deleted between the two steps, or not in the scope.
                    continue

                service = EntityService(
                    session=session, provider=provider, settings=settings, scope=scope
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
        scope=scope,
    )


async def run_relationship_extraction(
    memory_ids: List[uuid.UUID],
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    session_factory=None,
    *,
    scope: KnowledgeScope,
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
                if not await _member_still_active(session, scope, "relationships"):
                    return
                memory = await _scoped_memory(session, memory_id, scope)
                if memory is None:
                    continue

                service = RelationshipService(
                    session=session, provider=provider, settings=settings, scope=scope
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

    # Conflicts are evaluated last, once entities and relationships are
    # committed: detection resolves entity names and inspects relationship
    # shape, so running it earlier would judge an incomplete picture.
    await run_conflict_evaluation(
        memory_ids=memory_ids,
        settings=settings,
        session_factory=session_factory,
        scope=scope,
    )


async def run_conflict_evaluation(
    memory_ids: List[uuid.UUID],
    settings: Optional[Settings] = None,
    session_factory=None,
    *,
    scope: KnowledgeScope,
) -> None:
    """Stage 3C: mark superseded knowledge historical, record what conflicts.

    Takes no provider. Conflict detection is deterministic and adds **zero**
    model calls anywhere -- background or request path.

    Runs on its own session per memory, after everything else is durable, so a
    failure here can never roll back a memory, an entity, a relationship, or
    the chat turn. The knowledge simply stays ACTIVE and unresolved, which the
    specification prefers over a partially mutated state.

    Never raises.
    """
    settings = settings or get_settings()
    if not (settings.MEMORY_ENABLED and settings.CONFLICT_DETECTION_ENABLED):
        return

    try:
        if session_factory is None:
            session_factory = get_session_factory()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Conflict evaluation could not start", extra={"error": str(exc)}
        )
        return

    for memory_id in memory_ids:
        try:
            async with session_factory() as session:
                if not await _member_still_active(session, scope, "conflicts"):
                    return
                memory = await _scoped_memory(session, memory_id, scope)
                if memory is None:
                    continue

                service = KnowledgeService(session=session, settings=settings, scope=scope)
                report = await service.evaluate_memory(memory)
                if report.links_created:
                    await session.commit()
                else:
                    await session.rollback()
        except Exception as exc:  # noqa: BLE001 - contain per memory
            logger.error(
                "Conflict evaluation task failed",
                extra={"memory_id": str(memory_id), "error": str(exc)},
                exc_info=exc,
            )
