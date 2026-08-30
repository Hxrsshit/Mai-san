"""Context assembly orchestration.

Gathers the two optional sources -- recent conversation and Stage 2D retrieval
-- and hands them to the assembler.

**Read-only.** Nothing here creates, updates or deletes any memory, entity,
relationship or evidence row. Context assembly is a transformation layer.

**No model calls.** Retrieval is delegated to Stage 2D, which is itself
database-only; this module imports nothing from `app.llm`.
"""

import time
import uuid
from typing import List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.context.assembler import ContextAssembler
from app.context.budget import BudgetLimits
from app.context.schemas import ContextPackage
from app.retrieval.schemas import RetrievalResult
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


class ContextService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        retrieval_service: Optional[RetrievalService] = None,
        conversation_service: Optional[ConversationService] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._retrieval = retrieval_service or RetrievalService(
            session, self._settings
        )
        self._conversations = conversation_service or ConversationService(session)
        self._assembler = ContextAssembler(self.limits)

    @property
    def limits(self) -> BudgetLimits:
        return BudgetLimits(
            recent_message_limit=self._settings.CONTEXT_RECENT_MESSAGE_LIMIT,
            max_memory_items=self._settings.CONTEXT_MAX_MEMORY_ITEMS,
            max_entity_items=self._settings.CONTEXT_MAX_ENTITY_ITEMS,
            max_relationship_items=self._settings.CONTEXT_MAX_RELATIONSHIP_ITEMS,
            max_total_chars=self._settings.CONTEXT_MAX_TOTAL_CHARS,
        )

    async def build(
        self,
        current_message: str,
        conversation_id: Optional[uuid.UUID] = None,
    ) -> ContextPackage:
        """Assemble the context package for one message.

        Never raises. Each optional source is gathered independently, so a
        failure in one leaves the others intact -- and the current message is
        always present, whatever else fails.
        """
        started = time.perf_counter()
        degraded: List[str] = []

        recent = await self._safe_recent(conversation_id, degraded)
        retrieval = await self._safe_retrieval(current_message, degraded)

        package = self._assembler.assemble(
            current_message=current_message,
            recent_messages=recent,
            retrieval=retrieval,
            conversation_id=conversation_id,
            degraded_sources=degraded,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

        logger.info(
            "Context assembled",
            extra={
                "conversation_id": str(conversation_id) if conversation_id else None,
                "recent_messages": package.metadata.recent_message_count,
                "memories": package.metadata.memory_count,
                "entities": package.metadata.entity_count,
                "relationships": package.metadata.relationship_count,
                "total_chars": package.metadata.characters.total,
                "dropped": package.metadata.dropped_count,
                "degraded": ",".join(degraded) or None,
                "duration_ms": package.metadata.duration_ms,
            },
        )
        return package

    # --- Source gathering ---------------------------------------------------

    async def _safe_recent(
        self, conversation_id: Optional[uuid.UUID], degraded: List[str]
    ) -> Sequence:
        """Recent conversation, bounded.

        Reuses `ConversationService.get_messages(limit=...)`, which already
        selects the newest N and returns them oldest-first -- the ordering the
        model should see. No second implementation is needed.
        """
        if conversation_id is None:
            return []
        try:
            return await self._conversations.get_messages(
                conversation_id,
                limit=self._settings.CONTEXT_RECENT_MESSAGE_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - degrade, never fail
            logger.error(
                "Recent conversation unavailable for context assembly",
                extra={"conversation_id": str(conversation_id), "error": str(exc)},
            )
            degraded.append("recent_conversation")
            return []

    async def _safe_retrieval(
        self, current_message: str, degraded: List[str]
    ) -> Optional[RetrievalResult]:
        """Stage 2D's ranked result. Consumed, never recomputed."""
        try:
            return await self._retrieval.retrieve(current_message)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail
            logger.error(
                "Long-term retrieval unavailable for context assembly",
                extra={"error": str(exc)},
            )
            degraded.append("long_term_knowledge")
            return None
