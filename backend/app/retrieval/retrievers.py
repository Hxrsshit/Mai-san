"""Bounded candidate retrieval for memories and relationships.

Every query here is bounded and indexed. The whole memory table is never
loaded: candidates come from three narrow sources, each capped, and the union
is capped again before ranking.

Relationship retrieval is strictly **one hop** -- relationships touching a
matched entity, and nothing beyond. There is no traversal from those
relationships' far ends.

Stage 3C made both retrievers lifecycle-aware. They take the set of statuses
they may return, defaulting to ACTIVE alone -- the behaviour every earlier
stage relied on. A query with explicit historical intent widens that set to
include SUPERSEDED, which is the only way retired knowledge ever reaches a
prompt. ARCHIVED is never included by either path.
"""

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.core.logging import get_logger
from app.entities.models import Entity, MemoryEntity
from app.memory.models import Memory, MemoryStatus
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
)

logger = get_logger(__name__)


@dataclass
class MemoryCandidate:
    """A memory in the candidate pool, with the signals that surfaced it."""

    memory: Memory
    keyword_hits: Set[str] = field(default_factory=set)
    entity_ids: Set[uuid.UUID] = field(default_factory=set)
    relationship_ids: Set[uuid.UUID] = field(default_factory=set)

    @property
    def sources(self) -> List[str]:
        found = []
        if self.keyword_hits:
            found.append("keyword")
        if self.entity_ids:
            found.append("entity")
        if self.relationship_ids:
            found.append("relationship")
        return found


#: What retrieval returns unless a caller widens it. Every stage before 3C
#: hard-coded exactly this.
DEFAULT_MEMORY_STATUSES: Tuple[MemoryStatus, ...] = (MemoryStatus.ACTIVE,)
DEFAULT_RELATIONSHIP_STATUSES: Tuple[RelationshipStatus, ...] = (
    RelationshipStatus.ACTIVE,
)

#: Widened set for a query that explicitly asks about the past. ARCHIVED is
#: deliberately absent: archiving means "do not surface this", and no query
#: intent overrides that.
HISTORICAL_MEMORY_STATUSES: Tuple[MemoryStatus, ...] = (
    MemoryStatus.ACTIVE,
    MemoryStatus.SUPERSEDED,
)
HISTORICAL_RELATIONSHIP_STATUSES: Tuple[RelationshipStatus, ...] = (
    RelationshipStatus.ACTIVE,
    RelationshipStatus.SUPERSEDED,
)


class MemoryRetriever:
    """Collects a bounded pool of candidate memories."""

    def __init__(
        self,
        session: AsyncSession,
        pool_size: int,
        statuses: Sequence[MemoryStatus] = DEFAULT_MEMORY_STATUSES,
    ) -> None:
        self._session = session
        self._pool_size = pool_size
        # Materialised once: the same list is reused across three queries, and
        # an exhausted generator would silently empty the later two.
        self._statuses = list(statuses) or list(DEFAULT_MEMORY_STATUSES)

    async def collect(
        self,
        keywords: Sequence[str],
        entity_ids: Sequence[uuid.UUID],
        relationship_ids: Sequence[uuid.UUID],
    ) -> Dict[uuid.UUID, MemoryCandidate]:
        """Union of three bounded sources, keyed by memory id.

        A memory found by several sources keeps all of them -- that is what
        lets the ranker reward multi-signal matches.
        """
        candidates: Dict[uuid.UUID, MemoryCandidate] = {}

        await self._collect_by_entity(candidates, entity_ids)
        await self._collect_by_relationship(candidates, relationship_ids)
        await self._collect_by_keyword(candidates, keywords)

        return candidates

    async def _collect_by_entity(
        self, candidates: Dict[uuid.UUID, MemoryCandidate], entity_ids: Sequence[uuid.UUID]
    ) -> None:
        """Memories linked to a matched entity. Indexed on memory_entities."""
        if not entity_ids:
            return
        statement = (
            select(Memory, MemoryEntity.entity_id)
            .join(MemoryEntity, MemoryEntity.memory_id == Memory.id)
            .where(
                MemoryEntity.entity_id.in_(list(entity_ids)),
                Memory.status.in_(self._statuses),
            )
            .order_by(Memory.importance_score.desc(), Memory.created_at.desc())
            .limit(self._pool_size)
        )
        for memory, entity_id in (await self._session.execute(statement)).all():
            candidate = candidates.setdefault(memory.id, MemoryCandidate(memory=memory))
            candidate.entity_ids.add(entity_id)

    async def _collect_by_relationship(
        self,
        candidates: Dict[uuid.UUID, MemoryCandidate],
        relationship_ids: Sequence[uuid.UUID],
    ) -> None:
        """Memories that are evidence for a relevant relationship."""
        if not relationship_ids:
            return
        statement = (
            select(Memory, RelationshipEvidence.relationship_id)
            .join(RelationshipEvidence, RelationshipEvidence.memory_id == Memory.id)
            .where(
                RelationshipEvidence.relationship_id.in_(list(relationship_ids)),
                Memory.status.in_(self._statuses),
            )
            .limit(self._pool_size)
        )
        for memory, relationship_id in (await self._session.execute(statement)).all():
            candidate = candidates.setdefault(memory.id, MemoryCandidate(memory=memory))
            candidate.relationship_ids.add(relationship_id)

    async def _collect_by_keyword(
        self, candidates: Dict[uuid.UUID, MemoryCandidate], keywords: Sequence[str]
    ) -> None:
        """Memories whose text contains a query keyword.

        A single OR'd query rather than one per keyword, so the number of
        round trips does not grow with query length. Matching runs against
        `normalized_content`, which is already lowercased and punctuation-free.
        """
        if not keywords:
            return

        conditions = [
            Memory.normalized_content.like(f"%{keyword}%") for keyword in keywords
        ]
        statement = (
            select(Memory)
            .where(Memory.status.in_(self._statuses), or_(*conditions))
            .order_by(Memory.importance_score.desc(), Memory.created_at.desc())
            .limit(self._pool_size)
        )
        for memory in (await self._session.execute(statement)).scalars().all():
            candidate = candidates.setdefault(memory.id, MemoryCandidate(memory=memory))
            normalized = memory.normalized_content or ""
            for keyword in keywords:
                if keyword in normalized:
                    candidate.keyword_hits.add(keyword)


@dataclass
class RelationshipCandidate:
    relationship: Relationship
    source_name: str
    target_name: str
    source_matched: bool
    target_matched: bool


class RelationshipRetriever:
    """Retrieves relationships one hop from the matched entities."""

    def __init__(
        self,
        session: AsyncSession,
        limit: int,
        statuses: Sequence[RelationshipStatus] = DEFAULT_RELATIONSHIP_STATUSES,
    ) -> None:
        self._session = session
        self._limit = limit
        self._statuses = list(statuses) or list(DEFAULT_RELATIONSHIP_STATUSES)

    async def collect(
        self, entity_ids: Sequence[uuid.UUID]
    ) -> List[RelationshipCandidate]:
        """Relationships where a matched entity is the source or the target.

        **One hop only.** The far end of each relationship is resolved for
        display, but is never fed back in to find further relationships.
        """
        if not entity_ids:
            return []

        matched = set(entity_ids)
        statement = (
            select(Relationship)
            .where(
                Relationship.status.in_(self._statuses),
                or_(
                    Relationship.source_entity_id.in_(list(matched)),
                    Relationship.target_entity_id.in_(list(matched)),
                ),
            )
            .order_by(Relationship.confidence_score.desc(), Relationship.created_at.desc())
            .limit(self._limit)
            # Relationship.evidence is lazy="selectin"; evidence is fetched
            # separately in one batched query, so loading it here is waste.
            .options(noload(Relationship.evidence))
        )
        relationships = (await self._session.execute(statement)).scalars().all()
        if not relationships:
            return []

        names = await self._resolve_names(relationships)

        return [
            RelationshipCandidate(
                relationship=relationship,
                source_name=names.get(relationship.source_entity_id, "?"),
                target_name=names.get(relationship.target_entity_id, "?"),
                source_matched=relationship.source_entity_id in matched,
                target_matched=relationship.target_entity_id in matched,
            )
            for relationship in relationships
        ]

    async def _resolve_names(
        self, relationships: Sequence[Relationship]
    ) -> Dict[uuid.UUID, str]:
        """One batched lookup for every endpoint, rather than one per row."""
        endpoint_ids = {r.source_entity_id for r in relationships}
        endpoint_ids |= {r.target_entity_id for r in relationships}

        rows = (
            await self._session.execute(
                select(Entity.id, Entity.canonical_name).where(
                    Entity.id.in_(list(endpoint_ids))
                )
            )
        ).all()
        return {entity_id: name for entity_id, name in rows}


async def evidence_map(
    session: AsyncSession, relationship_ids: Sequence[uuid.UUID]
) -> Tuple[Dict[uuid.UUID, Set[uuid.UUID]], Set[uuid.UUID]]:
    """Which memories support which relationships.

    One batched query. Returns the mapping and the flat set of memory ids, so
    the caller can pull those memories into the candidate pool without a
    second round trip.
    """
    if not relationship_ids:
        return {}, set()

    rows = (
        await session.execute(
            select(
                RelationshipEvidence.relationship_id, RelationshipEvidence.memory_id
            ).where(RelationshipEvidence.relationship_id.in_(list(relationship_ids)))
        )
    ).all()

    mapping: Dict[uuid.UUID, Set[uuid.UUID]] = {}
    memory_ids: Set[uuid.UUID] = set()
    for relationship_id, memory_id in rows:
        mapping.setdefault(relationship_id, set()).add(memory_id)
        memory_ids.add(memory_id)
    return mapping, memory_ids
