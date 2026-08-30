"""Entity persistence and the extraction pipeline.

Pipeline, per stored memory:

    memory -> extract candidates -> confidence filter
           -> normalize -> resolve (reuse or create)
           -> aliases -> memory link

Only this module writes entities. The model proposes; the application decides.

Each candidate is written inside its own savepoint, so an entity, its aliases
and its memory link either all land or none do -- a failure on one candidate
cannot leave a half-written entity behind or abort the others.
"""

import uuid
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.entities.extractor import EntityExtractor
from app.entities.models import (
    Entity,
    EntityAlias,
    EntityStatus,
    MemoryEntity,
)
from app.entities.normalizer import clean_display_name, normalize_name
from app.entities.resolver import EntityResolver
from app.entities.schemas import EntityCandidate
from app.llm.base import LLMProvider
from app.memory.models import Memory

logger = get_logger(__name__)

# Driver connection failures surface as bare OSErrors before SQLAlchemy wraps
# them, so both count as database errors.
_DB_ERRORS = (SQLAlchemyError, OSError)


class EntityNotFoundError(NotFoundError):
    code = "entity_not_found"
    message = "Entity not found."


class EntityService:
    def __init__(
        self,
        session: AsyncSession,
        provider: Optional[LLMProvider] = None,
        settings: Optional[Settings] = None,
        extractor: Optional[EntityExtractor] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._provider = provider
        self._extractor = extractor or (
            EntityExtractor(provider, self._settings) if provider else None
        )
        self._resolver = EntityResolver(session)

    # --- Extraction pipeline ------------------------------------------------

    async def extract_for_memory(self, memory: Memory) -> List[Entity]:
        """Extract, resolve and link entities for one stored memory.

        Never raises. The memory is already committed by the time this runs.
        """
        if not self._settings.ENTITY_EXTRACTION_ENABLED:
            return []
        if self._extractor is None:
            logger.warning("Entity extraction skipped: no provider configured")
            return []

        logger.info(
            "Entity extraction started", extra={"memory_id": str(memory.id)}
        )

        try:
            candidates = await self._extractor.extract(
                memory_content=memory.content,
                memory_type=memory.memory_type.value,
            )
        except Exception as exc:  # noqa: BLE001 - belt and braces
            logger.error(
                "Entity extraction raised unexpectedly",
                extra={"memory_id": str(memory.id), "error": str(exc)},
                exc_info=exc,
            )
            return []

        accepted = self._filter_by_confidence(candidates)

        linked: List[Entity] = []
        created = reused = 0
        for candidate in accepted:
            try:
                entity, was_created = await self._store_candidate(candidate, memory)
            except Exception as exc:  # noqa: BLE001 - one bad candidate only
                logger.error(
                    "Failed to store entity candidate",
                    extra={"memory_id": str(memory.id), "error": str(exc)},
                )
                continue
            if entity is None:
                continue
            linked.append(entity)
            created += int(was_created)
            reused += int(not was_created)

        logger.info(
            "Entity extraction completed",
            extra={
                "memory_id": str(memory.id),
                "candidates": len(candidates),
                "below_confidence": len(candidates) - len(accepted),
                # NOT "created"/"reused" alone: `created` is a reserved
                # LogRecord attribute and passing it in `extra` raises,
                # which would abort this task after the writes but before
                # the commit -- silently discarding every extracted entity.
                "entities_created": created,
                "entities_reused": reused,
                "linked": len(linked),
            },
        )
        return linked

    def _filter_by_confidence(
        self, candidates: Sequence[EntityCandidate]
    ) -> List[EntityCandidate]:
        kept: List[EntityCandidate] = []
        for candidate in candidates:
            if candidate.confidence_score < self._settings.ENTITY_MIN_CONFIDENCE:
                logger.info(
                    "Entity rejected: confidence below threshold",
                    extra={
                        "confidence": candidate.confidence_score,
                        "threshold": self._settings.ENTITY_MIN_CONFIDENCE,
                        "entity_type": candidate.entity_type.value,
                    },
                )
                continue
            kept.append(candidate)
        return kept

    async def _store_candidate(
        self, candidate: EntityCandidate, memory: Memory
    ) -> Tuple[Optional[Entity], bool]:
        """Resolve or create one entity, add its aliases, and link the memory.

        Wrapped in a savepoint so the three writes are atomic together.
        """
        async with self._session.begin_nested():
            resolution = await self._resolver.resolve(candidate.name)

            if resolution.is_existing:
                entity = resolution.entity
                was_created = False
                logger.info(
                    "Entity reused",
                    extra={
                        "entity_id": str(entity.id),
                        "match": resolution.reason,
                        "entity_type": entity.entity_type.value,
                    },
                )
            else:
                entity = await self._create_entity(candidate)
                was_created = True

            await self._add_aliases(entity, candidate.aliases)
            await self._link_memory(memory, entity, candidate.name)

        return entity, was_created

    async def _create_entity(self, candidate: EntityCandidate) -> Entity:
        entity = Entity(
            canonical_name=clean_display_name(candidate.name),
            normalized_name=normalize_name(candidate.name),
            entity_type=candidate.entity_type,
            status=EntityStatus.ACTIVE,
            description=candidate.description,
        )
        self._session.add(entity)
        await self._session.flush()

        logger.info(
            "Entity created",
            extra={
                "entity_id": str(entity.id),
                "entity_type": entity.entity_type.value,
                "has_description": entity.description is not None,
            },
        )
        return entity

    async def _add_aliases(self, entity: Entity, aliases: Sequence[str]) -> None:
        """Add aliases that do not make resolution ambiguous.

        An alias is skipped when it already names another entity or is already
        registered elsewhere -- a duplicate alias is refused, not overwritten.
        """
        for raw in aliases:
            normalized = normalize_name(raw)
            if not normalized or normalized == entity.normalized_name:
                continue

            conflict = await self._resolver.alias_conflict(normalized)
            if conflict is not None:
                logger.info(
                    "Alias rejected",
                    extra={"reason": conflict, "entity_id": str(entity.id)},
                )
                continue

            alias = EntityAlias(
                entity_id=entity.id,
                alias=clean_display_name(raw),
                normalized_alias=normalized,
            )
            self._session.add(alias)
            try:
                async with self._session.begin_nested():
                    await self._session.flush()
            except IntegrityError:
                # Another extraction registered the same alias concurrently.
                logger.info(
                    "Alias rejected",
                    extra={
                        "reason": "unique constraint",
                        "entity_id": str(entity.id),
                    },
                )
                continue

            logger.info("Alias created", extra={"entity_id": str(entity.id)})

    async def _link_memory(
        self, memory: Memory, entity: Entity, mention_text: str
    ) -> None:
        """Link memory to entity, ignoring a link that already exists."""
        existing = await self._session.get(MemoryEntity, (memory.id, entity.id))
        if existing is not None:
            return

        link = MemoryEntity(
            memory_id=memory.id,
            entity_id=entity.id,
            mention_text=clean_display_name(mention_text)[:200],
        )
        self._session.add(link)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            # Concurrent extraction created the same link.
            return

        logger.info(
            "Memory linked to entity",
            extra={"memory_id": str(memory.id), "entity_id": str(entity.id)},
        )

    # --- Queries ------------------------------------------------------------

    async def list_entities(
        self,
        entity_type=None,
        status=None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Entity]:
        statement = select(Entity)
        if entity_type is not None:
            statement = statement.where(Entity.entity_type == entity_type)
        if status is not None:
            statement = statement.where(Entity.status == status)
        statement = (
            statement.order_by(Entity.created_at.desc()).limit(limit).offset(offset)
        )
        try:
            result = await self._session.execute(statement)
            return result.scalars().unique().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("list entities", exc)

    async def count_entities(self, entity_type=None, status=None) -> int:
        statement = select(func.count()).select_from(Entity)
        if entity_type is not None:
            statement = statement.where(Entity.entity_type == entity_type)
        if status is not None:
            statement = statement.where(Entity.status == status)
        try:
            result = await self._session.execute(statement)
            return int(result.scalar_one())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("count entities", exc)

    async def get_entity(self, entity_id: uuid.UUID) -> Entity:
        try:
            entity = await self._session.get(Entity, entity_id)
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("get entity", exc)
        if entity is None:
            raise EntityNotFoundError(f"Entity {entity_id} does not exist.")
        return entity

    async def count_linked_memories(self, entity_id: uuid.UUID) -> int:
        try:
            result = await self._session.execute(
                select(func.count())
                .select_from(MemoryEntity)
                .where(MemoryEntity.entity_id == entity_id)
            )
            return int(result.scalar_one())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("count linked memories", exc)

    async def get_linked_memories(
        self, entity_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> Sequence[Memory]:
        statement = (
            select(Memory)
            .join(MemoryEntity, MemoryEntity.memory_id == Memory.id)
            .where(MemoryEntity.entity_id == entity_id)
            .order_by(Memory.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        try:
            result = await self._session.execute(statement)
            return result.scalars().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("list linked memories", exc)

    async def delete_entity(self, entity_id: uuid.UUID) -> None:
        """Delete an entity, its aliases and its links.

        The memories themselves are untouched: only the links are removed, by
        ON DELETE CASCADE on `memory_entities.entity_id`.
        """
        try:
            result = await self._session.execute(
                delete(Entity).where(Entity.id == entity_id)
            )
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("delete entity", exc)
        if result.rowcount == 0:
            raise EntityNotFoundError(f"Entity {entity_id} does not exist.")
        logger.info("Entity deleted", extra={"entity_id": str(entity_id)})

    @staticmethod
    def _wrap_db_error(action: str, exc: Exception) -> DatabaseError:
        logger.error(
            "Database operation failed",
            extra={"action": action, "error": str(exc)},
            exc_info=exc,
        )
        return DatabaseError(f"Could not {action}.")
