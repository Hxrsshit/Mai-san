"""Deterministic context budgeting.

Two stages, applied in order:

1. **Category limits** -- no single category may consume the whole context.
2. **Total budget** -- the final authority. Even items that passed their
   category limit are dropped if the whole exceeds `max_total_chars`.

Items are dropped whole. A memory either appears completely or not at all;
text is never cut mid-sentence, and a structured item is never half-rendered.

Dropping order is deliberate and documented: the *lowest-ranked* optional item
goes first, and the current message is never eligible. Stage 2D's ranking is
preserved throughout -- nothing here re-scores anything.

Size is measured in characters. All measurement goes through `Sizer`, so
token-based budgeting can replace it later without touching assembly logic.
"""

from dataclasses import dataclass
from typing import Callable, List, Protocol, Sequence, Tuple

from app.context.schemas import (
    ContextCharacterCounts,
    ContextEntity,
    ContextMemory,
    ContextRelationship,
    DroppedItem,
    RecentMessage,
)


class Sizer(Protocol):
    """Measures how much budget a piece of context consumes."""

    def __call__(self, text: str) -> int: ...


def character_sizer(text: str) -> int:
    """Stage 3A's default: one character, one unit.

    Deterministic and dependency-free. A token-aware sizer can be substituted
    without any other change, which is why every measurement routes through
    this one function.
    """
    return len(text or "")


@dataclass
class BudgetLimits:
    recent_message_limit: int
    max_memory_items: int
    max_entity_items: int
    max_relationship_items: int
    max_total_chars: int


@dataclass
class BudgetOutcome:
    """What survived, what was dropped, and what it all costs."""

    recent_conversation: List[RecentMessage]
    memories: List[ContextMemory]
    entities: List[ContextEntity]
    relationships: List[ContextRelationship]
    dropped: List[DroppedItem]
    counts: ContextCharacterCounts


def _memory_text(item: ContextMemory) -> str:
    return item.content


def _entity_text(item: ContextEntity) -> str:
    return item.render() + (f": {item.description}" if item.description else "")


def _relationship_text(item: ContextRelationship) -> str:
    return item.render()


def _message_text(item: RecentMessage) -> str:
    return f"{item.role}: {item.content}"


class ContextBudgeter:
    def __init__(
        self, limits: BudgetLimits, sizer: Callable[[str], int] = character_sizer
    ) -> None:
        self._limits = limits
        self._size = sizer

    def apply(
        self,
        current_message: str,
        recent_conversation: Sequence[RecentMessage],
        memories: Sequence[ContextMemory],
        entities: Sequence[ContextEntity],
        relationships: Sequence[ContextRelationship],
    ) -> BudgetOutcome:
        dropped: List[DroppedItem] = []

        # --- Stage 1: category limits -------------------------------------
        kept_messages, dropped_messages = self._apply_limit(
            recent_conversation, self._limits.recent_message_limit
        )
        # Conversation is trimmed from the *front*: the oldest turns go first,
        # keeping the most recent exchange intact.
        if dropped_messages:
            kept_messages = list(recent_conversation)[
                -self._limits.recent_message_limit :
            ]
            dropped_messages = list(recent_conversation)[
                : len(recent_conversation) - self._limits.recent_message_limit
            ]
        for message in dropped_messages:
            dropped.append(
                DroppedItem(
                    category="recent_message",
                    identifier=f"{message.role}:{message.content[:40]}",
                    reason="category_limit",
                )
            )

        kept_memories, dropped_memories = self._apply_limit(
            memories, self._limits.max_memory_items
        )
        dropped.extend(
            DroppedItem(
                category="memory", identifier=str(item.id),
                reason="category_limit", retrieval_rank=item.retrieval_rank,
            )
            for item in dropped_memories
        )

        kept_entities, dropped_entities = self._apply_limit(
            entities, self._limits.max_entity_items
        )
        dropped.extend(
            DroppedItem(
                category="entity", identifier=str(item.id),
                reason="category_limit", retrieval_rank=item.retrieval_rank,
            )
            for item in dropped_entities
        )

        kept_relationships, dropped_relationships = self._apply_limit(
            relationships, self._limits.max_relationship_items
        )
        dropped.extend(
            DroppedItem(
                category="relationship", identifier=str(item.id),
                reason="category_limit", retrieval_rank=item.retrieval_rank,
            )
            for item in dropped_relationships
        )

        # --- Stage 2: total budget ----------------------------------------
        # The current message is reserved first and never dropped, then recent
        # conversation, then long-term knowledge lowest-ranked-first.
        message_chars = self._size(current_message)
        available = self._limits.max_total_chars - message_chars

        kept_messages, kept_memories, kept_entities, kept_relationships = (
            self._enforce_total(
                available,
                kept_messages,
                kept_memories,
                kept_entities,
                kept_relationships,
                dropped,
            )
        )

        counts = ContextCharacterCounts(
            current_message=message_chars,
            recent_conversation=self._total(kept_messages, _message_text),
            memories=self._total(kept_memories, _memory_text),
            entities=self._total(kept_entities, _entity_text),
            relationships=self._total(kept_relationships, _relationship_text),
        )
        counts.total = (
            counts.current_message
            + counts.recent_conversation
            + counts.memories
            + counts.entities
            + counts.relationships
        )

        return BudgetOutcome(
            recent_conversation=kept_messages,
            memories=kept_memories,
            entities=kept_entities,
            relationships=kept_relationships,
            dropped=dropped,
            counts=counts,
        )

    def _enforce_total(
        self,
        available: int,
        messages: List[RecentMessage],
        memories: List[ContextMemory],
        entities: List[ContextEntity],
        relationships: List[ContextRelationship],
        dropped: List[DroppedItem],
    ):
        """Drop whole items until everything fits.

        Order of sacrifice, lowest value first:
          1. lowest-ranked relationships
          2. lowest-ranked entities
          3. lowest-ranked memories
          4. oldest recent messages

        Long-term knowledge yields before short-term conversation, because the
        current exchange is what the user is actually engaged in.
        """
        def used() -> int:
            return (
                self._total(messages, _message_text)
                + self._total(memories, _memory_text)
                + self._total(entities, _entity_text)
                + self._total(relationships, _relationship_text)
            )

        while used() > available:
            if relationships:
                item = relationships.pop()
                dropped.append(DroppedItem(
                    category="relationship", identifier=str(item.id),
                    reason="total_budget", retrieval_rank=item.retrieval_rank))
            elif entities:
                item = entities.pop()
                dropped.append(DroppedItem(
                    category="entity", identifier=str(item.id),
                    reason="total_budget", retrieval_rank=item.retrieval_rank))
            elif memories:
                item = memories.pop()
                dropped.append(DroppedItem(
                    category="memory", identifier=str(item.id),
                    reason="total_budget", retrieval_rank=item.retrieval_rank))
            elif messages:
                message = messages.pop(0)  # oldest first
                dropped.append(DroppedItem(
                    category="recent_message",
                    identifier=f"{message.role}:{message.content[:40]}",
                    reason="total_budget"))
            else:
                # Only the current message remains. It is never dropped, even
                # if it alone exceeds the budget -- the user's input is the
                # one thing that must always be present.
                break

        return messages, memories, entities, relationships

    @staticmethod
    def _apply_limit(items: Sequence, limit: int) -> Tuple[List, List]:
        """Keep the first `limit` items; ranking order is already correct."""
        listed = list(items)
        if limit < 0:
            limit = 0
        return listed[:limit], listed[limit:]

    def _total(self, items: Sequence, extract: Callable) -> int:
        return sum(self._size(extract(item)) for item in items)
