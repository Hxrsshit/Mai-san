"""Relationship resolution, deduplication, evidence and integrity."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.entities.models import Entity, EntityStatus, EntityType, MemoryEntity
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)
from app.relationships.service import RelationshipService
from app.services.conversation_service import ConversationService


def payload(*relationships) -> str:
    return json.dumps({"relationships": list(relationships)})


def candidate(source, rel_type, target, confidence=0.93):
    return {
        "source_entity": source,
        "relationship_type": rel_type,
        "target_entity": target,
        "confidence_score": confidence,
    }


@pytest.fixture
async def conversation(db_session):
    return await ConversationService(db_session).create_conversation()


async def make_entity(db_session, name, kind=EntityType.TECHNOLOGY) -> Entity:
    entity = Entity(
        canonical_name=name,
        normalized_name=name.lower(),
        entity_type=kind,
        status=EntityStatus.ACTIVE,
    )
    db_session.add(entity)
    await db_session.flush()
    return entity


async def make_memory(db_session, conversation, content, entities=()) -> Memory:
    memory = Memory(
        content=content,
        normalized_content=content.lower().rstrip("."),
        memory_type=MemoryType.DECISION,
        status=MemoryStatus.ACTIVE,
        importance_score=8,
        confidence_score=0.95,
        source_conversation_id=conversation.id,
    )
    db_session.add(memory)
    await db_session.flush()
    for entity in entities:
        db_session.add(MemoryEntity(memory_id=memory.id, entity_id=entity.id))
    await db_session.flush()
    return memory


def service(db_session, fake_provider, settings) -> RelationshipService:
    return RelationshipService(
        session=db_session, provider=fake_provider, settings=settings
    )


# --- Creation and direction -------------------------------------------------


async def test_relationship_is_created_with_correct_direction(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "Mai uses PostgreSQL.", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))

    stored = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert len(stored) == 1
    rel = stored[0]
    assert rel.source_entity_id == mai.id
    assert rel.target_entity_id == pg.id
    assert rel.relationship_type is RelationshipType.USES
    assert rel.status is RelationshipStatus.ACTIVE


async def test_direction_is_not_reversed(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "Mai uses PostgreSQL.", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))

    rel = (await service(db_session, fake_provider, settings).extract_for_memory(memory))[0]

    assert (rel.source_entity_id, rel.target_entity_id) == (mai.id, pg.id)
    assert (rel.source_entity_id, rel.target_entity_id) != (pg.id, mai.id)


async def test_both_directions_can_coexist_as_distinct_claims(
    db_session, fake_provider, settings, conversation
) -> None:
    a = await make_entity(db_session, "Mai", EntityType.PROJECT)
    b = await make_entity(db_session, "Groq", EntityType.COMPANY)
    memory = await make_memory(db_session, conversation, "x", [a, b])
    fake_provider.relationship_reply = payload(
        candidate("Mai", "DEPENDS_ON", "Groq"), candidate("Groq", "RELATED_TO", "Mai")
    )

    stored = await service(db_session, fake_provider, settings).extract_for_memory(memory)
    assert len(stored) == 2


# --- Entity resolution ------------------------------------------------------


@pytest.mark.parametrize("written_as", ["PostgreSQL", "postgresql", "POSTGRESQL",
                                        "PostgreSQL database"])
async def test_entity_names_resolve_via_stage_2b_logic(
    db_session, fake_provider, settings, conversation, written_as
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", written_as))

    stored = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert len(stored) == 1
    assert stored[0].target_entity_id == pg.id


async def test_unknown_entities_are_rejected_not_created(
    db_session, fake_provider, settings, conversation
) -> None:
    """The relationship system never creates entities."""
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    before = (await db_session.execute(select(func.count()).select_from(Entity))).scalar_one()

    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "MongoDB"),          # target unknown
        candidate("Redis", "USES", "PostgreSQL"),     # source unknown
    )
    stored = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert stored == []
    after = (await db_session.execute(select(func.count()).select_from(Entity))).scalar_one()
    assert after == before


async def test_self_reference_after_resolution_is_rejected(
    db_session, fake_provider, settings, conversation
) -> None:
    """Two different names may resolve to the same entity."""
    pg = await make_entity(db_session, "PostgreSQL")
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    memory = await make_memory(db_session, conversation, "x", [pg, mai])
    fake_provider.relationship_reply = payload(
        candidate("PostgreSQL", "USES", "postgresql database")
    )

    assert await service(db_session, fake_provider, settings).extract_for_memory(memory) == []


# --- Deduplication ----------------------------------------------------------


async def test_the_same_relationship_is_not_duplicated(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    relationships = service(db_session, fake_provider, settings)

    memory_one = await make_memory(db_session, conversation, "Mai uses PostgreSQL.", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    first = (await relationships.extract_for_memory(memory_one))[0]

    memory_two = await make_memory(db_session, conversation, "Mai still uses PostgreSQL.", [mai, pg])
    second = (await relationships.extract_for_memory(memory_two))[0]

    assert second.id == first.id
    total = (await db_session.execute(select(func.count()).select_from(Relationship))).scalar_one()
    assert total == 1


async def test_equivalent_wording_does_not_create_a_duplicate(
    db_session, fake_provider, settings, conversation
) -> None:
    """UTILIZES normalizes to USES, so it must reuse the existing row."""
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    relationships = service(db_session, fake_provider, settings)

    memory_one = await make_memory(db_session, conversation, "Mai uses PostgreSQL.", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    first = (await relationships.extract_for_memory(memory_one))[0]

    memory_two = await make_memory(db_session, conversation, "Mai utilizes PostgreSQL.", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "UTILIZES", "PostgreSQL"))
    second = (await relationships.extract_for_memory(memory_two))[0]

    assert second.id == first.id
    assert second.relationship_type is RelationshipType.USES
    total = (await db_session.execute(select(func.count()).select_from(Relationship))).scalar_one()
    assert total == 1


async def test_a_different_type_is_a_different_relationship(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "PostgreSQL"),
        candidate("Mai", "DEPENDS_ON", "PostgreSQL"),
    )

    stored = await service(db_session, fake_provider, settings).extract_for_memory(memory)
    assert len({r.id for r in stored}) == 2


# --- Evidence ---------------------------------------------------------------


async def test_two_memories_become_two_evidence_rows_on_one_relationship(
    db_session, fake_provider, settings, conversation
) -> None:
    """The specification's example, exactly."""
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    relationships = service(db_session, fake_provider, settings)
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))

    memory_one = await make_memory(db_session, conversation, "Mai uses PostgreSQL.", [mai, pg])
    first = (await relationships.extract_for_memory(memory_one))[0]

    memory_two = await make_memory(
        db_session, conversation, "PostgreSQL was selected as Mai's database.", [mai, pg]
    )
    second = (await relationships.extract_for_memory(memory_two))[0]

    assert first.id == second.id
    assert await relationships.count_evidence(first.id) == 2

    memory_ids = {
        row.memory_id
        for row in (
            await db_session.execute(
                select(RelationshipEvidence).where(
                    RelationshipEvidence.relationship_id == first.id
                )
            )
        ).scalars()
    }
    assert memory_ids == {memory_one.id, memory_two.id}


async def test_a_relationship_always_has_evidence(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))

    relationships = service(db_session, fake_provider, settings)
    rel = (await relationships.extract_for_memory(memory))[0]

    assert await relationships.count_evidence(rel.id) >= 1


async def test_re_extracting_the_same_memory_does_not_duplicate_evidence(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    relationships = service(db_session, fake_provider, settings)

    rel = (await relationships.extract_for_memory(memory))[0]
    await relationships.extract_for_memory(memory)
    await relationships.extract_for_memory(memory)

    assert await relationships.count_evidence(rel.id) == 1


# --- Fewer than two entities ------------------------------------------------


async def test_extraction_is_skipped_with_one_entity(
    db_session, fake_provider, settings, conversation
) -> None:
    """"User prefers concise answers." has nothing to relate."""
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "User prefers concise answers.", [pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))

    # The seeded User entity is absent in this fixture, so only one is available.
    assert await service(db_session, fake_provider, settings).extract_for_memory(memory) == []
    assert fake_provider.relationship_calls == []


async def test_the_seeded_user_entity_is_offered_when_present(
    db_session, fake_provider, settings, conversation
) -> None:
    """The user is the implicit subject of every memory."""
    user = await make_entity(db_session, "User", EntityType.PERSON)
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    memory = await make_memory(db_session, conversation, "User is building Mai.", [mai])
    fake_provider.relationship_reply = payload(candidate("User", "BUILDS", "Mai"))

    stored = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert len(stored) == 1
    assert stored[0].source_entity_id == user.id
    assert stored[0].target_entity_id == mai.id
    # "User" was offered even though it is not linked to the memory.
    assert "User" in fake_provider.last_relationship_call[1].content


# --- Confidence -------------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.69])
async def test_low_confidence_is_discarded(
    db_session, fake_provider, settings, conversation, confidence
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "PostgreSQL", confidence)
    )

    assert await service(db_session, fake_provider, settings).extract_for_memory(memory) == []


async def test_confidence_threshold_is_configurable(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.RELATIONSHIP_MIN_CONFIDENCE = 0.3
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "PostgreSQL", 0.5)
    )

    assert len(await service(db_session, fake_provider, settings).extract_for_memory(memory)) == 1


# --- Cascade behaviour ------------------------------------------------------


async def test_deleting_a_relationship_keeps_entities_and_memories(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    relationships = service(db_session, fake_provider, settings)
    rel = (await relationships.extract_for_memory(memory))[0]
    await db_session.commit()

    await relationships.delete_relationship(rel.id)
    await db_session.commit()

    assert await db_session.get(Entity, mai.id) is not None
    assert await db_session.get(Entity, pg.id) is not None
    assert await db_session.get(Memory, memory.id) is not None
    evidence = (await db_session.execute(
        select(func.count()).select_from(RelationshipEvidence)
    )).scalar_one()
    assert evidence == 0


async def test_deleting_an_entity_removes_its_relationships(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    await service(db_session, fake_provider, settings).extract_for_memory(memory)
    await db_session.commit()

    await db_session.delete(pg)
    await db_session.commit()

    assert (await db_session.execute(
        select(func.count()).select_from(Relationship)
    )).scalar_one() == 0
    # No orphan evidence.
    assert (await db_session.execute(
        select(func.count()).select_from(RelationshipEvidence)
    )).scalar_one() == 0
    # The memory survives.
    assert await db_session.get(Memory, memory.id) is not None


async def test_deleting_a_memory_removes_only_its_evidence(
    db_session, fake_provider, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    rel = (await service(db_session, fake_provider, settings).extract_for_memory(memory))[0]
    await db_session.commit()

    await db_session.delete(memory)
    await db_session.commit()

    # The relationship survives; its evidence row is gone.
    assert await db_session.get(Relationship, rel.id) is not None
    assert (await db_session.execute(
        select(func.count()).select_from(RelationshipEvidence)
    )).scalar_one() == 0


async def test_extraction_can_be_disabled(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.RELATIONSHIP_EXTRACTION_ENABLED = False
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    pg = await make_entity(db_session, "PostgreSQL")
    memory = await make_memory(db_session, conversation, "x", [mai, pg])
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))

    assert await service(db_session, fake_provider, settings).extract_for_memory(memory) == []
    assert fake_provider.relationship_calls == []


async def test_unknown_relationship_lookup_raises_not_found(
    db_session, fake_provider, settings
) -> None:
    from app.relationships.service import RelationshipNotFoundError

    with pytest.raises(RelationshipNotFoundError):
        await service(db_session, fake_provider, settings).get_relationship(uuid.uuid4())
