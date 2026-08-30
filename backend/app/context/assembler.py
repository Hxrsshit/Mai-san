"""Context assembly.

Pure transformation: takes the current message, recent conversation and Stage
2D's ranked `RetrievalResult`, and produces a bounded `ContextPackage`.

Nothing here queries the database, re-scores anything, or calls a model.
Stage 2D's ranking order is carried through untouched -- the only reordering
that ever happens is dropping items the budget cannot fit.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from app.core.logging import get_logger
from app.context.budget import BudgetLimits, ContextBudgeter
from app.context.schemas import (
    ContextBudget,
    ContextEntity,
    ContextMemory,
    ContextMetadata,
    ContextPackage,
    ContextRelationship,
    RecentMessage,
)
from app.retrieval.schemas import RetrievalResult

logger = get_logger(__name__)


def to_recent_messages(messages: Sequence) -> List[RecentMessage]:
    """Narrow stored `Message` rows to the package's compact shape.

    Original chronological order is preserved exactly. Exposed at module level
    because the chat service's failure path needs the same conversion when
    assembly never completed -- one implementation, not two.
    """
    converted: List[RecentMessage] = []
    for message in messages:
        role = getattr(message, "role", None)
        converted.append(
            RecentMessage(
                role=getattr(role, "value", role),
                content=message.content,
                created_at=getattr(message, "created_at", None),
            )
        )
    return converted


class ContextAssembler:
    """Combines the three context sources into one structured package."""

    def __init__(self, limits: BudgetLimits, budgeter: Optional[ContextBudgeter] = None) -> None:
        self._limits = limits
        self._budgeter = budgeter or ContextBudgeter(limits)

    def assemble(
        self,
        current_message: str,
        recent_messages: Sequence = (),
        retrieval: Optional[RetrievalResult] = None,
        conversation_id: Optional[uuid.UUID] = None,
        degraded_sources: Optional[List[str]] = None,
        duration_ms: float = 0.0,
    ) -> ContextPackage:
        """Build the package.

        `current_message` is preserved exactly -- never normalized, rewritten
        or truncated. Every optional source may be absent.
        """
        conversation = self._convert_messages(recent_messages)
        memories = self._convert_memories(retrieval)
        entities = self._convert_entities(retrieval)
        relationships = self._convert_relationships(retrieval, entities)

        outcome = self._budgeter.apply(
            current_message=current_message,
            recent_conversation=conversation,
            memories=memories,
            entities=entities,
            relationships=relationships,
        )

        metadata = ContextMetadata(
            assembled_at=datetime.now(timezone.utc),
            conversation_id=conversation_id,
            recent_message_count=len(outcome.recent_conversation),
            memory_count=len(outcome.memories),
            entity_count=len(outcome.entities),
            relationship_count=len(outcome.relationships),
            characters=outcome.counts,
            budget=ContextBudget(
                recent_message_limit=self._limits.recent_message_limit,
                max_memory_items=self._limits.max_memory_items,
                max_entity_items=self._limits.max_entity_items,
                max_relationship_items=self._limits.max_relationship_items,
                max_total_chars=self._limits.max_total_chars,
            ),
            dropped_items=outcome.dropped,
            degraded_sources=list(degraded_sources or []),
            duration_ms=duration_ms,
        )

        if outcome.dropped:
            logger.info(
                "Context budget applied",
                extra={
                    "dropped": len(outcome.dropped),
                    "total_chars": outcome.counts.total,
                    "budget_chars": self._limits.max_total_chars,
                },
            )

        return ContextPackage(
            current_message=current_message,
            recent_conversation=outcome.recent_conversation,
            memories=outcome.memories,
            entities=outcome.entities,
            relationships=outcome.relationships,
            metadata=metadata,
        )

    # --- Conversion ---------------------------------------------------------
    # Database rows and Stage 2D types are narrowed to the compact shapes the
    # package exposes. Nothing gains a field it does not need.

    @staticmethod
    def _convert_messages(messages: Sequence) -> List[RecentMessage]:
        """Preserve original chronological order exactly."""
        return to_recent_messages(messages)

    @staticmethod
    def _convert_memories(retrieval: Optional[RetrievalResult]) -> List[ContextMemory]:
        if retrieval is None:
            return []
        return [
            ContextMemory(
                id=memory.id,
                content=memory.content,
                memory_type=memory.memory_type,
                importance_score=memory.importance_score,
                confidence_score=memory.confidence_score,
                created_at=memory.created_at,
                # Stage 2D's rank and score, carried through unchanged.
                retrieval_rank=memory.rank,
                retrieval_score=memory.score.final_score,
            )
            for memory in retrieval.memories
        ]

    @staticmethod
    def _convert_entities(retrieval: Optional[RetrievalResult]) -> List[ContextEntity]:
        if retrieval is None:
            return []
        return [
            ContextEntity(
                id=entity.id,
                name=entity.canonical_name,
                entity_type=entity.entity_type,
                description=entity.description,
                retrieval_rank=entity.rank,
                match_strength=entity.match_strength,
                directly_matched=True,
            )
            for entity in retrieval.matched_entities
        ]

    @staticmethod
    def _convert_relationships(
        retrieval: Optional[RetrievalResult], entities: Sequence[ContextEntity]
    ) -> List[ContextRelationship]:
        if retrieval is None:
            return []

        matched_names = {entity.name for entity in entities}
        return [
            ContextRelationship(
                id=relationship.id,
                source_name=relationship.source_name,
                relationship_type=relationship.relationship_type,
                target_name=relationship.target_name,
                confidence_score=relationship.confidence_score,
                retrieval_rank=relationship.rank,
                # Flagged, not re-ranked: Stage 3B may prefer these when
                # rendering, but the order stays Stage 2D's.
                connects_matched_entities=(
                    relationship.source_name in matched_names
                    and relationship.target_name in matched_names
                ),
            )
            for relationship in retrieval.relationships
        ]
