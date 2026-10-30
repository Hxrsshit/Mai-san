"""Deterministic ranking.

No model is involved. The same candidates always produce the same order, which
is what makes retrieval testable.

    RELEVANCE = w_text * text_relevance + w_ent * entity_relevance
              + w_rel  * relationship_relevance
    METADATA  = w_imp  * importance + w_conf * confidence + w_rec * recency
    FINAL     = RELEVANCE + METADATA * max(text, entity, relationship relevance)

Every component is normalised to [0, 1] before weighting, so the weights are
directly comparable and the final score also lands in [0, 1].

Relevance dominates by design: text + entity + relationship carry 0.70 of the
weight, while importance + confidence + recency carry 0.30 -- and the metadata
part is multiplied by the memory's strongest relevance signal, so metadata
orders equally relevant memories but cannot reverse a clear relevance gap. A
highly important but irrelevant (or only partly relevant) memory therefore
cannot outrank a directly relevant one.
"""

import math
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Set

from app.core.config import Settings
from app.retrieval.query_normalizer import query_keywords, term_weight
from app.retrieval.retrievers import MemoryCandidate, RelationshipCandidate
from app.retrieval.schemas import (
    MatchSignal,
    RetrievedMemory,
    RetrievedRelationship,
    ScoreBreakdown,
)

#: Recency halves every this many days. Deliberately long: "User is building
#: Mai" stays relevant for months, and the specification is explicit that old
#: knowledge must not disappear merely for being old.
RECENCY_HALF_LIFE_DAYS = 180.0

#: Relationship relevance, by how much of the relationship the query matched.
RELATIONSHIP_BOTH_ENDS = 1.0
RELATIONSHIP_ONE_END = 0.6
RELATIONSHIP_EVIDENCE_ONLY = 0.3


def recency_score(created_at: datetime, now: datetime = None) -> float:
    """Gentle exponential decay in [0, 1].

    1.0 today, 0.5 at six months, 0.25 at a year. With a weight of 0.05 this
    can only ever break ties -- it cannot overturn a relevance difference.
    """
    now = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class Ranker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def weights(self) -> Dict[str, float]:
        return {
            "text_relevance": self._settings.RETRIEVAL_WEIGHT_TEXT,
            "entity_relevance": self._settings.RETRIEVAL_WEIGHT_ENTITY,
            "relationship_relevance": self._settings.RETRIEVAL_WEIGHT_RELATIONSHIP,
            "importance": self._settings.RETRIEVAL_WEIGHT_IMPORTANCE,
            "confidence": self._settings.RETRIEVAL_WEIGHT_CONFIDENCE,
            "recency": self._settings.RETRIEVAL_WEIGHT_RECENCY,
        }

    # --- Memories -----------------------------------------------------------

    def rank_memories(
        self,
        candidates: Sequence[MemoryCandidate],
        keywords: Sequence[str],
        entity_strength: Dict[uuid.UUID, float],
        now: datetime = None,
    ) -> List[RetrievedMemory]:
        """Score, sort and number the candidate memories."""
        now = now or datetime.now(timezone.utc)
        # The keywords the memory search used, so text relevance is measured
        # against the same words that could have matched. Unchanged for any
        # question within `MAX_QUERY_KEYWORDS`.
        keywords = query_keywords(keywords)
        scored: List[RetrievedMemory] = []

        for candidate in candidates:
            breakdown, signals = self._score_memory(
                candidate, keywords, entity_strength, now
            )
            memory = candidate.memory
            scored.append(
                RetrievedMemory(
                    id=memory.id,
                    content=memory.content,
                    memory_type=memory.memory_type.value,
                    importance_score=memory.importance_score,
                    confidence_score=memory.confidence_score,
                    created_at=memory.created_at,
                    score=breakdown,
                    signals=signals,
                )
            )

        scored.sort(key=self._memory_sort_key)
        for position, item in enumerate(scored, start=1):
            item.rank = position
        return scored

    def _score_memory(
        self,
        candidate: MemoryCandidate,
        keywords: Sequence[str],
        entity_strength: Dict[uuid.UUID, float],
        now: datetime,
    ):
        memory = candidate.memory
        signals: List[MatchSignal] = []

        # Text: share of query keywords present in the memory.
        text = 0.0
        if keywords:
            # Weighted by what each word contributes: the question's subject
            # ("tea") counts fully, a preference word ("prefer") and a framing
            # word ("kind") only a little. Counting them equally tied every
            # "User prefers ..." memory with the one about tea, and importance
            # then decided the order. See `query_normalizer.TERM_WEIGHTS`.
            total = sum(term_weight(k) for k in keywords)
            matched = sum(term_weight(k) for k in keywords if k in candidate.keyword_hits)
            text = _clamp(matched / total) if total else 0.0
            if candidate.keyword_hits:
                signals.append(
                    MatchSignal(
                        name="keyword",
                        strength=text,
                        detail=",".join(sorted(candidate.keyword_hits)),
                    )
                )

        # Entity: strength of the strongest matched entity linked to it.
        entity = 0.0
        if candidate.entity_ids:
            entity = max(
                (entity_strength.get(eid, 0.0) for eid in candidate.entity_ids),
                default=0.0,
            )
            if entity:
                signals.append(MatchSignal(name="entity", strength=entity))

        # Relationship: the memory is evidence for a relevant relationship.
        relationship = 0.0
        if candidate.relationship_ids:
            relationship = RELATIONSHIP_ONE_END
            signals.append(
                MatchSignal(name="relationship", strength=relationship)
            )

        # Existing metadata, normalised. Importance is stored 1-10.
        importance = _clamp((memory.importance_score - 1) / 9.0)
        confidence = _clamp(memory.confidence_score)
        recency = recency_score(memory.created_at, now)

        weights = self.weights
        relevance = (
            weights["text_relevance"] * text
            + weights["entity_relevance"] * entity
            + weights["relationship_relevance"] * relationship
        )
        metadata = (
            weights["importance"] * importance
            + weights["confidence"] * confidence
            + weights["recency"] * recency
        )
        # Metadata is gated by how relevant the memory is. Added at full
        # weight, importance (0.15) could overturn a partial-relevance gap: a
        # tea memory without the preference (text 0.74, importance 10) beat
        # "favourite tea is jasmine" (text 1.0, importance 2) on "what is my
        # favourite tea?". Scaled by the strongest relevance signal, metadata
        # still orders equally relevant memories -- importance keeps its
        # purpose -- but can no longer reverse a clear relevance difference.
        gate = max(text, entity, relationship)
        final = relevance + metadata * gate

        return (
            ScoreBreakdown(
                text_relevance=round(text, 6),
                entity_relevance=round(entity, 6),
                relationship_relevance=round(relationship, 6),
                importance=round(importance, 6),
                confidence=round(confidence, 6),
                recency=round(recency, 6),
                final_score=round(_clamp(final), 6),
            ),
            signals,
        )

    @staticmethod
    def _memory_sort_key(item: RetrievedMemory):
        """Documented tie-breaking, applied in order.

        1. final score       2. entity match strength
        3. importance        4. confidence
        5. recency           6. memory id (stable, so ordering is total)
        """
        score = item.score
        return (
            -score.final_score,
            -score.entity_relevance,
            -score.importance,
            -score.confidence,
            -score.recency,
            str(item.id),
        )

    # --- Relationships ------------------------------------------------------

    def rank_relationships(
        self,
        candidates: Sequence[RelationshipCandidate],
        evidence: Dict[uuid.UUID, Set[uuid.UUID]],
        keyword_matched_memories: Set[uuid.UUID],
    ) -> List[RetrievedRelationship]:
        """Score relationships by how much of them the query matched.

        Highest when both endpoints were matched, medium for one endpoint, and
        lowest when only a supporting memory matched the query text.
        """
        scored: List[RetrievedRelationship] = []

        for candidate in candidates:
            relationship = candidate.relationship
            signals: List[MatchSignal] = []

            if candidate.source_matched and candidate.target_matched:
                relevance = RELATIONSHIP_BOTH_ENDS
                signals.append(MatchSignal(name="both_endpoints", strength=relevance))
            elif candidate.source_matched or candidate.target_matched:
                relevance = RELATIONSHIP_ONE_END
                signals.append(MatchSignal(name="one_endpoint", strength=relevance))
            else:
                relevance = 0.0

            supporting = evidence.get(relationship.id, set())
            if supporting & keyword_matched_memories:
                relevance = max(relevance, RELATIONSHIP_EVIDENCE_ONLY)
                signals.append(
                    MatchSignal(
                        name="evidence_keyword", strength=RELATIONSHIP_EVIDENCE_ONLY
                    )
                )

            # Confidence only modulates an already-relevant relationship; it
            # never promotes an irrelevant one.
            score = _clamp(relevance * (0.8 + 0.2 * relationship.confidence_score))

            scored.append(
                RetrievedRelationship(
                    id=relationship.id,
                    source_name=candidate.source_name,
                    relationship_type=relationship.relationship_type.value,
                    target_name=candidate.target_name,
                    confidence_score=relationship.confidence_score,
                    score=round(score, 6),
                    signals=signals,
                )
            )

        scored.sort(
            key=lambda r: (-r.score, -r.confidence_score, r.source_name, str(r.id))
        )
        for position, item in enumerate(scored, start=1):
            item.rank = position
        return scored
