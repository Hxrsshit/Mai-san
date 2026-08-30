"""Stage 3C: supersession, non-conflict, ambiguity, and database invariants.

Exercises the detector and writer against a real database. The organising
question throughout is not "did it find a conflict?" but "did it find exactly
the conflicts that are really there?"
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.entities.models import Entity, EntityAlias, EntityStatus, EntityType, MemoryEntity
from app.knowledge.models import (
    ConflictReason,
    ConflictResolution,
    KnowledgeConflict,
)
from app.knowledge.schemas import ConflictOutcome
from app.knowledge.lifecycle import LifecycleWriter
from app.knowledge.service import KnowledgeService
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- Builders ---------------------------------------------------------------


async def make_entity(session, name, kind=EntityType.TECHNOLOGY, aliases=()):
    entity = Entity(
        canonical_name=name,
        normalized_name=name.lower(),
        entity_type=kind,
        status=EntityStatus.ACTIVE,
    )
    session.add(entity)
    await session.flush()
    for alias in aliases:
        session.add(
            EntityAlias(
                entity_id=entity.id, alias=alias, normalized_alias=alias.lower()
            )
        )
    await session.flush()
    return entity


async def make_memory(
    session, conversation_id, content, day=0, kind=MemoryType.SEMANTIC, entities=()
):
    created = BASE + timedelta(days=day)
    memory = Memory(
        content=content,
        normalized_content=content.lower().rstrip("."),
        memory_type=kind,
        status=MemoryStatus.ACTIVE,
        importance_score=8,
        confidence_score=0.95,
        source_conversation_id=conversation_id,
        created_at=created,
        updated_at=created,
    )
    session.add(memory)
    await session.flush()
    for entity in entities:
        session.add(MemoryEntity(memory_id=memory.id, entity_id=entity.id))
    await session.flush()
    return memory


async def make_relationship(session, source, kind, target, evidence=()):
    relationship = Relationship(
        source_entity_id=source.id,
        relationship_type=kind,
        target_entity_id=target.id,
        confidence_score=0.95,
        status=RelationshipStatus.ACTIVE,
    )
    session.add(relationship)
    await session.flush()
    for memory in evidence:
        session.add(
            RelationshipEvidence(
                relationship_id=relationship.id, memory_id=memory.id
            )
        )
    await session.flush()
    return relationship


@pytest.fixture
async def conversation(db_session):
    from app.services.conversation_service import ConversationService

    created = await ConversationService(db_session).create_conversation()
    return created.id


async def statuses(session):
    rows = (
        await session.execute(select(Memory.content, Memory.status))
    ).all()
    return {content: status for content, status in rows}


# --- Direct supersession ----------------------------------------------------


async def test_an_explicit_switch_supersedes_the_old_memory(
    db_session, settings, conversation
) -> None:
    openrouter = await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    groq = await make_entity(db_session, "Groq", EntityType.COMPANY)
    user = await make_entity(db_session, "User", EntityType.PERSON)

    old = await make_memory(
        db_session, conversation, "User uses OpenRouter for inference.", day=0,
        entities=[user, openrouter],
    )
    await make_relationship(
        db_session, user, RelationshipType.USES, openrouter, evidence=[old]
    )

    new = await make_memory(
        db_session, conversation,
        "User switched from OpenRouter to Groq.", day=10,
        kind=MemoryType.DECISION, entities=[user, groq],
    )
    new_rel = await make_relationship(
        db_session, user, RelationshipType.USES, groq, evidence=[new]
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(new)

    assert report.memories_superseded == 1
    assert report.relationships_superseded == 1

    by_content = await statuses(db_session)
    assert by_content["User uses OpenRouter for inference."] is MemoryStatus.SUPERSEDED
    assert by_content["User switched from OpenRouter to Groq."] is MemoryStatus.ACTIVE

    # The replacement relationship is still active; the old one is not.
    refreshed = (
        await db_session.execute(
            select(Relationship.target_entity_id, Relationship.status)
        )
    ).all()
    by_target = dict(refreshed)
    assert by_target[openrouter.id] is RelationshipStatus.SUPERSEDED
    assert by_target[groq.id] is RelationshipStatus.ACTIVE
    assert new_rel.id is not None


async def test_the_original_text_is_never_modified(
    db_session, settings, conversation
) -> None:
    """Supersession moves a status column and nothing else."""
    openrouter = await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    await make_entity(db_session, "Groq", EntityType.COMPANY)

    old = await make_memory(
        db_session, conversation, "User uses OpenRouter for inference.", day=0
    )
    original = (old.content, old.normalized_content,
                old.importance_score, old.confidence_score)
    original_created = old.created_at

    new = await make_memory(
        db_session, conversation, "User switched from OpenRouter to Groq.", day=10
    )
    await KnowledgeService(db_session, settings).evaluate_memory(new)

    refreshed = await db_session.get(Memory, old.id)
    await db_session.refresh(refreshed)
    assert (
        refreshed.content, refreshed.normalized_content,
        refreshed.importance_score, refreshed.confidence_score,
    ) == original
    # SQLite stores no timezone, so a read-back is naive. Compare the instant,
    # not the representation -- the stored value is unchanged either way.
    assert refreshed.created_at.replace(tzinfo=None) == (
        original_created.replace(tzinfo=None)
    )
    assert refreshed.status is MemoryStatus.SUPERSEDED
    assert openrouter.id is not None


async def test_nothing_is_ever_deleted(db_session, settings, conversation) -> None:
    await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    await make_entity(db_session, "Groq", EntityType.COMPANY)
    await make_memory(db_session, conversation, "User uses OpenRouter.", day=0)
    new = await make_memory(
        db_session, conversation, "User switched from OpenRouter to Groq.", day=10
    )

    await KnowledgeService(db_session, settings).evaluate_memory(new)

    total = (await db_session.execute(select(Memory))).scalars().all()
    assert len(total) == 2


async def test_supersession_is_traceable(
    db_session, settings, conversation
) -> None:
    """A developer must be able to answer "what replaced this, and why?"."""
    await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    await make_entity(db_session, "Groq", EntityType.COMPANY)

    old = await make_memory(db_session, conversation, "User uses OpenRouter.", day=0)
    new = await make_memory(
        db_session, conversation, "User switched from OpenRouter to Groq.", day=10
    )

    await KnowledgeService(db_session, settings).evaluate_memory(new)

    link = (
        await db_session.execute(
            select(KnowledgeConflict).where(
                KnowledgeConflict.older_memory_id == old.id
            )
        )
    ).scalars().one()

    assert link.newer_memory_id == new.id
    assert link.resolution is ConflictResolution.SUPERSEDED
    assert link.reason is ConflictReason.EXPLICIT_REPLACEMENT
    assert link.triggering_memory_id == new.id


async def test_abandonment_retires_without_naming_a_successor(
    db_session, settings, conversation
) -> None:
    openrouter = await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    user = await make_entity(db_session, "User", EntityType.PERSON)

    old = await make_memory(db_session, conversation, "User uses OpenRouter.", day=0)
    await make_relationship(
        db_session, user, RelationshipType.USES, openrouter, evidence=[old]
    )

    new = await make_memory(
        db_session, conversation, "User no longer uses OpenRouter.", day=5
    )
    report = await KnowledgeService(db_session, settings).evaluate_memory(new)

    assert report.memories_superseded == 1
    assert report.relationships_superseded == 1

    link = (
        await db_session.execute(
            select(KnowledgeConflict).where(
                KnowledgeConflict.older_relationship_id.isnot(None)
            )
        )
    ).scalars().one()
    assert link.newer_relationship_id is None, "invented a successor"
    assert link.reason is ConflictReason.EXPLICIT_ABANDONMENT
    assert link.triggering_memory_id == new.id


async def test_an_alias_resolves_to_the_canonical_entity(
    db_session, settings, conversation
) -> None:
    await make_entity(
        db_session, "PostgreSQL", EntityType.TECHNOLOGY, aliases=["Postgres"]
    )
    await make_entity(db_session, "SQLite", EntityType.TECHNOLOGY)

    old = await make_memory(db_session, conversation, "Mai uses postgres.", day=0)
    new = await make_memory(
        db_session, conversation, "Mai switched from Postgres to SQLite.", day=5
    )

    await KnowledgeService(db_session, settings).evaluate_memory(new)

    assert (await db_session.get(Memory, old.id)).status is MemoryStatus.SUPERSEDED


# --- No false conflicts -----------------------------------------------------


async def test_two_interests_both_stay_active(
    db_session, settings, conversation
) -> None:
    """INTERESTED_IN is non-exclusive. Both are true."""
    user = await make_entity(db_session, "User", EntityType.PERSON)
    ai = await make_entity(db_session, "AI", EntityType.CONCEPT)
    robotics = await make_entity(db_session, "Robotics", EntityType.CONCEPT)

    first = await make_memory(db_session, conversation, "User is interested in AI.", day=0)
    await make_relationship(
        db_session, user, RelationshipType.INTERESTED_IN, ai, evidence=[first]
    )
    second = await make_memory(
        db_session, conversation, "User is interested in Robotics.", day=5
    )
    await make_relationship(
        db_session, user, RelationshipType.INTERESTED_IN, robotics, evidence=[second]
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(second)

    assert report.conflicts_detected == 0
    assert all(
        status is MemoryStatus.ACTIVE for status in (await statuses(db_session)).values()
    )
    rows = (await db_session.execute(select(Relationship.status))).scalars().all()
    assert all(status is RelationshipStatus.ACTIVE for status in rows)


async def test_two_tools_used_together_do_not_conflict(
    db_session, settings, conversation
) -> None:
    """The design's central case: `Mai USES PostgreSQL` + `Mai USES Groq`."""
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    postgres = await make_entity(db_session, "PostgreSQL", EntityType.TECHNOLOGY)
    groq = await make_entity(db_session, "Groq", EntityType.COMPANY)

    first = await make_memory(
        db_session, conversation, "Mai uses PostgreSQL for storage.", day=0
    )
    await make_relationship(
        db_session, mai, RelationshipType.USES, postgres, evidence=[first]
    )
    second = await make_memory(
        db_session, conversation, "Mai uses Groq for inference.", day=5
    )
    await make_relationship(
        db_session, mai, RelationshipType.USES, groq, evidence=[second]
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(second)

    assert report.conflicts_detected == 0
    assert (await db_session.get(Memory, first.id)).status is MemoryStatus.ACTIVE


async def test_compatible_preferences_in_free_text_do_not_conflict(
    db_session, settings, conversation
) -> None:
    await make_entity(db_session, "Python", EntityType.TECHNOLOGY)
    await make_entity(db_session, "JavaScript", EntityType.TECHNOLOGY)

    first = await make_memory(db_session, conversation, "User likes Python.", day=0)
    second = await make_memory(
        db_session, conversation, "User enjoys JavaScript.", day=5
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(second)

    assert report.conflicts_detected == 0
    assert (await db_session.get(Memory, first.id)).status is MemoryStatus.ACTIVE


async def test_temporal_progress_is_not_treated_as_contradiction(
    db_session, settings, conversation
) -> None:
    """"working on Project A" then "completed Project A" is one story."""
    user = await make_entity(db_session, "User", EntityType.PERSON)
    project = await make_entity(db_session, "Project A", EntityType.PROJECT)

    first = await make_memory(
        db_session, conversation, "User is working on Project A.", day=0
    )
    await make_relationship(
        db_session, user, RelationshipType.WORKS_ON, project, evidence=[first]
    )
    second = await make_memory(
        db_session, conversation, "User completed Project A.", day=30,
        kind=MemoryType.EPISODIC,
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(second)

    assert report.conflicts_detected == 0
    assert (await db_session.get(Memory, first.id)).status is MemoryStatus.ACTIVE


async def test_an_unresolvable_name_supersedes_nothing(
    db_session, settings, conversation
) -> None:
    """Replacement language naming an unknown entity must abstain."""
    old = await make_memory(db_session, conversation, "User uses OpenRouter.", day=0)
    new = await make_memory(
        db_session, conversation, "User switched from Fooware to Barware.", day=5
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(new)

    assert report.conflicts_detected == 0
    assert (await db_session.get(Memory, old.id)).status is MemoryStatus.ACTIVE


async def test_a_newer_memory_cannot_be_retired_by_an_older_one(
    db_session, settings, conversation
) -> None:
    await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    await make_entity(db_session, "Groq", EntityType.COMPANY)

    new_switch = await make_memory(
        db_session, conversation, "User switched from OpenRouter to Groq.", day=0
    )
    later = await make_memory(
        db_session, conversation, "User reviewed OpenRouter pricing.", day=30
    )

    await KnowledgeService(db_session, settings).evaluate_memory(new_switch)

    assert (await db_session.get(Memory, later.id)).status is MemoryStatus.ACTIVE


async def test_a_memory_describing_the_change_is_left_alone(
    db_session, settings, conversation
) -> None:
    """A memory naming both sides is already about the change, not the old state."""
    await make_entity(db_session, "OpenRouter", EntityType.COMPANY)
    await make_entity(db_session, "Groq", EntityType.COMPANY)

    both = await make_memory(
        db_session, conversation,
        "User compared OpenRouter and Groq before deciding.", day=0,
    )
    new = await make_memory(
        db_session, conversation, "User switched from OpenRouter to Groq.", day=5
    )

    await KnowledgeService(db_session, settings).evaluate_memory(new)

    assert (await db_session.get(Memory, both.id)).status is MemoryStatus.ACTIVE


# --- Ambiguity --------------------------------------------------------------


async def test_two_locations_are_recorded_as_unresolved_not_decided(
    db_session, settings, conversation
) -> None:
    """The specification's true-contradiction case. No winner is invented."""
    user = await make_entity(db_session, "User", EntityType.PERSON)
    hyderabad = await make_entity(db_session, "Hyderabad", EntityType.PLACE)
    bangalore = await make_entity(db_session, "Bangalore", EntityType.PLACE)

    first = await make_memory(db_session, conversation, "User lives in Hyderabad.", day=0)
    old_rel = await make_relationship(
        db_session, user, RelationshipType.LOCATED_IN, hyderabad, evidence=[first]
    )
    second = await make_memory(db_session, conversation, "User lives in Bangalore.", day=5)
    await make_relationship(
        db_session, user, RelationshipType.LOCATED_IN, bangalore, evidence=[second]
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(second)

    assert report.unresolved == 1
    assert report.memories_superseded == 0
    assert report.relationships_superseded == 0

    # Both remain retrievable. The uncertainty is recorded, not resolved.
    assert (await db_session.get(Memory, first.id)).status is MemoryStatus.ACTIVE
    assert (
        await db_session.get(Relationship, old_rel.id)
    ).status is RelationshipStatus.ACTIVE

    link = (await db_session.execute(select(KnowledgeConflict))).scalars().one()
    assert link.resolution is ConflictResolution.UNRESOLVED
    assert link.reason is ConflictReason.EXCLUSIVE_AMBIGUOUS


async def test_an_explicit_present_tense_preference_does_supersede(
    db_session, settings, conversation
) -> None:
    """"now prefers hybrid" names the present, so the old preference retires."""
    user = await make_entity(db_session, "User", EntityType.PERSON)
    remote = await make_entity(db_session, "Remote Work", EntityType.CONCEPT)
    hybrid = await make_entity(db_session, "Hybrid Work", EntityType.CONCEPT)

    first = await make_memory(
        db_session, conversation, "User prefers Remote Work.", day=0,
        kind=MemoryType.PREFERENCE,
    )
    old_rel = await make_relationship(
        db_session, user, RelationshipType.PREFERS, remote, evidence=[first]
    )
    second = await make_memory(
        db_session, conversation, "User now prefers Hybrid Work.", day=30,
        kind=MemoryType.PREFERENCE,
    )
    await make_relationship(
        db_session, user, RelationshipType.PREFERS, hybrid, evidence=[second]
    )

    report = await KnowledgeService(db_session, settings).evaluate_memory(second)

    assert report.relationships_superseded == 1
    assert report.unresolved == 0
    assert (
        await db_session.get(Relationship, old_rel.id)
    ).status is RelationshipStatus.SUPERSEDED
    link = (await db_session.execute(select(KnowledgeConflict))).scalars().one()
    assert link.reason is ConflictReason.EXCLUSIVE_REPLACEMENT


# --- Database invariants ----------------------------------------------------


async def test_a_memory_cannot_supersede_itself(db_session, conversation) -> None:
    memory = await make_memory(db_session, conversation, "Only memory.", day=0)

    report = await LifecycleWriter(db_session).apply(
        [
            ConflictOutcome(
                resolution=ConflictResolution.SUPERSEDED,
                reason=ConflictReason.EXPLICIT_REPLACEMENT,
                older_memory_id=memory.id,
                newer_memory_id=memory.id,
            )
        ]
    )

    assert report.cycles_prevented == 1
    assert report.links_created == 0
    assert (await db_session.get(Memory, memory.id)).status is MemoryStatus.ACTIVE


async def test_a_supersession_cycle_is_refused(db_session, conversation) -> None:
    """A superseded B; B may not then supersede A."""
    a = await make_memory(db_session, conversation, "Memory A.", day=0)
    b = await make_memory(db_session, conversation, "Memory B.", day=5)
    writer = LifecycleWriter(db_session)

    forward = ConflictOutcome(
        resolution=ConflictResolution.SUPERSEDED,
        reason=ConflictReason.EXPLICIT_REPLACEMENT,
        older_memory_id=a.id, newer_memory_id=b.id,
    )
    assert (await writer.apply([forward])).links_created == 1

    backward = ConflictOutcome(
        resolution=ConflictResolution.SUPERSEDED,
        reason=ConflictReason.EXPLICIT_REPLACEMENT,
        older_memory_id=b.id, newer_memory_id=a.id,
    )
    report = await writer.apply([backward])

    assert report.cycles_prevented == 1
    assert report.links_created == 0
    assert (await db_session.get(Memory, b.id)).status is MemoryStatus.ACTIVE


async def test_a_longer_supersession_cycle_is_refused(
    db_session, conversation
) -> None:
    """A <- B <- C, then C -> A must be refused."""
    a = await make_memory(db_session, conversation, "Memory A.", day=0)
    b = await make_memory(db_session, conversation, "Memory B.", day=5)
    c = await make_memory(db_session, conversation, "Memory C.", day=10)
    writer = LifecycleWriter(db_session)

    for older, newer in ((a, b), (b, c)):
        await writer.apply([
            ConflictOutcome(
                resolution=ConflictResolution.SUPERSEDED,
                reason=ConflictReason.EXPLICIT_REPLACEMENT,
                older_memory_id=older.id, newer_memory_id=newer.id,
            )
        ])

    report = await writer.apply([
        ConflictOutcome(
            resolution=ConflictResolution.SUPERSEDED,
            reason=ConflictReason.EXPLICIT_REPLACEMENT,
            older_memory_id=c.id, newer_memory_id=a.id,
        )
    ])

    assert report.cycles_prevented == 1


async def test_a_duplicate_link_is_not_written_twice(
    db_session, conversation
) -> None:
    a = await make_memory(db_session, conversation, "Memory A.", day=0)
    b = await make_memory(db_session, conversation, "Memory B.", day=5)
    writer = LifecycleWriter(db_session)
    outcome = ConflictOutcome(
        resolution=ConflictResolution.SUPERSEDED,
        reason=ConflictReason.EXPLICIT_REPLACEMENT,
        older_memory_id=a.id, newer_memory_id=b.id,
    )

    first = await writer.apply([outcome])
    second = await writer.apply([outcome])

    assert first.links_created == 1
    assert second.links_created == 0
    assert second.links_already_present == 1
    links = (await db_session.execute(select(KnowledgeConflict))).scalars().all()
    assert len(links) == 1


async def test_deleting_a_memory_removes_its_lifecycle_links(
    db_session, conversation
) -> None:
    """No orphaned lifecycle references."""
    a = await make_memory(db_session, conversation, "Memory A.", day=0)
    b = await make_memory(db_session, conversation, "Memory B.", day=5)
    await LifecycleWriter(db_session).apply([
        ConflictOutcome(
            resolution=ConflictResolution.SUPERSEDED,
            reason=ConflictReason.EXPLICIT_REPLACEMENT,
            older_memory_id=a.id, newer_memory_id=b.id,
        )
    ])
    assert (await db_session.execute(select(KnowledgeConflict))).scalars().all()

    await db_session.delete(await db_session.get(Memory, a.id))
    await db_session.flush()

    assert (await db_session.execute(select(KnowledgeConflict))).scalars().all() == []


async def test_deleting_the_trigger_keeps_the_decision(
    db_session, conversation
) -> None:
    """SET NULL, not CASCADE: losing the cause must not erase the record."""
    a = await make_memory(db_session, conversation, "Memory A.", day=0)
    b = await make_memory(db_session, conversation, "Memory B.", day=5)
    trigger = await make_memory(db_session, conversation, "Trigger.", day=6)

    await LifecycleWriter(db_session).apply(
        [
            ConflictOutcome(
                resolution=ConflictResolution.SUPERSEDED,
                reason=ConflictReason.EXPLICIT_REPLACEMENT,
                older_memory_id=a.id, newer_memory_id=b.id,
            )
        ],
        triggering_memory_id=trigger.id,
    )

    await db_session.delete(await db_session.get(Memory, trigger.id))
    await db_session.flush()

    link = (await db_session.execute(select(KnowledgeConflict))).scalars().one()
    assert link.triggering_memory_id is None
    assert link.older_memory_id == a.id


async def test_a_link_cannot_mix_memories_and_relationships(
    db_session, conversation
) -> None:
    """The check constraint is the last line of defence."""
    from sqlalchemy.exc import IntegrityError

    memory = await make_memory(db_session, conversation, "Memory.", day=0)
    source = await make_entity(db_session, "A", EntityType.OTHER)
    target = await make_entity(db_session, "B", EntityType.OTHER)
    relationship = await make_relationship(
        db_session, source, RelationshipType.USES, target
    )

    db_session.add(
        KnowledgeConflict(
            older_memory_id=memory.id,
            older_relationship_id=relationship.id,
            resolution=ConflictResolution.SUPERSEDED,
            reason=ConflictReason.EXPLICIT_REPLACEMENT,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
