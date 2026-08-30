"""Context retrieval orchestration.

Runs on the **request path**, before the chat model call, and adds **zero
model calls** -- every step is a bounded, indexed database query.

    query -> normalize -> match entities -> collect candidates
          -> rank -> deduplicate -> budget -> RetrievalResult

Retrieval degrades gracefully. Each source is wrapped independently: if
relationship lookup fails, memories and entities are still used; if everything
fails, an empty package is returned and the chat turn proceeds on recent
conversation alone. Retrieval must never break chat.

Stage 3C made retrieval lifecycle-aware. By default only ACTIVE knowledge is
eligible, so anything the background pipeline marked SUPERSEDED stops being
offered as current fact. A query with explicit historical intent ("what did I
use before?") widens the eligible set to include SUPERSEDED -- the one route
by which retired knowledge reaches a prompt.

**Retrieval remains read-only.** Lifecycle state is consulted here and changed
only by the background pipeline. Nothing on the request path writes.
"""

import time
import uuid
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.retrieval.context_builder import ContextBuilder
from app.retrieval.entity_matcher import EntityMatcher
from app.retrieval.query_normalizer import NormalizedQuery, analyse
from app.retrieval.ranker import Ranker
from app.retrieval.retrievers import (
    DEFAULT_MEMORY_STATUSES,
    DEFAULT_RELATIONSHIP_STATUSES,
    HISTORICAL_MEMORY_STATUSES,
    HISTORICAL_RELATIONSHIP_STATUSES,
    MemoryCandidate,
    MemoryRetriever,
    RelationshipCandidate,
    RelationshipRetriever,
    evidence_map,
)
from app.retrieval.schemas import (
    RetrievalResult,
    RetrievalMetadata,
    RetrievedEntity,
    RetrievedMemory,
    RetrievedRelationship,
)

logger = get_logger(__name__)


class RetrievalService:
    def __init__(
        self, session: AsyncSession, settings: Optional[Settings] = None
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._matcher = EntityMatcher(session)
        self._ranker = Ranker(self._settings)
        self._builder = ContextBuilder(self._settings)

    @property
    def builder(self) -> ContextBuilder:
        return self._builder

    @property
    def ranker(self) -> Ranker:
        return self._ranker

    async def retrieve(self, query: str) -> RetrievalResult:
        """Assemble relevant knowledge for one user message.

        Never raises.
        """
        started = time.perf_counter()
        package = RetrievalResult(query=query)

        if not self._settings.RETRIEVAL_ENABLED:
            package.metadata.enabled = False
            return package

        try:
            analysis = analyse(query)
        except Exception as exc:  # noqa: BLE001
            logger.error("Query analysis failed", extra={"error": str(exc)})
            return package

        package.metadata.normalized_query = analysis.normalized
        package.metadata.keywords = list(analysis.keywords)

        if analysis.is_empty:
            package.metadata.duration_ms = (time.perf_counter() - started) * 1000
            return package

        degraded: List[str] = []

        # Lifecycle eligibility, decided once for the whole pass. Superseded
        # knowledge is offered only when the query asked about the past, and
        # only when the feature is enabled; archived knowledge, never.
        historical = bool(
            analysis.historical_intent
            and self._settings.HISTORICAL_RETRIEVAL_ENABLED
        )
        memory_statuses = (
            HISTORICAL_MEMORY_STATUSES if historical else DEFAULT_MEMORY_STATUSES
        )
        relationship_statuses = (
            HISTORICAL_RELATIONSHIP_STATUSES
            if historical
            else DEFAULT_RELATIONSHIP_STATUSES
        )
        package.metadata.historical_intent = historical

        matches = await self._safe(self._matcher.match(analysis), "entities", degraded, [])
        entity_strength: Dict[uuid.UUID, float] = {
            match.entity.id: match.strength for match in matches
        }
        entity_ids = list(entity_strength)

        relationship_candidates: List[RelationshipCandidate] = await self._safe(
            RelationshipRetriever(
                self._session,
                self._settings.RETRIEVAL_CANDIDATE_POOL_SIZE,
                statuses=relationship_statuses,
            ).collect(entity_ids),
            "relationships",
            degraded,
            [],
        )
        relationship_ids = [c.relationship.id for c in relationship_candidates]

        evidence, evidence_memory_ids = await self._safe(
            evidence_map(self._session, relationship_ids), "evidence", degraded, ({}, set())
        )

        candidates: Dict[uuid.UUID, MemoryCandidate] = await self._safe(
            MemoryRetriever(
                self._session,
                self._settings.RETRIEVAL_CANDIDATE_POOL_SIZE,
                statuses=memory_statuses,
            ).collect(
                keywords=analysis.keywords,
                entity_ids=entity_ids,
                relationship_ids=relationship_ids,
            ),
            "memories",
            degraded,
            {},
        )

        # Bound the pool before ranking, so scoring cost stays predictable.
        pool = list(candidates.values())[: self._settings.RETRIEVAL_CANDIDATE_POOL_SIZE]

        ranked_memories = self._ranker.rank_memories(
            pool, analysis.keywords, entity_strength
        )
        keyword_matched = {
            candidate.memory.id for candidate in pool if candidate.keyword_hits
        }
        ranked_relationships = self._ranker.rank_relationships(
            relationship_candidates, evidence, keyword_matched
        )

        retrieved_entities = [
            RetrievedEntity(
                id=match.entity.id,
                canonical_name=match.entity.canonical_name,
                entity_type=match.entity.entity_type.value,
                description=match.entity.description,
                match_strength=match.strength,
                matched_via=match.matched_via,
                matched_text=match.matched_text,
                rank=position,
            )
            for position, match in enumerate(matches, start=1)
        ]

        package.metadata = RetrievalMetadata(
            normalized_query=analysis.normalized,
            keywords=list(analysis.keywords),
            candidate_memories=len(pool),
            candidate_relationships=len(relationship_candidates),
            degraded_sources=degraded,
            historical_intent=historical,
        )
        package = self._builder.build(
            query=query,
            entities=retrieved_entities,
            memories=ranked_memories,
            relationships=ranked_relationships,
            package=package,
        )
        package.metadata.duration_ms = round((time.perf_counter() - started) * 1000, 2)

        logger.info(
            "Context retrieval completed",
            extra={
                "keywords": len(analysis.keywords),
                "matched_entities": len(retrieved_entities),
                "candidate_memories": package.metadata.candidate_memories,
                "selected_memories": package.metadata.selected_memories,
                "selected_relationships": package.metadata.selected_relationships,
                "context_chars": package.metadata.context_chars,
                "historical_intent": historical,
                "degraded": ",".join(degraded) or None,
                "duration_ms": package.metadata.duration_ms,
            },
        )
        return package

    def render(self, package: RetrievalResult) -> str:
        """Debug-only rendering of a retrieval result.

        **Retired from the chat prompt in Stage 3B.** Until then `ChatService`
        called this and spliced the result into the message list as a second
        system message -- Stage 2D both retrieved knowledge and decided how it
        appeared to the model. Stage 3B moved that responsibility to
        `app.prompt.formatter`, and this method now has exactly two callers,
        neither of which reaches a model:

        - `/api/retrieval/debug` and the conversation context preview, which
          show a human what was retrieved;
        - `ContextBuilder.build`, which measures the rendered length to apply
          `RETRIEVAL_MAX_CONTEXT_CHARS`.

        Nothing it returns is sent to an LLM. Do not call it from a service on
        the request path: long-term knowledge has one production route into a
        prompt, and it runs through `PromptFormatter.format`.
        """
        return self._builder.render(
            package.matched_entities, package.memories, package.relationships
        )

    async def _safe(self, awaitable, source: str, degraded: List[str], fallback):
        """Run one retrieval source, degrading to `fallback` if it fails.

        A failing source must not take the others down with it.
        """
        try:
            return await awaitable
        except Exception as exc:  # noqa: BLE001 - graceful degradation
            logger.error(
                "Retrieval source failed",
                extra={"source": source, "error": str(exc)},
            )
            degraded.append(source)
            return fallback

    # --- Debug support ------------------------------------------------------

    async def analyse_only(self, query: str) -> NormalizedQuery:
        return analyse(query)

    async def candidates_for_debug(
        self, analysis: NormalizedQuery
    ) -> "tuple":
        """Everything the debug endpoint needs, without re-running retrieval."""
        matches = await self._matcher.match(analysis)
        entity_strength = {m.entity.id: m.strength for m in matches}
        entity_ids = list(entity_strength)

        relationship_candidates = await RelationshipRetriever(
            self._session, self._settings.RETRIEVAL_CANDIDATE_POOL_SIZE
        ).collect(entity_ids)
        relationship_ids = [c.relationship.id for c in relationship_candidates]
        evidence, _ = await evidence_map(self._session, relationship_ids)

        candidates = await MemoryRetriever(
            self._session, self._settings.RETRIEVAL_CANDIDATE_POOL_SIZE
        ).collect(
            keywords=analysis.keywords,
            entity_ids=entity_ids,
            relationship_ids=relationship_ids,
        )
        pool = list(candidates.values())
        return matches, entity_strength, relationship_candidates, evidence, pool


def selected_ids(memories: Sequence[RetrievedMemory]) -> Set[uuid.UUID]:
    return {memory.id for memory in memories}


__all__ = ["RetrievalService", "RetrievedRelationship", "selected_ids"]
