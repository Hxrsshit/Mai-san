"""Applying lifecycle decisions.

The only module that changes a `status` or writes a `KnowledgeConflict`.
Detection is read-only and lives in `conflicts.py`; keeping the write side
alone here means a detection change can never half-mutate the knowledge base.

Three invariants are enforced here rather than hoped for:

1. **History is never destroyed.** Nothing is deleted, and no `content`,
   `created_at` or score is ever modified. A status column moves; that is all.
2. **No cycle.** A supersession that would close a loop -- A superseded by B
   where B is already superseded by A, directly or through a chain -- is
   refused and counted.
3. **Partial states are avoided.** Each decision is applied inside its own
   SAVEPOINT. If the link or the status update fails, that one decision rolls
   back whole and the item stays ACTIVE with nothing written, which is the
   state the specification prefers over an inconsistent one.

Concurrency is handled by the database, not by application checks. Two
background evaluations cannot see each other's uncommitted rows, so the
uniqueness of a conflict link is enforced by a UNIQUE index and a duplicate
surfaces as `IntegrityError` -- expected, counted, and not an error.
"""

import uuid
from typing import List, Optional, Sequence, Set, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.knowledge.models import (
    ConflictResolution,
    KnowledgeConflict,
)
from app.knowledge.schemas import ConflictOutcome, EvaluationReport
from app.memory.models import Memory, MemoryStatus
from app.relationships.models import Relationship, RelationshipStatus

logger = get_logger(__name__)

#: How far a supersession chain is walked when checking for a cycle. Chains
#: are short in practice; the cap bounds a pathological case rather than
#: expressing a real limit.
MAX_CHAIN_DEPTH = 32


class LifecycleWriter:
    """Applies `ConflictOutcome` decisions to the knowledge base."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def apply(
        self,
        outcomes: Sequence[ConflictOutcome],
        triggering_memory_id: Optional[uuid.UUID] = None,
    ) -> EvaluationReport:
        """Write every decision. Returns what happened.

        Each decision is independent: one that cannot be applied does not
        prevent the others.
        """
        report = EvaluationReport(conflicts_detected=len(outcomes))

        for outcome in outcomes:
            await self._apply_one(outcome, triggering_memory_id, report)
        return report

    async def _apply_one(
        self,
        outcome: ConflictOutcome,
        triggering_memory_id: Optional[uuid.UUID],
        report: EvaluationReport,
    ) -> None:
        if outcome.supersedes and await self._would_cycle(outcome):
            report.cycles_prevented += 1
            logger.info(
                "Supersession refused: it would close a cycle",
                extra={
                    "older_memory_id": _s(outcome.older_memory_id),
                    "newer_memory_id": _s(outcome.newer_memory_id),
                    "older_relationship_id": _s(outcome.older_relationship_id),
                    "newer_relationship_id": _s(outcome.newer_relationship_id),
                },
            )
            return

        try:
            # SAVEPOINT: this one decision commits or vanishes. Without it a
            # constraint violation would poison the whole transaction and take
            # the other decisions -- and the extraction that ran before them --
            # down with it.
            async with self._session.begin_nested():
                self._session.add(
                    KnowledgeConflict(
                        older_memory_id=outcome.older_memory_id,
                        newer_memory_id=outcome.newer_memory_id,
                        older_relationship_id=outcome.older_relationship_id,
                        newer_relationship_id=outcome.newer_relationship_id,
                        resolution=outcome.resolution,
                        reason=outcome.reason,
                        triggering_memory_id=triggering_memory_id,
                    )
                )
                await self._session.flush()

                if outcome.supersedes:
                    await self._mark_superseded(outcome)
        except IntegrityError:
            # Either a concurrent evaluation already recorded this link, or
            # the status change collided with the relationship triple's
            # uniqueness. Both leave the knowledge base consistent: the item
            # stays as it was.
            report.links_already_present += 1
            logger.info(
                "Lifecycle decision already recorded or not applicable",
                extra={
                    "resolution": outcome.resolution.value,
                    "older_memory_id": _s(outcome.older_memory_id),
                    "older_relationship_id": _s(outcome.older_relationship_id),
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 - one decision must not cascade
            report.status_updates_failed += 1
            logger.error(
                "Lifecycle decision could not be applied",
                extra={
                    "resolution": outcome.resolution.value,
                    "older_memory_id": _s(outcome.older_memory_id),
                    "older_relationship_id": _s(outcome.older_relationship_id),
                    "error": str(exc),
                },
            )
            return

        report.links_created += 1
        if outcome.resolution is ConflictResolution.UNRESOLVED:
            report.unresolved += 1
        elif outcome.older_memory_id is not None:
            report.memories_superseded += 1
        else:
            report.relationships_superseded += 1

    async def _mark_superseded(self, outcome: ConflictOutcome) -> None:
        """Move one row's status. Content is never touched.

        A bulk UPDATE rather than an ORM attribute set: the row may not be
        loaded in this session at all. `synchronize_session="fetch"` costs one
        extra SELECT of a single row and keeps any instance that *is* loaded
        from continuing to report the old status -- a stale read here would be
        a subtle trap for anything reading back in the same session.
        """
        if outcome.older_memory_id is not None:
            await self._session.execute(
                update(Memory)
                .where(
                    Memory.id == outcome.older_memory_id,
                    Memory.status == MemoryStatus.ACTIVE,
                )
                .values(status=MemoryStatus.SUPERSEDED)
                .execution_options(synchronize_session="fetch")
            )
            return

        # `uq_relationships_triple` includes status, so this can collide with
        # an existing superseded row carrying the same triple. That surfaces as
        # IntegrityError and is handled by the caller: the relationship stays
        # ACTIVE rather than being left in a half-written state.
        await self._session.execute(
            update(Relationship)
            .where(
                Relationship.id == outcome.older_relationship_id,
                Relationship.status == RelationshipStatus.ACTIVE,
            )
            .values(status=RelationshipStatus.SUPERSEDED)
            .execution_options(synchronize_session="fetch")
        )

    # --- Cycle prevention ---------------------------------------------------

    async def _would_cycle(self, outcome: ConflictOutcome) -> bool:
        """True when superseding would make A and B supersede each other.

        Walks forward from the *newer* item: if following its own supersession
        links ever reaches the older item, writing this link would close a
        loop and "what replaced this?" would have no answer.
        """
        if outcome.older_memory_id is not None:
            if outcome.newer_memory_id is None:
                return False
            if outcome.older_memory_id == outcome.newer_memory_id:
                return True
            return await self._reaches(
                start=outcome.newer_memory_id,
                target=outcome.older_memory_id,
                memories=True,
            )

        if outcome.newer_relationship_id is None:
            return False
        if outcome.older_relationship_id == outcome.newer_relationship_id:
            return True
        return await self._reaches(
            start=outcome.newer_relationship_id,
            target=outcome.older_relationship_id,
            memories=False,
        )

    async def _reaches(
        self, start: uuid.UUID, target: uuid.UUID, memories: bool
    ) -> bool:
        """Breadth-first walk along existing supersession links."""
        older_column = (
            KnowledgeConflict.older_memory_id
            if memories
            else KnowledgeConflict.older_relationship_id
        )
        newer_column = (
            KnowledgeConflict.newer_memory_id
            if memories
            else KnowledgeConflict.newer_relationship_id
        )

        seen: Set[uuid.UUID] = {start}
        frontier: List[uuid.UUID] = [start]

        for _ in range(MAX_CHAIN_DEPTH):
            if not frontier:
                return False

            statement = select(newer_column).where(
                older_column.in_(frontier),
                newer_column.isnot(None),
                KnowledgeConflict.resolution == ConflictResolution.SUPERSEDED,
            )
            rows = (await self._session.execute(statement)).scalars().all()

            frontier = []
            for row in rows:
                if row == target:
                    return True
                if row not in seen:
                    seen.add(row)
                    frontier.append(row)
        return False


def _s(value: Optional[uuid.UUID]) -> Optional[str]:
    return str(value) if value is not None else None


__all__ = ["LifecycleWriter", "MAX_CHAIN_DEPTH"]
