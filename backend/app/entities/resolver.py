"""Entity resolution: does this candidate already exist?

Deliberately conservative. The specification is explicit that a false merge is
worse than a duplicate, and that judgement drives every choice here: two
entities are only ever treated as one when a *deterministic* rule says so.

Resolution order:

1. Exact canonical name.
2. Normalized name  ("postgresql" == "PostgreSQL" == "PostgreSQL database").
3. Alias match      ("postgres" -> PostgreSQL).
4. Compact form     ("Postgre-SQL" -> "postgresql"), punctuation removed.

There is no fuzzy or similarity-based step. Every fuzzy rule considered would
also merge "Claude" with "Claude Code", which the specification names as a
pair that must stay separate. Nothing here calls a model.
"""

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.entities.models import Entity, EntityAlias, EntityStatus
from app.entities.normalizer import normalize_name

logger = get_logger(__name__)

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def compact_form(raw: str) -> str:
    """Normalized name with all punctuation and spacing removed.

    Lets "Postgre-SQL", "Postgre SQL" and "PostgreSQL" resolve together while
    keeping "claude" and "claudecode" distinct.
    """
    return _NON_ALPHANUMERIC.sub("", normalize_name(raw))


@dataclass(frozen=True)
class ResolutionResult:
    """The outcome of resolving one candidate name."""

    entity: Optional[Entity]
    reason: str  # "exact" | "normalized" | "alias" | "compact" | "new"

    @property
    def is_existing(self) -> bool:
        return self.entity is not None


class EntityResolver:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, name: str) -> ResolutionResult:
        """Find the entity this name refers to, if one already exists."""
        normalized = normalize_name(name)
        if not normalized:
            return ResolutionResult(entity=None, reason="new")

        # 1 & 2. Canonical and normalized name. The normalized column is
        # UNIQUE, so this is an indexed single-row lookup.
        statement = select(Entity).where(Entity.normalized_name == normalized)
        entity = (await self._session.execute(statement)).scalars().first()
        if entity is not None:
            reason = "exact" if entity.canonical_name == name else "normalized"
            return ResolutionResult(entity=entity, reason=reason)

        # 3. Alias.
        alias_statement = (
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(EntityAlias.normalized_alias == normalized)
        )
        entity = (await self._session.execute(alias_statement)).scalars().first()
        if entity is not None:
            return ResolutionResult(entity=entity, reason="alias")

        # 4. Compact form. Only consulted when the plain forms did not match,
        # and only against active entities.
        compact = compact_form(name)
        if compact and compact != normalized:
            candidates = (
                await self._session.execute(
                    select(Entity).where(Entity.status == EntityStatus.ACTIVE)
                )
            ).scalars().all()
            for existing in candidates:
                if compact_form(existing.canonical_name) == compact:
                    return ResolutionResult(entity=existing, reason="compact")

        return ResolutionResult(entity=None, reason="new")

    async def alias_conflict(self, normalized_alias: str) -> Optional[str]:
        """Why an alias cannot be created, or None if it is safe.

        An alias must identify exactly one entity. It is refused when it is
        already another entity's canonical name, or already registered as an
        alias -- either way it would make resolution ambiguous.
        """
        name_clash = (
            await self._session.execute(
                select(Entity.id).where(Entity.normalized_name == normalized_alias)
            )
        ).scalars().first()
        if name_clash is not None:
            return "matches an existing entity name"

        alias_clash = (
            await self._session.execute(
                select(EntityAlias.entity_id).where(
                    EntityAlias.normalized_alias == normalized_alias
                )
            )
        ).scalars().first()
        if alias_clash is not None:
            return "already registered as an alias"

        return None
