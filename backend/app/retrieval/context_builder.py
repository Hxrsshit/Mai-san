"""Deduplication, budget enforcement and context assembly.

Rank first, then select. Items are taken in score order and dropped when a
budget is reached, so the highest-value knowledge always survives -- never the
first N rows the database happened to return.

Deduplication affects only the assembled context. Nothing is deleted.
"""

from typing import List, Sequence, Set, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.memory.deduplication import normalize as normalize_text
from app.retrieval.schemas import (
    ContextPackage,
    RetrievedEntity,
    RetrievedMemory,
    RetrievedRelationship,
)

logger = get_logger(__name__)

CONTEXT_HEADER = "PERSONAL KNOWLEDGE CONTEXT"

#: Appended to every assembled context. Retrieved knowledge is background: the
#: user's current message must always win over something remembered earlier.
CONTEXT_FOOTER = (
    "This is background knowledge from earlier conversations. Use it only when "
    "it is relevant to the current request. If it conflicts with what the user "
    "is saying now, the user is right -- treat this as possibly out of date. "
    "Never present it as something the user just said, and never claim "
    "information that is not here or in the conversation."
)


def deduplicate_memories(
    memories: Sequence[RetrievedMemory],
) -> Tuple[List[RetrievedMemory], int]:
    """Drop memories whose normalised text repeats one already kept.

    Ranked order is preserved, so the higher-scoring phrasing is the one that
    survives.
    """
    seen: Set[str] = set()
    kept: List[RetrievedMemory] = []
    dropped = 0

    for memory in memories:
        key = normalize_text(memory.content)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(memory)
    return kept, dropped


def deduplicate_relationships(
    relationships: Sequence[RetrievedRelationship],
) -> Tuple[List[RetrievedRelationship], int]:
    """One line per distinct claim, regardless of how many rows produced it."""
    seen: Set[str] = set()
    kept: List[RetrievedRelationship] = []
    dropped = 0

    for relationship in relationships:
        key = relationship.render().lower()
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(relationship)
    return kept, dropped


def deduplicate_entities(
    entities: Sequence[RetrievedEntity],
) -> Tuple[List[RetrievedEntity], int]:
    seen: Set[str] = set()
    kept: List[RetrievedEntity] = []
    dropped = 0

    for entity in entities:
        key = str(entity.id)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(entity)
    return kept, dropped


class ContextBuilder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(
        self,
        query: str,
        entities: Sequence[RetrievedEntity],
        memories: Sequence[RetrievedMemory],
        relationships: Sequence[RetrievedRelationship],
        package: ContextPackage,
    ) -> ContextPackage:
        """Deduplicate, apply the budget, and record what happened."""
        entities, entity_dupes = deduplicate_entities(entities)
        memories, memory_dupes = deduplicate_memories(memories)
        relationships, relationship_dupes = deduplicate_relationships(relationships)

        if memory_dupes or relationship_dupes or entity_dupes:
            logger.info(
                "Retrieval deduplication",
                extra={
                    "memories_dropped": memory_dupes,
                    "relationships_dropped": relationship_dupes,
                    "entities_dropped": entity_dupes,
                },
            )

        selected_memories = list(memories[: self._settings.RETRIEVAL_MAX_MEMORIES])
        selected_entities = list(entities[: self._settings.RETRIEVAL_MAX_ENTITIES])
        selected_relationships = list(
            relationships[: self._settings.RETRIEVAL_MAX_RELATIONSHIPS]
        )

        # Character budget, applied after the count budgets. Items are removed
        # from the end -- lowest ranked first -- until the rendered context
        # fits. A partially rendered section is never emitted.
        budget = self._settings.RETRIEVAL_MAX_CONTEXT_CHARS
        exhausted = False
        rendered = self.render(
            selected_entities, selected_memories, selected_relationships
        )
        while len(rendered) > budget and (
            selected_memories or selected_relationships or selected_entities
        ):
            exhausted = True
            if selected_memories:
                selected_memories.pop()
            elif selected_relationships:
                selected_relationships.pop()
            else:
                selected_entities.pop()
            rendered = self.render(
                selected_entities, selected_memories, selected_relationships
            )

        if exhausted:
            logger.info(
                "Retrieval context budget reached",
                extra={"budget_chars": budget, "context_chars": len(rendered)},
            )

        package.query = query
        package.matched_entities = selected_entities
        package.memories = selected_memories
        package.relationships = selected_relationships
        package.metadata.selected_memories = len(selected_memories)
        package.metadata.selected_entities = len(selected_entities)
        package.metadata.selected_relationships = len(selected_relationships)
        package.metadata.context_chars = len(rendered)
        package.metadata.budget_exhausted = exhausted
        return package

    def render(
        self,
        entities: Sequence[RetrievedEntity],
        memories: Sequence[RetrievedMemory],
        relationships: Sequence[RetrievedRelationship],
    ) -> str:
        """Render the package as compact prose.

        **No longer part of any prompt.** Stage 3B made
        `app.prompt.formatter.render_reference_block` the single owner of
        knowledge-to-prompt rendering; what remains here measures the Stage 2D
        character budget and answers the retrieval debug endpoints.

        Database rows are never dumped: only the fields a reader needs. Scores
        and signals stay out -- they were never useful to a model, and are not
        useful to a human reading the debug output either.
        """
        if not (entities or memories or relationships):
            return ""

        sections: List[str] = [CONTEXT_HEADER]

        if memories:
            sections.append("\nRelevant memories:")
            sections.extend(f"- {memory.content}" for memory in memories)

        if entities:
            sections.append("\nRelevant entities:")
            sections.extend(
                f"- {entity.canonical_name} ({entity.entity_type})"
                + (f": {entity.description}" if entity.description else "")
                for entity in entities
            )

        if relationships:
            sections.append("\nKnown connections:")
            sections.extend(f"- {r.render()}" for r in relationships)

        sections.append(f"\n{CONTEXT_FOOTER}")
        return "\n".join(sections)
