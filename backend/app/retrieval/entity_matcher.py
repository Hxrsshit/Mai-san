"""Deterministic entity matching against the query.

Entity matches are the strongest retrieval signal, so this stays conservative:
matching is by **exact lookup** of query n-grams against the normalized name
and alias columns. Both are UNIQUE-indexed, so each lookup is an indexed
`IN` query -- no scans, no N+1, and no fuzzy matching.

The specification is explicit that false positives are worse than missed
matches. Substring and similarity matching are therefore not used: "AI" must
not silently match "AI Product Development", and "Claude" must not match
"Claude Code".
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.core.logging import get_logger
from app.entities.models import Entity, EntityAlias, EntityStatus
from app.retrieval.query_normalizer import NormalizedQuery

logger = get_logger(__name__)

#: How strong each kind of match is, on a 0-1 scale. A canonical-name hit is
#: the strongest evidence that the user meant this entity; an alias is nearly
#: as strong; a normalized-form hit slightly less.
MATCH_STRENGTH = {
    "canonical": 1.0,
    "alias": 0.9,
    "normalized": 0.8,
}


@dataclass(frozen=True)
class EntityMatch:
    entity: Entity
    strength: float
    matched_via: str
    matched_text: str


class EntityMatcher:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def match(self, query: NormalizedQuery) -> List[EntityMatch]:
        """Return the entities this query refers to, strongest first.

        Two indexed queries total, regardless of how many phrases the query
        produced.
        """
        phrases = list(query.phrases)
        if not phrases:
            return []

        matches: Dict[str, EntityMatch] = {}

        for match in await self._match_by_name(phrases):
            self._keep_strongest(matches, match)
        for match in await self._match_by_alias(phrases):
            self._keep_strongest(matches, match)

        ranked = sorted(
            matches.values(),
            # Strongest first; then longer matched text, since a longer phrase
            # is a more specific reference; then name for stable ordering.
            key=lambda m: (-m.strength, -len(m.matched_text), m.entity.canonical_name),
        )
        if ranked:
            logger.info(
                "Entity matches found",
                extra={"matched": len(ranked), "phrases_tried": len(phrases)},
            )
        return ranked

    async def _match_by_name(self, phrases: Sequence[str]) -> List[EntityMatch]:
        statement = (
            select(Entity)
            .where(
                Entity.normalized_name.in_(phrases),
                Entity.status == EntityStatus.ACTIVE,
            )
            # Entity.aliases is lazy="selectin"; retrieval never reads it, and
            # loading it costs an extra query per lookup.
            .options(noload(Entity.aliases))
        )
        rows = (await self._session.execute(statement)).scalars().all()

        results: List[EntityMatch] = []
        for entity in rows:
            # A phrase equal to the entity's own display form is the strongest
            # possible signal; a match on the normalized form is slightly less.
            exact_canonical = entity.canonical_name.lower() == entity.normalized_name
            via = "canonical" if exact_canonical else "normalized"
            results.append(
                EntityMatch(
                    entity=entity,
                    strength=MATCH_STRENGTH[via],
                    matched_via=via,
                    matched_text=entity.normalized_name,
                )
            )
        return results

    async def _match_by_alias(self, phrases: Sequence[str]) -> List[EntityMatch]:
        statement = (
            select(Entity, EntityAlias.normalized_alias)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(
                EntityAlias.normalized_alias.in_(phrases),
                Entity.status == EntityStatus.ACTIVE,
            )
            .options(noload(Entity.aliases))
        )
        rows = (await self._session.execute(statement)).all()
        return [
            EntityMatch(
                entity=entity,
                strength=MATCH_STRENGTH["alias"],
                matched_via="alias",
                matched_text=alias,
            )
            for entity, alias in rows
        ]

    @staticmethod
    def _keep_strongest(matches: Dict[str, EntityMatch], match: EntityMatch) -> None:
        """One entity, one match -- the strongest signal wins."""
        key = str(match.entity.id)
        existing = matches.get(key)
        if existing is None or match.strength > existing.strength:
            matches[key] = match
