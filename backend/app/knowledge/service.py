"""Knowledge lifecycle orchestration.

The single owner of conflict evaluation. Detection and writing are separate
modules; this ties them together and is the only thing the background pipeline
calls.

**Never on the request path.** Conflict evaluation reads and writes knowledge,
and the specification is explicit that retrieval and context assembly must not
mutate anything. Nothing in `app.retrieval`, `app.context` or `app.prompt`
imports this module, and a structural test enforces that.

**No model call.** This module imports nothing from `app.llm`. Conflict
detection is lexical and structural throughout.
"""

import time
import uuid
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import MemoryNotFoundError
from app.core.logging import get_logger
from app.knowledge.conflicts import ConflictDetector
from app.knowledge.lifecycle import LifecycleWriter
from app.knowledge.models import KnowledgeConflict
from app.knowledge.schemas import ConflictLink, EvaluationReport, MemoryLifecycle
from app.memory.models import Memory

logger = get_logger(__name__)


class KnowledgeService:
    def __init__(
        self, session: AsyncSession, settings: Optional[Settings] = None
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._detector = ConflictDetector(session)
        self._writer = LifecycleWriter(session)

    async def evaluate_memory(self, memory: Memory) -> EvaluationReport:
        """Detect and apply every conflict caused by one new memory.

        Does not commit: the caller owns the transaction, so a lifecycle
        change lands atomically with whatever else that unit of work did.
        """
        started = time.perf_counter()

        outcomes = await self._detector.detect(memory)
        report = await self._writer.apply(
            outcomes, triggering_memory_id=memory.id
        )
        report.candidates_examined = 1
        report.duration_ms = round((time.perf_counter() - started) * 1000, 2)

        if outcomes:
            # Ids and counts only. Memory text is personal data and does not
            # belong in a log with different retention from the database.
            logger.info(
                "Knowledge conflicts evaluated",
                extra={
                    "memory_id": str(memory.id),
                    "conflicts_detected": report.conflicts_detected,
                    "memories_superseded": report.memories_superseded,
                    "relationships_superseded": report.relationships_superseded,
                    "unresolved": report.unresolved,
                    "links_created": report.links_created,
                    "links_already_present": report.links_already_present,
                    "cycles_prevented": report.cycles_prevented,
                    "status_updates_failed": report.status_updates_failed,
                    "duration_ms": report.duration_ms,
                },
            )
        return report

    # --- Debug reads --------------------------------------------------------

    async def lifecycle_for_memory(self, memory_id: uuid.UUID) -> MemoryLifecycle:
        """Everything known about one memory's lifecycle. Read-only."""
        memory = await self._session.get(Memory, memory_id)
        if memory is None:
            raise MemoryNotFoundError(f"Memory {memory_id} does not exist.")

        return MemoryLifecycle(
            memory_id=memory.id,
            status=memory.status.value,
            content=memory.content,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            superseded_by=await self._links(KnowledgeConflict.older_memory_id, memory_id),
            supersedes=await self._links(KnowledgeConflict.newer_memory_id, memory_id),
            triggered=await self._links(
                KnowledgeConflict.triggering_memory_id, memory_id
            ),
        )

    async def _links(self, column, value: uuid.UUID) -> List[ConflictLink]:
        statement = (
            select(KnowledgeConflict)
            .where(column == value)
            .order_by(KnowledgeConflict.detected_at.desc())
            .limit(100)
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return [
            ConflictLink(
                id=row.id,
                resolution=row.resolution,
                reason=row.reason,
                older_memory_id=row.older_memory_id,
                newer_memory_id=row.newer_memory_id,
                older_relationship_id=row.older_relationship_id,
                newer_relationship_id=row.newer_relationship_id,
                triggering_memory_id=row.triggering_memory_id,
                detected_at=row.detected_at,
            )
            for row in rows
        ]


__all__ = ["KnowledgeService"]
