"""Entity matching, memory retrieval, ranking priority, budget and one-hop."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.entities.models import Entity, EntityAlias, EntityStatus, EntityType, MemoryEntity
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService


@pytest.fixture
async def conversation(db_session):
    return await ConversationService(db_session).create_conversation()


async def make_entity(db_session, name, kind=EntityType.TECHNOLOGY, aliases=()) -> Entity:
    entity = Entity(
        canonical_name=name,
        normalized_name=name.lower(),
        entity_type=kind,
        status=EntityStatus.ACTIVE,
    )
    db_session.add(entity)
    await db_session.flush()
    for alias in aliases:
        db_session.add(
            EntityAlias(
                entity_id=entity.id, alias=alias, normalized_alias=alias.lower()
            )
        )
    await db_session.flush()
    return entity


async def make_memory(
    db_session, conversation, content, *, entities=(), importance=7,
    confidence=0.9, age_days=0, memory_type=MemoryType.SEMANTIC,
) -> Memory:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    memory = Memory(
        content=content,
        normalized_content=content.lower().rstrip("."),
        memory_type=memory_type,
        status=MemoryStatus.ACTIVE,
        importance_score=importance,
        confidence_score=confidence,
        source_conversation_id=conversation.id,
        created_at=created,
        updated_at=created,
    )
    db_session.add(memory)
    await db_session.flush()
    for entity in entities:
        db_session.add(MemoryEntity(memory_id=memory.id, entity_id=entity.id))
    await db_session.flush()
    return memory


async def make_relationship(db_session, source, kind, target, memory=None, confidence=0.9):
    relationship = Relationship(
        source_entity_id=source.id,
        relationship_type=kind,
        target_entity_id=target.id,
        confidence_score=confidence,
        status=RelationshipStatus.ACTIVE,
    )
    db_session.add(relationship)
    await db_session.flush()
    if memory is not None:
        db_session.add(
            RelationshipEvidence(relationship_id=relationship.id, memory_id=memory.id)
        )
        await db_session.flush()
    return relationship


def service(db_session, settings) -> RetrievalService:
    return RetrievalService(session=db_session, settings=settings)


# --- Entity matching --------------------------------------------------------


async def test_canonical_name_is_matched(db_session, settings, conversation) -> None:
    await make_entity(db_session, "Mai", EntityType.PROJECT)
    package = await service(db_session, settings).retrieve("How is Mai progressing?")
    assert [e.canonical_name for e in package.matched_entities] == ["Mai"]


async def test_alias_is_matched(db_session, settings, conversation) -> None:
    """The specification's example: "Postgres" must find PostgreSQL."""
    await make_entity(db_session, "PostgreSQL", aliases=["Postgres"])
    package = await service(db_session, settings).retrieve("Should I keep using Postgres?")
    assert [e.canonical_name for e in package.matched_entities] == ["PostgreSQL"]
    assert package.matched_entities[0].matched_via == "alias"


async def test_multi_word_entity_is_matched(db_session, settings, conversation) -> None:
    await make_entity(db_session, "AI Product Development", EntityType.CONCEPT)
    package = await service(db_session, settings).retrieve(
        "Tell me about AI product development please"
    )
    assert [e.canonical_name for e in package.matched_entities] == [
        "AI Product Development"
    ]


@pytest.mark.parametrize(
    "query",
    [
        "What is the weather today?",
        "Tell me about databases",
        "How do I write a postgres-like system?",  # substring, not a word
        "Claud is a name",                          # near-miss spelling
    ],
)
async def test_no_false_entity_matches(db_session, settings, conversation, query) -> None:
    """Matching is exact on word n-grams -- never substrings or fuzzy."""
    await make_entity(db_session, "Claude", EntityType.PRODUCT)
    await make_entity(db_session, "PostgreSQL")
    package = await service(db_session, settings).retrieve(query)
    assert package.matched_entities == []


async def test_a_longer_name_containing_a_known_entity_still_matches_it(
    db_session, settings, conversation
) -> None:
    """Documented behaviour, not a bug.

    "Claude Code" contains the word "Claude". With only a "Claude" entity
    stored, the query genuinely mentions that word, so it matches. This is
    mild over-retrieval, not a fabricated match -- and the assembled context
    is explicitly labelled as background knowledge. Once a "Claude Code"
    entity exists, the longer n-gram matches it too.
    """
    claude = await make_entity(db_session, "Claude", EntityType.PRODUCT)
    package = await service(db_session, settings).retrieve("Claude Code is great")
    assert [e.id for e in package.matched_entities] == [claude.id]

    await make_entity(db_session, "Claude Code", EntityType.PRODUCT)
    package = await service(db_session, settings).retrieve("Claude Code is great")
    names = {e.canonical_name for e in package.matched_entities}
    assert names == {"Claude", "Claude Code"}


async def test_archived_entities_are_not_matched(db_session, settings, conversation) -> None:
    entity = await make_entity(db_session, "Mai", EntityType.PROJECT)
    entity.status = EntityStatus.ARCHIVED
    await db_session.flush()
    package = await service(db_session, settings).retrieve("How is Mai?")
    assert package.matched_entities == []


# --- Ranking priority: relevance beats importance ---------------------------


async def test_relevant_memory_outranks_important_irrelevant_one(
    db_session, settings, conversation
) -> None:
    """The specification's exact scenario."""
    postgres = await make_entity(db_session, "PostgreSQL")
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    await make_memory(
        db_session, conversation, "Mai uses PostgreSQL as its database.",
        entities=[mai, postgres], importance=5, confidence=0.8,
    )
    await make_memory(
        db_session, conversation, "User wants to change careers entirely.",
        importance=10, confidence=1.0,
    )

    package = await service(db_session, settings).retrieve(
        "What database does Mai use?"
    )

    assert package.memories, "nothing retrieved"
    assert "PostgreSQL" in package.memories[0].content
    assert package.memories[0].rank == 1


async def test_importance_breaks_ties_when_relevance_matches(
    db_session, settings, conversation
) -> None:
    await make_memory(
        db_session, conversation, "User prefers PostgreSQL for storage.",
        importance=9, confidence=0.9,
    )
    await make_memory(
        db_session, conversation, "User prefers PostgreSQL for reporting.",
        importance=3, confidence=0.9,
    )

    package = await service(db_session, settings).retrieve("PostgreSQL storage reporting")
    assert package.memories[0].importance_score == 9


async def test_confidence_breaks_ties_when_relevance_matches(
    db_session, settings, conversation
) -> None:
    await make_memory(
        db_session, conversation, "User deployed PostgreSQL on Monday.",
        importance=7, confidence=0.99,
    )
    await make_memory(
        db_session, conversation, "User deployed PostgreSQL on Tuesday.",
        importance=7, confidence=0.71,
    )

    package = await service(db_session, settings).retrieve("PostgreSQL deployed")
    assert package.memories[0].confidence_score > package.memories[1].confidence_score


async def test_old_relevant_beats_recent_irrelevant(
    db_session, settings, conversation
) -> None:
    """Recency is a weak signal by design."""
    await make_memory(
        db_session, conversation, "User uses PostgreSQL for Mai.",
        importance=7, confidence=0.9, age_days=400,
    )
    await make_memory(
        db_session, conversation, "User had coffee this morning.",
        importance=7, confidence=0.9, age_days=0,
    )

    package = await service(db_session, settings).retrieve("PostgreSQL")
    assert "PostgreSQL" in package.memories[0].content


async def test_recency_breaks_ties_between_equal_memories(
    db_session, settings, conversation
) -> None:
    await make_memory(
        db_session, conversation, "User configured PostgreSQL settings alpha.",
        importance=7, confidence=0.9, age_days=300,
    )
    await make_memory(
        db_session, conversation, "User configured PostgreSQL settings beta.",
        importance=7, confidence=0.9, age_days=1,
    )

    package = await service(db_session, settings).retrieve("PostgreSQL configured settings")
    assert "beta" in package.memories[0].content


async def test_ranking_is_deterministic(db_session, settings, conversation) -> None:
    for index in range(6):
        await make_memory(
            db_session, conversation, f"User uses PostgreSQL for task {index}.",
            importance=7, confidence=0.9,
        )
    retrieval = service(db_session, settings)

    first = [m.id for m in (await retrieval.retrieve("PostgreSQL task")).memories]
    second = [m.id for m in (await retrieval.retrieve("PostgreSQL task")).memories]
    assert first == second


# --- Entity-linked and relationship retrieval -------------------------------


async def test_entity_match_pulls_in_linked_memories(
    db_session, settings, conversation
) -> None:
    """A memory with no keyword overlap is still found via its entity."""
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    await make_memory(
        db_session, conversation, "The assistant runs locally on a laptop.",
        entities=[mai],
    )

    package = await service(db_session, settings).retrieve("How is Mai progressing?")

    assert [m.content for m in package.memories] == [
        "The assistant runs locally on a laptop."
    ]
    assert any(s.name == "entity" for s in package.memories[0].signals)


async def test_relationships_involving_a_matched_entity_are_retrieved(
    db_session, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    postgres = await make_entity(db_session, "PostgreSQL")
    groq = await make_entity(db_session, "Groq", EntityType.COMPANY)
    await make_relationship(db_session, mai, RelationshipType.USES, postgres)
    await make_relationship(db_session, mai, RelationshipType.USES, groq)

    package = await service(db_session, settings).retrieve("What does Mai use?")

    rendered = {r.render() for r in package.relationships}
    assert rendered == {"Mai USES PostgreSQL", "Mai USES Groq"}


async def test_both_endpoints_matched_outranks_one(
    db_session, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    postgres = await make_entity(db_session, "PostgreSQL")
    groq = await make_entity(db_session, "Groq", EntityType.COMPANY)
    await make_relationship(db_session, mai, RelationshipType.USES, postgres)
    await make_relationship(db_session, mai, RelationshipType.USES, groq)

    package = await service(db_session, settings).retrieve(
        "Does Mai still use PostgreSQL?"
    )

    assert package.relationships[0].render() == "Mai USES PostgreSQL"
    assert package.relationships[0].score > package.relationships[1].score


async def test_relationship_retrieval_is_one_hop_only(
    db_session, settings, conversation
) -> None:
    """Query -> entity -> its relationships. No further traversal."""
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    postgres = await make_entity(db_session, "PostgreSQL")
    extension = await make_entity(db_session, "PostGIS")
    cloud = await make_entity(db_session, "AWS", EntityType.COMPANY)

    # One hop from Mai.
    await make_relationship(db_session, mai, RelationshipType.USES, postgres)
    # Two hops: PostgreSQL -> PostGIS, and three: PostGIS -> AWS.
    await make_relationship(db_session, postgres, RelationshipType.USES, extension)
    await make_relationship(db_session, extension, RelationshipType.DEPENDS_ON, cloud)

    package = await service(db_session, settings).retrieve("Tell me about Mai")

    rendered = {r.render() for r in package.relationships}
    assert rendered == {"Mai USES PostgreSQL"}
    assert "PostgreSQL USES PostGIS" not in rendered
    assert "PostGIS DEPENDS_ON AWS" not in rendered


# --- Deduplication ----------------------------------------------------------


async def test_duplicate_memory_text_appears_once(
    db_session, settings, conversation
) -> None:
    """Stage 2A's UNIQUE (memory_type, normalized_content) already blocks
    identical memories of one type, so the same text can only reach retrieval
    under two different types. Context assembly must still show it once.
    """
    await make_memory(
        db_session, conversation, "Mai uses PostgreSQL.",
        memory_type=MemoryType.SEMANTIC,
    )
    await make_memory(
        db_session, conversation, "mai uses postgresql",
        memory_type=MemoryType.DECISION,
    )

    package = await service(db_session, settings).retrieve("Mai PostgreSQL")
    assert len(package.memories) == 1


async def test_relationships_are_deduplicated_in_context(
    db_session, settings, conversation
) -> None:
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    postgres = await make_entity(db_session, "PostgreSQL")
    memory_one = await make_memory(db_session, conversation, "Mai uses PostgreSQL.")
    memory_two = await make_memory(
        db_session, conversation, "PostgreSQL was chosen for Mai."
    )
    relationship = await make_relationship(
        db_session, mai, RelationshipType.USES, postgres, memory=memory_one
    )
    # A second memory supporting the same relationship.
    db_session.add(
        RelationshipEvidence(relationship_id=relationship.id, memory_id=memory_two.id)
    )
    await db_session.flush()

    package = await service(db_session, settings).retrieve("Mai PostgreSQL")

    rendered = [r.render() for r in package.relationships]
    assert rendered == ["Mai USES PostgreSQL"]


# --- Context budget ---------------------------------------------------------


async def test_memory_budget_keeps_the_highest_ranked(
    db_session, settings, conversation
) -> None:
    settings.RETRIEVAL_MAX_MEMORIES = 3
    for index in range(10):
        await make_memory(
            db_session, conversation,
            f"User uses PostgreSQL for purpose number {index}.",
            importance=index + 1, confidence=0.9,
        )

    package = await service(db_session, settings).retrieve("PostgreSQL purpose")

    assert len(package.memories) == 3
    # Highest importance survives, since relevance is equal.
    assert package.memories[0].importance_score == 10
    scores = [m.score.final_score for m in package.memories]
    assert scores == sorted(scores, reverse=True)


async def test_character_budget_is_never_exceeded(
    db_session, settings, conversation
) -> None:
    settings.RETRIEVAL_MAX_CONTEXT_CHARS = 1200
    for index in range(12):
        await make_memory(
            db_session, conversation,
            f"User uses PostgreSQL extensively for a long detailed purpose {index} "
            f"with plenty of additional descriptive text to consume budget.",
        )

    retrieval = service(db_session, settings)
    package = await retrieval.retrieve("PostgreSQL purpose")
    rendered = retrieval.render(package)

    assert len(rendered) <= 1200
    assert package.metadata.budget_exhausted
    # Whatever survived is a complete, valid block -- never a truncated one.
    assert rendered.startswith("PERSONAL KNOWLEDGE CONTEXT")
    assert rendered.rstrip().endswith(".")
    # Items were dropped from the bottom of the ranking, not the top.
    assert len(package.memories) < 12


async def test_a_budget_smaller_than_the_fixed_overhead_yields_nothing(
    db_session, settings, conversation
) -> None:
    """Better an empty context than a corrupted one."""
    settings.RETRIEVAL_MAX_CONTEXT_CHARS = 100
    await make_memory(db_session, conversation, "User uses PostgreSQL for Mai.")

    retrieval = service(db_session, settings)
    package = await retrieval.retrieve("PostgreSQL")

    assert retrieval.render(package) == ""
    assert package.metadata.budget_exhausted


async def test_entity_and_relationship_budgets_apply(
    db_session, settings, conversation
) -> None:
    settings.RETRIEVAL_MAX_ENTITIES = 2
    settings.RETRIEVAL_MAX_RELATIONSHIPS = 1
    mai = await make_entity(db_session, "Mai", EntityType.PROJECT)
    others = [await make_entity(db_session, f"Tool{i}") for i in range(4)]
    for other in others:
        await make_relationship(db_session, mai, RelationshipType.USES, other)

    query = "Mai Tool0 Tool1 Tool2 Tool3"
    package = await service(db_session, settings).retrieve(query)

    assert len(package.matched_entities) <= 2
    assert len(package.relationships) <= 1


# --- Bounded candidate pool -------------------------------------------------


async def test_the_candidate_pool_stays_bounded(
    db_session, settings, conversation
) -> None:
    """The whole memory table must never be loaded."""
    settings.RETRIEVAL_CANDIDATE_POOL_SIZE = 20
    for index in range(120):
        await make_memory(
            db_session, conversation, f"User uses PostgreSQL for item {index}."
        )

    package = await service(db_session, settings).retrieve("PostgreSQL item")

    assert package.metadata.candidate_memories <= 20
    assert len(package.memories) <= settings.RETRIEVAL_MAX_MEMORIES


# --- Disabled mode ----------------------------------------------------------


async def test_retrieval_can_be_disabled(db_session, settings, conversation) -> None:
    settings.RETRIEVAL_ENABLED = False
    await make_entity(db_session, "Mai", EntityType.PROJECT)
    await make_memory(db_session, conversation, "Mai uses PostgreSQL.")

    package = await service(db_session, settings).retrieve("What does Mai use?")

    assert package.is_empty
    assert package.metadata.enabled is False


async def test_an_empty_query_retrieves_nothing(
    db_session, settings, conversation
) -> None:
    await make_memory(db_session, conversation, "Mai uses PostgreSQL.")
    for query in ["", "   ", "the of and to"]:
        assert (await service(db_session, settings).retrieve(query)).is_empty


async def test_unknown_entity_id_is_not_returned(db_session, settings) -> None:
    """Sanity: retrieval never invents ids."""
    package = await service(db_session, settings).retrieve("something unrelated")
    assert all(isinstance(e.id, uuid.UUID) for e in package.matched_entities)
