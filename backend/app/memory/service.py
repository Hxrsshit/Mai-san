"""Memory persistence and the extraction pipeline.

Pipeline, in order:

    turn -> extract candidates -> threshold filter -> deduplicate -> store

Only this module writes memories. The LLM proposes; the application decides.
"""

import uuid
from typing import List, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseError, MemoryNotFoundError
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.memory.deduplication import DuplicateMatch, find_duplicate, normalize
from app.memory.extractor import MemoryExtractor
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.memory.schemas import MemoryCandidate

logger = get_logger(__name__)

# Connection failures surface as bare OSErrors before SQLAlchemy wraps them.
_DB_ERRORS = (SQLAlchemyError, OSError)

# Width of the indexed normalized_content column.
NORMALIZED_CONTENT_LENGTH = 1000


class MemoryService:
    def __init__(
        self,
        session: AsyncSession,
        provider: Optional[LLMProvider] = None,
        settings: Optional[Settings] = None,
        extractor: Optional[MemoryExtractor] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._provider = provider
        self._extractor = extractor or (
            MemoryExtractor(provider, self._settings) if provider else None
        )

    # --- Extraction pipeline ------------------------------------------------

    async def extract_and_store(
        self,
        conversation_id: uuid.UUID,
        user_message: str,
        assistant_message: str,
        source_message_id: Optional[uuid.UUID] = None,
    ) -> List[Memory]:
        """Analyse one completed turn and store whatever survives validation.

        Never raises. This runs after the chat response has already been
        returned, so a failure here must stay contained.
        """
        if not self._settings.MEMORY_EXTRACTION_ENABLED:
            return []
        if self._extractor is None:
            logger.warning("Memory extraction skipped: no provider configured")
            return []

        logger.info(
            "Memory extraction started",
            extra={"conversation_id": str(conversation_id)},
        )

        try:
            candidates = await self._extractor.extract(
                user_message=user_message, assistant_message=assistant_message
            )
        except Exception as exc:  # noqa: BLE001 - belt and braces
            logger.error(
                "Memory extraction raised unexpectedly",
                extra={"conversation_id": str(conversation_id), "error": str(exc)},
                exc_info=exc,
            )
            return []

        accepted = self._filter_by_thresholds(candidates)

        stored: List[Memory] = []
        duplicates = 0
        try:
            for candidate in accepted:
                memory = await self._store_candidate(
                    candidate=candidate,
                    conversation_id=conversation_id,
                    fallback_message_id=source_message_id,
                )
                if memory is None:
                    duplicates += 1
                else:
                    stored.append(memory)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to store extracted memories",
                extra={"conversation_id": str(conversation_id), "error": str(exc)},
                exc_info=exc,
            )
            return []

        logger.info(
            "Memory extraction completed",
            extra={
                "conversation_id": str(conversation_id),
                "candidates": len(candidates),
                "below_threshold": len(candidates) - len(accepted),
                "duplicates": duplicates,
                "stored": len(stored),
            },
        )
        return stored

    def _filter_by_thresholds(
        self, candidates: Sequence[MemoryCandidate]
    ) -> List[MemoryCandidate]:
        """Drop candidates the model itself scored as weak."""
        kept: List[MemoryCandidate] = []
        for candidate in candidates:
            if candidate.importance_score < self._settings.MEMORY_MIN_IMPORTANCE:
                logger.info(
                    "Memory rejected: importance below threshold",
                    extra={
                        "importance": candidate.importance_score,
                        "threshold": self._settings.MEMORY_MIN_IMPORTANCE,
                        "memory_type": candidate.memory_type.value,
                    },
                )
                continue
            if candidate.confidence_score < self._settings.MEMORY_MIN_CONFIDENCE:
                logger.info(
                    "Memory rejected: confidence below threshold",
                    extra={
                        "confidence": candidate.confidence_score,
                        "threshold": self._settings.MEMORY_MIN_CONFIDENCE,
                        "memory_type": candidate.memory_type.value,
                    },
                )
                continue
            kept.append(candidate)
        return kept

    async def _store_candidate(
        self,
        candidate: MemoryCandidate,
        conversation_id: uuid.UUID,
        fallback_message_id: Optional[uuid.UUID],
    ) -> Optional[Memory]:
        """Store one candidate, or return None if it duplicates an existing one."""
        normalized = normalize(candidate.content)

        # Exact duplicates are checked against the whole table, not just the
        # recent window: an identical memory from months ago is still a
        # duplicate. This is an indexed equality lookup on normalized_content,
        # so it stays cheap as the store grows.
        duplicate = await self._find_exact_duplicate(candidate, normalized)

        if duplicate is None:
            # Fuzzy matching is necessarily bounded -- it compares text pairwise.
            existing = await self._recent_for_dedup(candidate.memory_type)
            duplicate = find_duplicate(
                candidate.content, existing, self._settings.MEMORY_DEDUP_THRESHOLD
            )
        if duplicate is not None:
            logger.info(
                "Memory rejected: duplicate of an existing memory",
                extra={
                    "reason": duplicate.reason,
                    "similarity": round(duplicate.score, 3),
                    "existing_memory_id": str(duplicate.existing.id),
                    "memory_type": candidate.memory_type.value,
                },
            )
            # Deliberately conservative: the existing memory is left untouched.
            # Supersession and contradiction handling belong to a later stage.
            return None

        memory = Memory(
            content=candidate.content,
            normalized_content=normalized[:NORMALIZED_CONTENT_LENGTH],
            memory_type=candidate.memory_type,
            status=MemoryStatus.ACTIVE,
            importance_score=candidate.importance_score,
            confidence_score=candidate.confidence_score,
            source_conversation_id=conversation_id,
            # The model may name a source message; fall back to the turn's
            # user message. An invented id would break the FK, so it is only
            # trusted when it resolves.
            source_message_id=await self._resolve_source_message(
                candidate.source_message_id, fallback_message_id
            ),
        )
        try:
            # A savepoint keeps a unique-violation from poisoning the whole
            # batch: only this insert is rolled back.
            async with self._session.begin_nested():
                self._session.add(memory)
                await self._session.flush()
        except IntegrityError:
            # Another extraction stored the same memory concurrently. The
            # application check above missed it because that insert was not
            # yet committed; the database caught it.
            logger.info(
                "Memory rejected: duplicate detected by the database",
                extra={
                    "reason": "unique_constraint",
                    "memory_type": candidate.memory_type.value,
                },
            )
            return None
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("store memory", exc)

        logger.info(
            "Memory stored",
            extra={
                "memory_id": str(memory.id),
                "memory_type": memory.memory_type.value,
                "importance": memory.importance_score,
                "confidence": memory.confidence_score,
            },
        )
        return memory

    async def _resolve_source_message(
        self, proposed: Optional[uuid.UUID], fallback: Optional[uuid.UUID]
    ) -> Optional[uuid.UUID]:
        """Only accept a model-supplied message id if that message exists."""
        if proposed is None:
            return fallback
        from app.database.models import Message

        try:
            found = await self._session.get(Message, proposed)
        except _DB_ERRORS:
            return fallback
        return proposed if found is not None else fallback

    async def _find_exact_duplicate(
        self, candidate: MemoryCandidate, normalized: str
    ) -> Optional[DuplicateMatch]:
        """Indexed lookup for an identical memory of the same type."""
        statement = select(Memory).where(
            Memory.normalized_content == normalized[:NORMALIZED_CONTENT_LENGTH],
            Memory.memory_type == candidate.memory_type,
            Memory.status == MemoryStatus.ACTIVE,
        )
        try:
            result = await self._session.execute(statement)
            matches = result.scalars().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("look up an exact duplicate memory", exc)

        for memory in matches:
            # The stored column is truncated, so confirm on the full text
            # before declaring a match.
            if normalize(memory.content) == normalized:
                return DuplicateMatch(existing=memory, score=1.0, reason="exact")
        return None

    async def _recent_for_dedup(self, memory_type: MemoryType) -> Sequence[Memory]:
        """A bounded window of same-type memories to compare against.

        Comparing only within a type avoids merging a goal with a similarly
        worded preference, and keeps the comparison set small.
        """
        statement = (
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                Memory.status == MemoryStatus.ACTIVE,
            )
            .order_by(Memory.created_at.desc())
            .limit(self._settings.MEMORY_DEDUP_CANDIDATES)
        )
        try:
            result = await self._session.execute(statement)
            return result.scalars().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("load memories for deduplication", exc)

    # --- Queries ------------------------------------------------------------

    async def list_memories(
        self,
        memory_type: Optional[MemoryType] = None,
        status: Optional[MemoryStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Memory]:
        statement = select(Memory)
        if memory_type is not None:
            statement = statement.where(Memory.memory_type == memory_type)
        if status is not None:
            statement = statement.where(Memory.status == status)

        statement = (
            statement.order_by(Memory.created_at.desc()).limit(limit).offset(offset)
        )
        try:
            result = await self._session.execute(statement)
            return result.scalars().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("list memories", exc)

    async def count_memories(
        self,
        memory_type: Optional[MemoryType] = None,
        status: Optional[MemoryStatus] = None,
    ) -> int:
        statement = select(func.count()).select_from(Memory)
        if memory_type is not None:
            statement = statement.where(Memory.memory_type == memory_type)
        if status is not None:
            statement = statement.where(Memory.status == status)
        try:
            result = await self._session.execute(statement)
            return int(result.scalar_one())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("count memories", exc)

    async def get_memory(self, memory_id: uuid.UUID) -> Memory:
        try:
            memory = await self._session.get(Memory, memory_id)
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("get memory", exc)
        if memory is None:
            raise MemoryNotFoundError(f"Memory {memory_id} does not exist.")
        return memory

    async def delete_memory(self, memory_id: uuid.UUID) -> None:
        try:
            result = await self._session.execute(
                delete(Memory).where(Memory.id == memory_id)
            )
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("delete memory", exc)
        if result.rowcount == 0:
            raise MemoryNotFoundError(f"Memory {memory_id} does not exist.")
        logger.info("Memory deleted", extra={"memory_id": str(memory_id)})

    @staticmethod
    def _wrap_db_error(action: str, exc: Exception) -> DatabaseError:
        logger.error(
            "Database operation failed",
            extra={"action": action, "error": str(exc)},
            exc_info=exc,
        )
        return DatabaseError(f"Could not {action}.")
