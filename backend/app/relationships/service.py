"""Relationship persistence and the extraction pipeline.

Pipeline, per stored memory:

    memory + its linked entities
      -> extract candidates -> confidence filter
      -> resolve both ends against EXISTING entities (Stage 2B resolver)
      -> reject anything naming an unknown entity
      -> reuse an existing relationship, or create one
      -> attach the memory as evidence

Only this module writes relationships. The model proposes; the application
decides. Entity creation is never performed here -- that belongs to Stage 2B.

Stage 7B unit 5: the service belongs to one knowledge scope, given at
construction. Both ends resolve only within the scope, relationships are
written with `scope.write_columns()`, evidence is attached only between a
relationship and a memory of the scope, and every read is filtered by
`scope.clause`.
"""

import uuid
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.entities.models import Entity, MemoryEntity
from app.entities.resolver import USER_ENTITY_NORMALIZED_NAME, EntityResolver
from app.identity.scope import KnowledgeScope, require_scope
from app.llm.base import LLMProvider
from app.memory.models import Memory
from app.relationships.extractor import RelationshipExtractor
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
)
from app.relationships.schemas import RelationshipCandidate

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)

#: `USER_ENTITY_NORMALIZED_NAME` (imported above) names the implicit subject
#: of every memory. Memories are written in the third person about "User",
#: and Stage 2B deliberately does not extract the user as an entity, so the
#: user entity is always offered to relationship extraction. Relationships
#: like INTERESTED_IN and HAS_GOAL are meaningless without it. Since Stage 7B
#: unit 5 each scope has its own, provisioned by the entity service; the
#: migration-seeded singleton is unattributed and never matched.


class RelationshipNotFoundError(NotFoundError):
    code = "relationship_not_found"
    message = "Relationship not found."


class RelationshipService:
    def __init__(
        self,
        session: AsyncSession,
        provider: Optional[LLMProvider] = None,
        settings: Optional[Settings] = None,
        extractor: Optional[RelationshipExtractor] = None,
        *,
        scope: KnowledgeScope,
    ) -> None:
        self._session = session
        self._scope = require_scope(scope, "RelationshipService")
        self._settings = settings or get_settings()
        self._provider = provider
        self._extractor = extractor or (
            RelationshipExtractor(provider, self._settings) if provider else None
        )
        self._resolver = EntityResolver(session, self._scope)

    @property
    def scope(self) -> KnowledgeScope:
        return self._scope

    # --- Extraction pipeline ------------------------------------------------

    async def extract_for_memory(self, memory: Memory) -> List[Relationship]:
        """Extract, resolve and store relationships for one stored memory.

        Never raises. The memory and its entities are already committed.
        """
        if not self._settings.RELATIONSHIP_EXTRACTION_ENABLED:
            return []
        if self._extractor is None:
            logger.warning("Relationship extraction skipped: no provider configured")
            return []
        if not self._scope.owns(memory):
            logger.warning(
                "Relationship extraction refused: the memory is not in its scope",
                extra={"memory_id": str(memory.id)},
            )
            return []

        entities = await self._available_entities(memory)
        if len(entities) < 2:
            # Nothing to relate. Extraction is not even attempted, which also
            # keeps the per-turn model-call count down.
            logger.info(
                "Relationship extraction skipped: fewer than two entities",
                extra={"memory_id": str(memory.id), "linked_entities": len(entities)},
            )
            return []

        by_name = {entity.canonical_name: entity for entity in entities}
        logger.info(
            "Relationship extraction started",
            extra={"memory_id": str(memory.id), "linked_entities": len(entities)},
        )

        try:
            candidates = await self._extractor.extract(
                memory_content=memory.content,
                memory_type=memory.memory_type.value,
                entity_names=list(by_name),
            )
        except Exception as exc:  # noqa: BLE001 - belt and braces
            logger.error(
                "Relationship extraction raised unexpectedly",
                extra={"memory_id": str(memory.id), "error": str(exc)},
                exc_info=exc,
            )
            return []

        accepted = self._filter_by_confidence(candidates)

        stored: List[Relationship] = []
        new_count = reused_count = unresolved = 0
        for candidate in accepted:
            try:
                relationship, was_new = await self._store_candidate(candidate, memory)
            except Exception as exc:  # noqa: BLE001 - contain per candidate
                logger.error(
                    "Failed to store relationship candidate",
                    extra={"memory_id": str(memory.id), "error": str(exc)},
                )
                continue
            if relationship is None:
                unresolved += 1
                continue
            stored.append(relationship)
            new_count += int(was_new)
            reused_count += int(not was_new)

        logger.info(
            "Relationship extraction completed",
            extra={
                "memory_id": str(memory.id),
                "candidates": len(candidates),
                "below_confidence": len(candidates) - len(accepted),
                "unresolved_entities": unresolved,
                "relationships_created": new_count,
                "relationships_reused": reused_count,
            },
        )
        return stored

    async def _available_entities(self, memory: Memory) -> List[Entity]:
        """Entities this memory may relate: its own, plus the implicit user."""
        statement = (
            select(Entity)
            .join(MemoryEntity, MemoryEntity.entity_id == Entity.id)
            .where(MemoryEntity.memory_id == memory.id, self._scope.clause(Entity))
        )
        try:
            linked = list((await self._session.execute(statement)).scalars().all())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("load entities for a memory", exc)

        user = await self._user_entity()
        if user is not None and all(e.id != user.id for e in linked):
            linked.append(user)
        return linked

    async def _user_entity(self) -> Optional[Entity]:
        """This scope's "User" entity, if the entity service has provisioned
        one. Never another scope's, and never the unattributed legacy one."""
        try:
            result = await self._session.execute(
                select(Entity).where(
                    Entity.normalized_name == USER_ENTITY_NORMALIZED_NAME,
                    self._scope.clause(Entity),
                )
            )
            return result.scalars().first()
        except _DB_ERRORS:
            return None

    def _filter_by_confidence(
        self, candidates: Sequence[RelationshipCandidate]
    ) -> List[RelationshipCandidate]:
        kept: List[RelationshipCandidate] = []
        for candidate in candidates:
            if candidate.confidence_score < self._settings.RELATIONSHIP_MIN_CONFIDENCE:
                logger.info(
                    "Relationship rejected: confidence below threshold",
                    extra={
                        "confidence": candidate.confidence_score,
                        "threshold": self._settings.RELATIONSHIP_MIN_CONFIDENCE,
                        "relationship_type": candidate.relationship_type.value,
                    },
                )
                continue
            kept.append(candidate)
        return kept

    async def _store_candidate(
        self, candidate: RelationshipCandidate, memory: Memory
    ) -> Tuple[Optional[Relationship], bool]:
        """Resolve both ends, then store the relationship with its evidence.

        The relationship and its evidence are written in one savepoint, so a
        relationship can never exist without the evidence that justifies it.
        """
        source = await self._resolve_existing(candidate.source_entity)
        target = await self._resolve_existing(candidate.target_entity)

        if source is None or target is None:
            # The model named something that is not an entity. Relationships
            # are only ever created between entities that already exist.
            logger.info(
                "Relationship rejected: entity does not exist",
                extra={
                    "relationship_type": candidate.relationship_type.value,
                    "source_resolved": source is not None,
                    "target_resolved": target is not None,
                },
            )
            return None, False

        if source.id == target.id:
            logger.info(
                "Relationship rejected: resolves to a self-reference",
                extra={"relationship_type": candidate.relationship_type.value},
            )
            return None, False

        # Reuse first: several memories supporting the same claim must become
        # evidence on one relationship, not several relationships.
        existing = await self._find_existing(
            source.id, candidate.relationship_type, target.id
        )
        if existing is not None:
            added = await self._add_evidence(existing, memory)
            logger.info(
                "Relationship reused",
                extra={
                    "relationship_id": str(existing.id),
                    "relationship_type": existing.relationship_type.value,
                    "evidence_added": added,
                },
            )
            return existing, False

        relationship = Relationship(
            source_entity_id=source.id,
            relationship_type=candidate.relationship_type,
            target_entity_id=target.id,
            confidence_score=candidate.confidence_score,
            status=RelationshipStatus.ACTIVE,
            **self._scope.write_columns(),
        )
        try:
            # The relationship and its first evidence row are written in one
            # savepoint: a relationship must never exist without the evidence
            # that justifies it.
            async with self._session.begin_nested():
                self._session.add(relationship)
                await self._session.flush()
                await self._add_evidence(relationship, memory)
        except IntegrityError:
            # A concurrent extraction created the same triple. The savepoint
            # has rolled back, so re-read and attach evidence to that one
            # rather than losing it.
            existing = await self._find_existing(
                source.id, candidate.relationship_type, target.id
            )
            if existing is None:
                logger.warning(
                    "Relationship insert conflicted but no existing row was found",
                    extra={"relationship_type": candidate.relationship_type.value},
                )
                return None, False
            await self._add_evidence(existing, memory)
            logger.info(
                "Relationship reused after a concurrent insert",
                extra={"relationship_id": str(existing.id)},
            )
            return existing, False

        logger.info(
            "Relationship created",
            extra={
                "relationship_id": str(relationship.id),
                "relationship_type": relationship.relationship_type.value,
                "confidence": relationship.confidence_score,
            },
        )
        return relationship, True

    async def _resolve_existing(self, name: str) -> Optional[Entity]:
        """Resolve a name to an existing entity, reusing the Stage 2B resolver."""
        resolution = await self._resolver.resolve(name)
        return resolution.entity

    async def _find_existing(
        self, source_id: uuid.UUID, relationship_type, target_id: uuid.UUID
    ) -> Optional[Relationship]:
        result = await self._session.execute(
            select(Relationship).where(
                Relationship.source_entity_id == source_id,
                Relationship.relationship_type == relationship_type,
                Relationship.target_entity_id == target_id,
                Relationship.status == RelationshipStatus.ACTIVE,
                self._scope.clause(Relationship),
            )
        )
        return result.scalars().first()

    async def _add_evidence(
        self, relationship: Relationship, memory: Memory
    ) -> bool:
        """Attach a memory as evidence, ignoring a duplicate pairing.

        Evidence is a child row with no scope of its own: it is written only
        between a relationship and a memory of this scope."""
        if not (self._scope.owns(relationship) and self._scope.owns(memory)):
            return False
        existing = await self._session.execute(
            select(RelationshipEvidence.id).where(
                RelationshipEvidence.relationship_id == relationship.id,
                RelationshipEvidence.memory_id == memory.id,
            )
        )
        if existing.scalars().first() is not None:
            return False

        evidence = RelationshipEvidence(
            relationship_id=relationship.id, memory_id=memory.id
        )
        self._session.add(evidence)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            return False

        logger.info(
            "Relationship evidence added",
            extra={
                "relationship_id": str(relationship.id),
                "memory_id": str(memory.id),
            },
        )
        return True

    # --- Queries ------------------------------------------------------------

    async def list_relationships(
        self,
        relationship_type=None,
        source_entity_id: Optional[uuid.UUID] = None,
        target_entity_id: Optional[uuid.UUID] = None,
        status=None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Relationship]:
        statement = select(Relationship).where(self._scope.clause(Relationship))
        statement = self._apply_filters(
            statement, relationship_type, source_entity_id, target_entity_id, status
        )
        statement = (
            statement.order_by(Relationship.created_at.desc()).limit(limit).offset(offset)
        )
        try:
            result = await self._session.execute(statement)
            return result.scalars().unique().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("list relationships", exc)

    async def count_relationships(
        self,
        relationship_type=None,
        source_entity_id: Optional[uuid.UUID] = None,
        target_entity_id: Optional[uuid.UUID] = None,
        status=None,
    ) -> int:
        statement = select(func.count()).select_from(Relationship).where(
            self._scope.clause(Relationship)
        )
        statement = self._apply_filters(
            statement, relationship_type, source_entity_id, target_entity_id, status
        )
        try:
            result = await self._session.execute(statement)
            return int(result.scalar_one())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("count relationships", exc)

    @staticmethod
    def _apply_filters(
        statement, relationship_type, source_entity_id, target_entity_id, status
    ):
        if relationship_type is not None:
            statement = statement.where(
                Relationship.relationship_type == relationship_type
            )
        if source_entity_id is not None:
            statement = statement.where(
                Relationship.source_entity_id == source_entity_id
            )
        if target_entity_id is not None:
            statement = statement.where(
                Relationship.target_entity_id == target_entity_id
            )
        if status is not None:
            statement = statement.where(Relationship.status == status)
        return statement

    async def get_relationship(self, relationship_id: uuid.UUID) -> Relationship:
        # A filtered SELECT, never `session.get` (the identity map bypasses
        # the filter).
        try:
            relationship = (
                await self._session.execute(
                    select(Relationship).where(
                        Relationship.id == relationship_id,
                        self._scope.clause(Relationship),
                    )
                )
            ).scalars().first()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("get relationship", exc)
        if relationship is None:
            raise RelationshipNotFoundError(
                f"Relationship {relationship_id} does not exist."
            )
        return relationship

    async def get_entity_ref(self, entity_id: uuid.UUID):
        """Minimal entity view for rendering a relationship's two ends."""
        from app.relationships.schemas import EntityRef

        entity = (
            await self._session.execute(
                select(Entity).where(Entity.id == entity_id, self._scope.clause(Entity))
            )
        ).scalars().first()
        if entity is None:  # pragma: no cover - same-scope ends by construction
            raise RelationshipNotFoundError("Relationship references a missing entity.")
        return EntityRef(
            id=entity.id,
            canonical_name=entity.canonical_name,
            entity_type=entity.entity_type.value,
        )

    async def count_evidence(self, relationship_id: uuid.UUID) -> int:
        try:
            result = await self._session.execute(
                select(func.count())
                .select_from(RelationshipEvidence)
                .join(Relationship, Relationship.id == RelationshipEvidence.relationship_id)
                .join(Memory, Memory.id == RelationshipEvidence.memory_id)
                .where(
                    RelationshipEvidence.relationship_id == relationship_id,
                    self._scope.clause(Relationship),
                    self._scope.clause(Memory),
                )
            )
            return int(result.scalar_one())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("count relationship evidence", exc)

    async def get_evidence_memories(
        self, relationship_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> Sequence[Tuple[Memory, RelationshipEvidence]]:
        statement = (
            select(Memory, RelationshipEvidence)
            .join(RelationshipEvidence, RelationshipEvidence.memory_id == Memory.id)
            .join(Relationship, Relationship.id == RelationshipEvidence.relationship_id)
            .where(
                RelationshipEvidence.relationship_id == relationship_id,
                self._scope.clause(Relationship),
                self._scope.clause(Memory),
            )
            .order_by(RelationshipEvidence.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        try:
            result = await self._session.execute(statement)
            return result.all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("list relationship evidence", exc)

    async def get_entity_relationships(
        self, entity_id: uuid.UUID, status=None
    ) -> Tuple[Sequence[Relationship], Sequence[Relationship]]:
        """Return (outgoing, incoming) for one entity, kept distinct."""
        outgoing = await self.list_relationships(
            source_entity_id=entity_id, status=status, limit=200
        )
        incoming = await self.list_relationships(
            target_entity_id=entity_id, status=status, limit=200
        )
        return outgoing, incoming

    async def delete_relationship(self, relationship_id: uuid.UUID) -> None:
        """Delete a relationship and its evidence.

        Entities and memories are untouched: only the evidence rows go, by
        ON DELETE CASCADE on `relationship_evidence.relationship_id`.
        """
        try:
            result = await self._session.execute(
                delete(Relationship).where(
                    Relationship.id == relationship_id, self._scope.clause(Relationship)
                )
            )
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("delete relationship", exc)
        if result.rowcount == 0:
            raise RelationshipNotFoundError(
                f"Relationship {relationship_id} does not exist."
            )
        logger.info(
            "Relationship deleted", extra={"relationship_id": str(relationship_id)}
        )

    @staticmethod
    def _wrap_db_error(action: str, exc: Exception) -> DatabaseError:
        logger.error(
            "Database operation failed",
            extra={"action": action, "error": str(exc)},
            exc_info=exc,
        )
        return DatabaseError(f"Could not {action}.")
