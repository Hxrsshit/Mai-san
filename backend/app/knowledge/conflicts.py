"""Deterministic conflict detection.

Reads the database, decides nothing about writes. Every rule is stated in
`policies.py`; this module applies them to real rows and returns
`ConflictOutcome` values. Applying those outcomes is `lifecycle.py`'s job, and
the separation is deliberate: detection can be tested exhaustively without any
write path, and a detection bug can never half-mutate the knowledge base.

**No model call.** Nothing here imports `app.llm`. Conflict detection is
lexical and structural: entity resolution, relationship shape, timestamps and
the replacement patterns in `policies.py`.

The three rules, in the order they run:

1. **Explicit replacement** -- the new memory names both sides ("switched from
   OpenRouter to Groq"). Supersedes.
2. **Explicit abandonment** -- the new memory names only what stopped ("no
   longer uses OpenRouter"). Supersedes, with no successor recorded.
3. **Exclusive relationship** -- a relationship type where two simultaneous
   targets are incoherent gained a second one. Supersedes only if the new
   memory states the present; otherwise records an unresolved conflict and
   changes nothing.

Anything else produces no conflict, which is the common and correct outcome.
"""

import uuid
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.entities.models import Entity, EntityAlias, EntityStatus, MemoryEntity
from app.knowledge import policies
from app.knowledge.models import ConflictReason, ConflictResolution
from app.knowledge.schemas import ConflictOutcome
from app.memory.models import Memory, MemoryStatus
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
)

logger = get_logger(__name__)

#: Upper bound on rows any single detection query may consider, so cost stays
#: predictable as the knowledge base grows. Mirrors Stage 2D's pool discipline.
MAX_CANDIDATES = 100


class ConflictDetector:
    """Finds conflicts caused by one newly stored memory."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def detect(self, memory: Memory) -> List[ConflictOutcome]:
        """Every conflict the arrival of `memory` creates.

        Returns an empty list far more often than not. The detector abstains
        whenever the evidence is not explicit, because a false conflict hides
        true knowledge and a missed one merely leaves it un-ranked.
        """
        text = memory.normalized_content or ""
        outcomes: List[ConflictOutcome] = []

        # Progress reports read like replacements to a keyword matcher --
        # "moved to the next phase" is not a migration. When the text is
        # clearly narrating progress, only the structural rule is trusted.
        narrating_progress = policies.looks_like_temporal_progress(text)

        if not narrating_progress:
            outcomes.extend(await self._detect_replacements(memory, text))
            outcomes.extend(await self._detect_abandonments(memory, text))

        outcomes.extend(await self._detect_exclusive_conflicts(memory, text))
        return _deduplicate(outcomes)

    # --- Rule 1: explicit replacement --------------------------------------

    async def _detect_replacements(
        self, memory: Memory, text: str
    ) -> List[ConflictOutcome]:
        """"switched from X to Y" -- X's knowledge becomes historical."""
        outcomes: List[ConflictOutcome] = []

        for replacement in policies.find_replacements(text):
            old_entity = await self._resolve(replacement.old)
            if old_entity is None:
                # Nothing resolvable was named. Abstaining is correct: acting
                # on an unresolved fragment would supersede by coincidence.
                continue
            new_entity = await self._resolve(replacement.new)

            outcomes.extend(
                await self._retire_relationships_targeting(
                    old_entity=old_entity,
                    new_entity=new_entity,
                    trigger=memory,
                    reason=ConflictReason.EXPLICIT_REPLACEMENT,
                )
            )
            outcomes.extend(
                await self._retire_memories_mentioning(
                    old_entity=old_entity,
                    new_entity=new_entity,
                    trigger=memory,
                    reason=ConflictReason.EXPLICIT_REPLACEMENT,
                )
            )
        return outcomes

    # --- Rule 2: explicit abandonment --------------------------------------

    async def _detect_abandonments(
        self, memory: Memory, text: str
    ) -> List[ConflictOutcome]:
        """"no longer uses X" -- X's knowledge is retired with no successor."""
        outcomes: List[ConflictOutcome] = []

        for fragment in policies.find_abandonments(text):
            old_entity = await self._resolve(fragment)
            if old_entity is None:
                continue

            outcomes.extend(
                await self._retire_relationships_targeting(
                    old_entity=old_entity,
                    new_entity=None,
                    trigger=memory,
                    reason=ConflictReason.EXPLICIT_ABANDONMENT,
                )
            )
            outcomes.extend(
                await self._retire_memories_mentioning(
                    old_entity=old_entity,
                    new_entity=None,
                    trigger=memory,
                    reason=ConflictReason.EXPLICIT_ABANDONMENT,
                )
            )
        return outcomes

    # --- Rule 3: exclusive relationship types ------------------------------

    async def _detect_exclusive_conflicts(
        self, memory: Memory, text: str
    ) -> List[ConflictOutcome]:
        """A second target on a relationship type that expects only one.

        Only `policies.EXCLUSIVE_RELATIONSHIP_TYPES` are considered. For every
        other type -- USES included -- two targets coexist happily, and
        treating that as a conflict is the specific false positive this design
        exists to avoid.
        """
        new_relationships = await self._relationships_from(memory)
        if not new_relationships:
            return []

        states_present = policies.states_the_present(text)
        outcomes: List[ConflictOutcome] = []

        for new in new_relationships:
            if not policies.is_exclusive(new.relationship_type):
                continue

            others = await self._sibling_relationships(new)
            for other in others:
                if states_present:
                    # "now prefers hybrid" names the present explicitly.
                    outcomes.append(
                        ConflictOutcome(
                            resolution=ConflictResolution.SUPERSEDED,
                            reason=ConflictReason.EXCLUSIVE_REPLACEMENT,
                            older_relationship_id=other.id,
                            newer_relationship_id=new.id,
                        )
                    )
                else:
                    # Two claims, nothing to separate them. Recording the
                    # uncertainty is the whole answer: both stay ACTIVE.
                    outcomes.append(
                        ConflictOutcome(
                            resolution=ConflictResolution.UNRESOLVED,
                            reason=ConflictReason.EXCLUSIVE_AMBIGUOUS,
                            older_relationship_id=other.id,
                            newer_relationship_id=new.id,
                        )
                    )
        return outcomes

    # --- Shared retirement helpers -----------------------------------------

    async def _retire_relationships_targeting(
        self,
        old_entity: Entity,
        new_entity: Optional[Entity],
        trigger: Memory,
        reason: ConflictReason,
    ) -> List[ConflictOutcome]:
        """Active relationships pointing at the replaced entity."""
        statement = (
            select(Relationship)
            .where(
                Relationship.status == RelationshipStatus.ACTIVE,
                Relationship.target_entity_id == old_entity.id,
            )
            .limit(MAX_CANDIDATES)
        )
        old_relationships = (
            (await self._session.execute(statement)).scalars().unique().all()
        )
        if not old_relationships:
            return []

        successors = await self._successor_map(new_entity)

        outcomes: List[ConflictOutcome] = []
        for old in old_relationships:
            successor = successors.get((old.source_entity_id, old.relationship_type))
            outcomes.append(
                ConflictOutcome(
                    resolution=ConflictResolution.SUPERSEDED,
                    reason=reason,
                    older_relationship_id=old.id,
                    # Present only when a like-for-like successor exists:
                    # same subject, same relationship type, new target.
                    newer_relationship_id=successor,
                )
            )
        return outcomes

    async def _retire_memories_mentioning(
        self,
        old_entity: Entity,
        new_entity: Optional[Entity],
        trigger: Memory,
        reason: ConflictReason,
    ) -> List[ConflictOutcome]:
        """Older active memories about the replaced entity.

        Two guards keep this from over-reaching:

        - only memories older than the trigger are eligible, so a statement
          cannot retire something written after it;
        - a memory that also mentions the *new* entity is left alone, because
          it is already describing the change rather than the old state.
        """
        old_name = old_entity.normalized_name
        if not old_name:
            return []

        # Text match on every surface form the entity is known by, not just
        # the canonical one. A memory saying "Mai uses postgres" is about
        # PostgreSQL, and matching only "postgresql" would leave it active
        # after an explicit migration away from it.
        old_forms = await self._surface_forms(old_entity)
        text_match = or_(
            *[Memory.normalized_content.like(f"%{form}%") for form in old_forms]
        )

        # The structured link is more reliable than text when it exists, so
        # both routes are used and the results unioned.
        linked = select(MemoryEntity.memory_id).where(
            MemoryEntity.entity_id == old_entity.id
        )

        statement = (
            select(Memory)
            .where(
                Memory.status == MemoryStatus.ACTIVE,
                Memory.id != trigger.id,
                Memory.created_at <= trigger.created_at,
                or_(text_match, Memory.id.in_(linked)),
            )
            .order_by(Memory.created_at.desc())
            .limit(MAX_CANDIDATES)
        )
        candidates = (await self._session.execute(statement)).scalars().all()

        new_forms = (
            await self._surface_forms(new_entity) if new_entity is not None else []
        )

        outcomes: List[ConflictOutcome] = []
        for candidate in candidates:
            content = candidate.normalized_content or ""
            if any(form in content for form in new_forms):
                # Already describing the change rather than the old state.
                continue
            outcomes.append(
                ConflictOutcome(
                    resolution=ConflictResolution.SUPERSEDED,
                    reason=reason,
                    older_memory_id=candidate.id,
                    newer_memory_id=trigger.id,
                )
            )
        return outcomes

    # --- Lookups ------------------------------------------------------------

    async def _resolve(self, fragment: str) -> Optional[Entity]:
        """Resolve a captured text fragment to one entity.

        Progressively shorter prefixes are tried longest-first, so
        "openrouter for inference" still finds "openrouter". Exact normalised
        name first, then alias -- the same order Stage 2B's resolver uses, and
        with no fuzzy step, which would merge distinct entities.
        """
        for name in policies.candidate_names(fragment):
            statement = select(Entity).where(
                Entity.normalized_name == name,
                Entity.status == EntityStatus.ACTIVE,
            )
            entity = (await self._session.execute(statement)).scalars().first()
            if entity is not None:
                return entity

            statement = (
                select(Entity)
                .join(EntityAlias, EntityAlias.entity_id == Entity.id)
                .where(
                    EntityAlias.normalized_alias == name,
                    Entity.status == EntityStatus.ACTIVE,
                )
            )
            entity = (await self._session.execute(statement)).scalars().first()
            if entity is not None:
                return entity
        return None

    async def _surface_forms(self, entity: Entity) -> List[str]:
        """Every normalised name this entity is known by, longest first.

        Longest first so a containment check prefers the most specific form.
        """
        statement = select(EntityAlias.normalized_alias).where(
            EntityAlias.entity_id == entity.id
        )
        aliases = (await self._session.execute(statement)).scalars().all()
        forms = {entity.normalized_name, *aliases}
        return sorted((form for form in forms if form), key=len, reverse=True)

    async def _relationships_from(self, memory: Memory) -> List[Relationship]:
        """Active relationships this memory is evidence for."""
        statement = (
            select(Relationship)
            .join(
                RelationshipEvidence,
                RelationshipEvidence.relationship_id == Relationship.id,
            )
            .where(
                RelationshipEvidence.memory_id == memory.id,
                Relationship.status == RelationshipStatus.ACTIVE,
            )
            .limit(MAX_CANDIDATES)
        )
        return list((await self._session.execute(statement)).scalars().unique().all())

    async def _sibling_relationships(
        self, relationship: Relationship
    ) -> List[Relationship]:
        """Other active relationships with the same subject and type."""
        statement = (
            select(Relationship)
            .where(
                Relationship.status == RelationshipStatus.ACTIVE,
                Relationship.source_entity_id == relationship.source_entity_id,
                Relationship.relationship_type == relationship.relationship_type,
                Relationship.target_entity_id != relationship.target_entity_id,
                Relationship.id != relationship.id,
            )
            .limit(MAX_CANDIDATES)
        )
        return list((await self._session.execute(statement)).scalars().unique().all())

    async def _successor_map(
        self, new_entity: Optional[Entity]
    ) -> Dict[Tuple[uuid.UUID, object], uuid.UUID]:
        """(subject, type) -> the active relationship pointing at the new target.

        Used to name a successor on a supersession link where one genuinely
        exists. When the new side did not resolve, or no like-for-like
        relationship was extracted, the link records the retirement without a
        successor rather than inventing one.
        """
        if new_entity is None:
            return {}

        statement = (
            select(Relationship)
            .where(
                Relationship.status == RelationshipStatus.ACTIVE,
                Relationship.target_entity_id == new_entity.id,
            )
            .limit(MAX_CANDIDATES)
        )
        rows = (await self._session.execute(statement)).scalars().unique().all()
        return {
            (row.source_entity_id, row.relationship_type): row.id for row in rows
        }


def _deduplicate(outcomes: Sequence[ConflictOutcome]) -> List[ConflictOutcome]:
    """One outcome per (older item, kind).

    Several rules can reach the same conclusion about the same row. A
    supersession always wins over an unresolved marking for the same item:
    once something deterministic named a replacement, the uncertainty is gone.
    """
    best: Dict[Tuple[str, uuid.UUID], ConflictOutcome] = {}
    order: List[Tuple[str, uuid.UUID]] = []

    for outcome in outcomes:
        if outcome.older_memory_id is not None:
            key = ("memory", outcome.older_memory_id)
        elif outcome.older_relationship_id is not None:
            key = ("relationship", outcome.older_relationship_id)
        else:  # pragma: no cover - an outcome always names an older item
            continue

        existing = best.get(key)
        if existing is None:
            best[key] = outcome
            order.append(key)
            continue

        if outcome.supersedes and not existing.supersedes:
            best[key] = outcome
        elif (
            outcome.supersedes
            and existing.supersedes
            and existing.newer_relationship_id is None
            and outcome.newer_relationship_id is not None
        ):
            # Prefer the version that can name its successor.
            best[key] = outcome

    return [best[key] for key in order]


__all__ = ["ConflictDetector", "MAX_CANDIDATES"]
