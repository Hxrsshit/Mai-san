"""Memory persistence and the extraction pipeline.

Pipeline, in order:

    turn -> extract candidates -> threshold filter -> deduplicate -> store

Only this module writes memories. The LLM proposes; the application decides.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseError, MemoryNotFoundError
from app.core.logging import get_logger
from app.database.models.base import utcnow
from app.database.models.conversation import Conversation
from app.database.models.message import Message
from app.identity.scope import KnowledgeScope
from app.llm.base import LLMProvider
from app.memory.deduplication import DuplicateMatch, find_duplicate, normalize
from app.memory.extractor import MemoryExtractor
from app.memory.models import Memory, MemoryOrigin, MemoryStatus, MemoryType
from app.memory.schemas import MemoryCandidate

logger = get_logger(__name__)

# Connection failures surface as bare OSErrors before SQLAlchemy wraps them.
_DB_ERRORS = (SQLAlchemyError, OSError)

# Width of the indexed normalized_content column.
NORMALIZED_CONTENT_LENGTH = 1000


@dataclass(frozen=True)
class MemoryProvenance:
    """Where a memory came from, and when it was said.

    One object rather than a widening parameter list, so there stays exactly
    one place that inserts a memory. Stage 5C added a second kind of source
    (an archived message from an imported export) and the alternative -- a
    parallel insert path for imported memories -- would have meant two
    implementations of deduplication and, before long, two behaviours.

    `stated_at` defaults to now for live memories, which is the truth: the
    user just said it. For imported memories the caller supplies the original
    timestamp, and that is what conflict resolution judges recency on.
    """

    origin: MemoryOrigin
    conversation_id: Optional[uuid.UUID] = None
    imported_message_id: Optional[uuid.UUID] = None
    #: Live only: the model may name a source message within the turn.
    fallback_message_id: Optional[uuid.UUID] = None
    stated_at: Optional[datetime] = None

    @classmethod
    def live(
        cls,
        conversation_id: uuid.UUID,
        fallback_message_id: Optional[uuid.UUID] = None,
    ) -> "MemoryProvenance":
        return cls(
            origin=MemoryOrigin.LIVE,
            conversation_id=conversation_id,
            fallback_message_id=fallback_message_id,
        )

    @classmethod
    def imported(
        cls, imported_message_id: uuid.UUID, stated_at: Optional[datetime]
    ) -> "MemoryProvenance":
        return cls(
            origin=MemoryOrigin.IMPORTED,
            imported_message_id=imported_message_id,
            stated_at=stated_at,
        )


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
        *,
        scope: KnowledgeScope,
    ) -> List[Memory]:
        """Analyse one completed turn and store whatever survives validation.

        Never raises. This runs after the chat response has already been
        returned, so a failure here must stay contained.

        `scope` is the trusted scope of the turn that produced the messages
        (Stage 7B unit 4), handed over by the route -- never derived from the
        conversation, the messages or the model's output. The scope bounds
        the write too: unless the conversation (and the source message, when
        given) is found inside it, nothing is extracted or stored.
        """
        if not self._settings.MEMORY_EXTRACTION_ENABLED:
            return []
        if self._extractor is None:
            logger.warning("Memory extraction skipped: no provider configured")
            return []
        if not await self._turn_in_scope(conversation_id, source_message_id, scope):
            logger.warning(
                "Memory extraction refused: the turn is not in its scope",
                extra={"conversation_id": str(conversation_id)},
            )
            return []

        recent_context = await self._earlier_context(
            conversation_id, source_message_id, scope
        )
        logger.info(
            "Memory extraction started",
            extra={
                "conversation_id": str(conversation_id),
                "context_messages": len(recent_context),
            },
        )

        try:
            candidates = await self._extractor.extract(
                user_message=user_message,
                assistant_message=assistant_message,
                recent_context=recent_context,
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
                    provenance=MemoryProvenance.live(
                        conversation_id=conversation_id,
                        fallback_message_id=source_message_id,
                    ),
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

    @property
    def extractor(self) -> Optional[MemoryExtractor]:
        """The configured extractor, or None. Read-only.

        Exposed so a caller can ask whether extraction is possible before
        doing the work of assembling input for it, without reaching into a
        private attribute.
        """
        return self._extractor

    async def store_imported(
        self, user_text: str, provenance: "MemoryProvenance"
    ) -> List[Memory]:
        """Derive memories from imported user text. Stage 5C.

        Only the user's own words are passed in, and `assistant_message` is
        deliberately empty. The extraction prompt already forbids deriving
        facts from assistant text, but a prompt is an instruction and this is
        untrusted archive content: giving the model no assistant text at all
        makes "an assistant claim became a user fact" structurally impossible
        rather than merely disallowed.

        Everything after extraction is the live path -- the same thresholds,
        the same deduplication, the same single insert site. An imported
        memory that restates one Mai already holds is dropped exactly as a
        live duplicate would be.

        Never raises, for the same reason `extract_and_store` never raises: an
        import must not fail because one conversation confused a model.
        """
        if not self._settings.MEMORY_EXTRACTION_ENABLED:
            return []
        if self._extractor is None:
            return []
        if provenance.origin is not MemoryOrigin.IMPORTED:
            # A programming error, not a runtime condition: this path writes
            # imported provenance and nothing else should reach it.
            raise ValueError("store_imported requires imported provenance")

        try:
            candidates = await self._extractor.extract(
                user_message=user_text, assistant_message=""
            )
        except Exception as exc:  # noqa: BLE001 - must never escape an import
            logger.error(
                "Imported memory extraction raised unexpectedly",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return []

        accepted = self._filter_by_thresholds(candidates)

        stored: List[Memory] = []
        for candidate in accepted:
            try:
                memory = await self._store_candidate(
                    candidate=candidate, provenance=provenance
                )
            except DatabaseError as exc:
                logger.error(
                    "Failed to store an imported memory",
                    extra={"error": str(exc)},
                )
                continue
            if memory is not None:
                stored.append(memory)
        return stored

    async def _turn_in_scope(
        self,
        conversation_id: uuid.UUID,
        source_message_id: Optional[uuid.UUID],
        scope: KnowledgeScope,
    ) -> bool:
        """The conversation -- and the source message, if any -- exist inside
        `scope`. Checked before anything is extracted, whatever the context
        settings, so a memory's provenance can only ever point into its own
        scope. Never raises: anything unexpected is a refusal."""
        if not isinstance(scope, KnowledgeScope):
            return False
        try:
            statement = select(Conversation.id).where(
                Conversation.id == conversation_id, scope.clause(Conversation)
            )
            if source_message_id is not None:
                statement = statement.join(
                    Message, Message.conversation_id == Conversation.id
                ).where(Message.id == source_message_id)
            return (await self._session.execute(statement)).first() is not None
        except Exception as exc:  # noqa: BLE001 - a refusal, never an error
            logger.warning(
                "Memory extraction scope check failed",
                extra={"conversation_id": str(conversation_id), "category": type(exc).__name__},
            )
            return False

    async def _earlier_context(
        self,
        conversation_id: uuid.UUID,
        source_message_id: Optional[uuid.UUID],
        scope: KnowledgeScope,
    ) -> List[str]:
        """The turns before the analysed message, oldest first, bounded.

        Without them the extractor saw a reply in isolation: "tall, wears
        glasses" answering "what is your ideal partner like?" was stored as a
        description of the *user*. Context for interpretation only -- the
        prompt still names the user's own message as the sole source of
        memories. Never raises: no context is a degraded extraction, not a
        failed one.

        Stage 7B unit 4: only within `scope`. The source message must belong
        to a conversation of the scope; otherwise there is no context at all,
        so the extractor never sees another member's turns.
        """
        limit = max(0, self._settings.MEMORY_EXTRACTION_CONTEXT_MESSAGES)
        if not limit or source_message_id is None:
            return []
        try:
            source_at = (
                await self._session.execute(
                    select(Message.created_at)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(
                        Message.id == source_message_id,
                        Message.conversation_id == conversation_id,
                        scope.clause(Conversation),
                    )
                )
            ).scalar_one_or_none()
            if source_at is None:
                return []
            rows = (
                await self._session.execute(
                    select(Message.role, Message.content)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.created_at < source_at,
                    )
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            ).all()
        except SQLAlchemyError as exc:
            logger.warning(
                "Earlier context unavailable for memory extraction",
                extra={"conversation_id": str(conversation_id), "error": str(exc)},
            )
            return []
        cap = max(1, self._settings.MEMORY_EXTRACTION_CONTEXT_CHARS)
        lines = []
        for role, content in reversed(rows):
            role = getattr(role, "value", role)
            text = " ".join((content or "").split())
            if len(text) > cap:
                text = text[: cap - 1].rstrip() + "…"
            lines.append(f"{role}: {text}")
        return lines

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
        provenance: "MemoryProvenance",
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
            origin=provenance.origin,
            stated_at=provenance.stated_at or utcnow(),
            source_conversation_id=provenance.conversation_id,
            source_imported_message_id=provenance.imported_message_id,
            # The model may name a source message; fall back to the turn's
            # user message. A model-named id is trusted only when it is a
            # message of this turn's own conversation (Stage 7B unit 4) --
            # never another conversation's, so model output cannot point a
            # memory's provenance into another scope. Imported memories have no live message
            # to point at, so the lookup is skipped entirely rather than
            # resolving a live id against archived content.
            source_message_id=(
                await self._resolve_source_message(
                    candidate.source_message_id,
                    provenance.fallback_message_id,
                    provenance.conversation_id,
                )
                if provenance.origin is MemoryOrigin.LIVE
                else None
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
        self,
        proposed: Optional[uuid.UUID],
        fallback: Optional[uuid.UUID],
        conversation_id: Optional[uuid.UUID],
    ) -> Optional[uuid.UUID]:
        """Accept a model-supplied message id only if it is a message of the
        turn's own conversation; otherwise the turn's user message."""
        if proposed is None or conversation_id is None:
            return fallback
        try:
            found = (
                await self._session.execute(
                    select(Message.id).where(
                        Message.id == proposed, Message.conversation_id == conversation_id
                    )
                )
            ).first()
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
