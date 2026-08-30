"""Entity resolution, aliases, linking and transaction integrity."""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.entities.models import Entity, EntityAlias, EntityStatus, EntityType, MemoryEntity
from app.entities.service import EntityService
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.services.conversation_service import ConversationService


def payload(*entities) -> str:
    return json.dumps({"entities": list(entities)})


def candidate(name, entity_type="technology", description=None, aliases=None,
              confidence=0.95):
    entry = {"name": name, "entity_type": entity_type,
             "confidence_score": confidence}
    if description is not None:
        entry["description"] = description
    if aliases is not None:
        entry["aliases"] = aliases
    return entry


@pytest.fixture
async def conversation(db_session):
    return await ConversationService(db_session).create_conversation()


async def make_memory(db_session, conversation, content="User decided to use PostgreSQL for Mai.") -> Memory:
    memory = Memory(
        content=content,
        normalized_content=content.lower().rstrip("."),
        memory_type=MemoryType.DECISION,
        status=MemoryStatus.ACTIVE,
        importance_score=7,
        confidence_score=0.95,
        source_conversation_id=conversation.id,
    )
    db_session.add(memory)
    await db_session.flush()
    return memory


def service(db_session, fake_provider, settings) -> EntityService:
    return EntityService(session=db_session, provider=fake_provider, settings=settings)


# --- Creation and linking ---------------------------------------------------


async def test_entities_are_created_and_linked(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology"), candidate("Mai", "project")
    )

    linked = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert {e.canonical_name for e in linked} == {"PostgreSQL", "Mai"}
    assert {e.entity_type for e in linked} == {EntityType.TECHNOLOGY, EntityType.PROJECT}
    assert all(e.status is EntityStatus.ACTIVE for e in linked)

    links = (await db_session.execute(
        select(MemoryEntity).where(MemoryEntity.memory_id == memory.id)
    )).scalars().all()
    assert len(links) == 2


async def test_canonical_case_is_stored_not_lowercased(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("PostgreSQL", "technology"))

    entity = (await service(db_session, fake_provider, settings).extract_for_memory(memory))[0]

    assert entity.canonical_name == "PostgreSQL"
    assert entity.normalized_name == "postgresql"


# --- Resolution: the same entity across memories ----------------------------


@pytest.mark.parametrize(
    "second_form", ["postgresql", "POSTGRESQL", "PostgreSQL", "PostgreSQL database"]
)
async def test_case_and_descriptor_variants_reuse_one_entity(
    db_session, fake_provider, settings, conversation, second_form
) -> None:
    memory_one = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("PostgreSQL", "technology"))
    first = (await service(db_session, fake_provider, settings).extract_for_memory(memory_one))[0]

    memory_two = await make_memory(db_session, conversation, "User still uses it.")
    fake_provider.entity_reply = payload(candidate(second_form, "technology"))
    second = (await service(db_session, fake_provider, settings).extract_for_memory(memory_two))[0]

    assert second.id == first.id
    total = (await db_session.execute(select(func.count()).select_from(Entity))).scalar_one()
    assert total == 1


async def test_first_classification_wins_on_reuse(
    db_session, fake_provider, settings, conversation
) -> None:
    """Models relabel the same thing; that must not fork the entity."""
    memory_one = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("Groq", "company"))
    first = (await service(db_session, fake_provider, settings).extract_for_memory(memory_one))[0]

    memory_two = await make_memory(db_session, conversation, "More about it.")
    fake_provider.entity_reply = payload(candidate("Groq", "technology"))
    second = (await service(db_session, fake_provider, settings).extract_for_memory(memory_two))[0]

    assert second.id == first.id
    assert second.entity_type is EntityType.COMPANY


async def test_distinct_entities_are_not_merged(
    db_session, fake_provider, settings, conversation
) -> None:
    """The specification's example: Claude and Claude Code stay separate."""
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("Claude", "product"), candidate("Claude Code", "product")
    )

    linked = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert len({e.id for e in linked}) == 2


# --- Aliases ----------------------------------------------------------------


async def test_alias_is_created_and_resolves_later(
    db_session, fake_provider, settings, conversation
) -> None:
    memory_one = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology", aliases=["Postgres"])
    )
    entity_service = service(db_session, fake_provider, settings)
    first = (await entity_service.extract_for_memory(memory_one))[0]

    aliases = (await db_session.execute(
        select(EntityAlias).where(EntityAlias.entity_id == first.id)
    )).scalars().all()
    assert [a.normalized_alias for a in aliases] == ["postgres"]

    # A later memory mentioning the alias reuses the entity.
    memory_two = await make_memory(db_session, conversation, "More postgres talk.")
    fake_provider.entity_reply = payload(candidate("postgres", "technology"))
    second = (await entity_service.extract_for_memory(memory_two))[0]

    assert second.id == first.id


async def test_alias_matching_the_canonical_name_is_skipped(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology", aliases=["postgresql", "PostgreSQL"])
    )

    entity = (await service(db_session, fake_provider, settings).extract_for_memory(memory))[0]
    count = (await db_session.execute(
        select(func.count()).select_from(EntityAlias).where(EntityAlias.entity_id == entity.id)
    )).scalar_one()
    assert count == 0


async def test_alias_clashing_with_another_entity_is_refused(
    db_session, fake_provider, settings, conversation
) -> None:
    """An ambiguous alias is worse than no alias."""
    memory_one = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("Claude", "product"))
    entity_service = service(db_session, fake_provider, settings)
    await entity_service.extract_for_memory(memory_one)

    memory_two = await make_memory(db_session, conversation, "Another one.")
    fake_provider.entity_reply = payload(
        candidate("Anthropic", "company", aliases=["Claude"])
    )
    await entity_service.extract_for_memory(memory_two)

    # "Claude" already names an entity, so it cannot also be Anthropic's alias.
    total_aliases = (await db_session.execute(
        select(func.count()).select_from(EntityAlias)
    )).scalar_one()
    assert total_aliases == 0


async def test_the_same_alias_cannot_point_at_two_entities(
    db_session, fake_provider, settings, conversation
) -> None:
    memory_one = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("Artificial Intelligence", "concept", aliases=["AI"])
    )
    entity_service = service(db_session, fake_provider, settings)
    await entity_service.extract_for_memory(memory_one)

    memory_two = await make_memory(db_session, conversation, "Other.")
    fake_provider.entity_reply = payload(
        candidate("Applied Informatics", "concept", aliases=["AI"])
    )
    await entity_service.extract_for_memory(memory_two)

    ai_aliases = (await db_session.execute(
        select(func.count()).select_from(EntityAlias).where(EntityAlias.normalized_alias == "ai")
    )).scalar_one()
    assert ai_aliases == 1


# --- Many-to-many linking ---------------------------------------------------


async def test_one_memory_links_to_many_entities(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("Mai", "project"),
        candidate("PostgreSQL", "technology"),
        candidate("Groq", "company"),
    )

    linked = await service(db_session, fake_provider, settings).extract_for_memory(memory)
    assert len(linked) == 3


async def test_one_entity_links_to_many_memories(
    db_session, fake_provider, settings, conversation
) -> None:
    entity_service = service(db_session, fake_provider, settings)
    ids = []
    for text in ["User uses PostgreSQL daily.", "User tuned PostgreSQL settings."]:
        memory = await make_memory(db_session, conversation, text)
        fake_provider.entity_reply = payload(candidate("PostgreSQL", "technology"))
        ids.append((await entity_service.extract_for_memory(memory))[0].id)

    assert len(set(ids)) == 1
    link_count = (await db_session.execute(
        select(func.count()).select_from(MemoryEntity).where(MemoryEntity.entity_id == ids[0])
    )).scalar_one()
    assert link_count == 2


async def test_re_extracting_a_memory_does_not_duplicate_links(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("PostgreSQL", "technology"))
    entity_service = service(db_session, fake_provider, settings)

    await entity_service.extract_for_memory(memory)
    await entity_service.extract_for_memory(memory)
    await entity_service.extract_for_memory(memory)

    links = (await db_session.execute(
        select(func.count()).select_from(MemoryEntity).where(MemoryEntity.memory_id == memory.id)
    )).scalar_one()
    assert links == 1


# --- Confidence threshold ---------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.69])
async def test_low_confidence_candidates_are_discarded(
    db_session, fake_provider, settings, conversation, confidence
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology", confidence=confidence)
    )

    assert await service(db_session, fake_provider, settings).extract_for_memory(memory) == []


async def test_confidence_threshold_is_configurable(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.ENTITY_MIN_CONFIDENCE = 0.3
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology", confidence=0.5)
    )

    assert len(await service(db_session, fake_provider, settings).extract_for_memory(memory)) == 1


# --- Cascade behaviour ------------------------------------------------------


async def test_deleting_an_entity_leaves_the_memory_intact(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology", aliases=["Postgres"])
    )
    entity_service = service(db_session, fake_provider, settings)
    entity = (await entity_service.extract_for_memory(memory))[0]
    await db_session.commit()

    await entity_service.delete_entity(entity.id)
    await db_session.commit()

    # The memory survives; only the link and alias are gone.
    assert await db_session.get(Memory, memory.id) is not None
    assert (await db_session.execute(select(func.count()).select_from(MemoryEntity))).scalar_one() == 0
    assert (await db_session.execute(select(func.count()).select_from(EntityAlias))).scalar_one() == 0


async def test_deleting_a_memory_removes_only_its_links(
    db_session, fake_provider, settings, conversation
) -> None:
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("PostgreSQL", "technology"))
    entity_service = service(db_session, fake_provider, settings)
    entity = (await entity_service.extract_for_memory(memory))[0]
    await db_session.commit()

    await db_session.delete(memory)
    await db_session.commit()

    assert await db_session.get(Entity, entity.id) is not None
    assert (await db_session.execute(select(func.count()).select_from(MemoryEntity))).scalar_one() == 0


async def test_extraction_can_be_disabled(
    db_session, fake_provider, settings, conversation
) -> None:
    settings.ENTITY_EXTRACTION_ENABLED = False
    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(candidate("PostgreSQL", "technology"))

    assert await service(db_session, fake_provider, settings).extract_for_memory(memory) == []
    assert fake_provider.entity_calls == []


async def test_unknown_entity_lookup_raises_not_found(
    db_session, fake_provider, settings
) -> None:
    from app.entities.service import EntityNotFoundError

    with pytest.raises(EntityNotFoundError):
        await service(db_session, fake_provider, settings).get_entity(uuid.uuid4())


# --- Logging safety ---------------------------------------------------------
# A reserved LogRecord attribute in `extra` raises at runtime, but only when
# the log level is enabled. That aborted entity extraction after the writes and
# before the commit, silently discarding every entity. Live execution found it;
# the suite had not, because it ran at WARNING.


def test_no_log_extra_collides_with_a_reserved_attribute() -> None:
    import ast
    import logging
    import pathlib

    probe = logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None)
    reserved = set(probe.__dict__) | {"message", "asctime"}

    collisions = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and key.value in reserved:
                        collisions.append(f"{path}:{key.lineno} -> {key.value!r}")

    assert not collisions, "reserved LogRecord keys in extra: " + "; ".join(collisions)


async def test_extraction_logging_executes_at_info_level(
    db_session, fake_provider, settings, conversation, caplog
) -> None:
    """Exercise every log line on the success path, not just the code around it."""
    import logging

    memory = await make_memory(db_session, conversation)
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology", aliases=["Postgres"])
    )

    with caplog.at_level(logging.INFO):
        linked = await service(db_session, fake_provider, settings).extract_for_memory(memory)

    assert len(linked) == 1
    messages = {record.message for record in caplog.records}
    assert "Entity extraction started" in messages
    assert "Entity created" in messages
    assert "Alias created" in messages
    assert "Memory linked to entity" in messages
    assert "Entity extraction completed" in messages
