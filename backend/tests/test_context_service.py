"""Context service: real sources, failure isolation, read-only guarantee."""

import uuid

import pytest
from sqlalchemy import event, func, select

from app.context.service import ContextService
from app.entities.models import Entity, EntityStatus, EntityType, MemoryEntity
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)
from app.services.conversation_service import ConversationService


@pytest.fixture
async def conversation(db_session):
    return await ConversationService(db_session).create_conversation()


async def seed_knowledge(db_session, conversation):
    """Entities, memories and a relationship the retrieval layer can find."""
    mai = Entity(canonical_name="Mai", normalized_name="mai",
                 entity_type=EntityType.PROJECT, status=EntityStatus.ACTIVE)
    postgres = Entity(canonical_name="PostgreSQL", normalized_name="postgresql",
                      entity_type=EntityType.TECHNOLOGY, status=EntityStatus.ACTIVE)
    db_session.add_all([mai, postgres])
    await db_session.flush()

    memories = []
    for index, (content, kind) in enumerate([
        ("User is building Mai as a personal AI environment.", MemoryType.SEMANTIC),
        ("User decided to use PostgreSQL for Mai.", MemoryType.DECISION),
    ]):
        memory = Memory(
            content=content, normalized_content=content.lower().rstrip("."),
            memory_type=kind, status=MemoryStatus.ACTIVE,
            importance_score=8, confidence_score=0.95,
            source_conversation_id=conversation.id,
        )
        db_session.add(memory)
        await db_session.flush()
        db_session.add(MemoryEntity(memory_id=memory.id, entity_id=mai.id))
        memories.append(memory)

    relationship = Relationship(
        source_entity_id=mai.id, relationship_type=RelationshipType.USES,
        target_entity_id=postgres.id, confidence_score=0.95,
        status=RelationshipStatus.ACTIVE,
    )
    db_session.add(relationship)
    await db_session.flush()
    db_session.add(RelationshipEvidence(
        relationship_id=relationship.id, memory_id=memories[1].id
    ))
    await db_session.flush()
    return mai, postgres


async def add_messages(db_session, conversation, count):
    from app.database.models import MessageRole

    service = ConversationService(db_session)
    for index in range(count):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await service.add_message(conversation.id, role, f"turn {index}")


def service(db_session, settings) -> ContextService:
    return ContextService(session=db_session, settings=settings)


# --- Real end-to-end assembly -----------------------------------------------


async def test_package_combines_all_three_sources(
    db_session, settings, conversation
) -> None:
    await seed_knowledge(db_session, conversation)
    await add_messages(db_session, conversation, 4)

    package = await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert package.current_message == "What database does Mai use?"
    assert package.has_recent_conversation
    assert package.has_long_term_knowledge
    assert package.metadata.conversation_id == conversation.id
    assert package.metadata.characters.total > 0
    assert package.metadata.duration_ms >= 0


async def test_recent_message_limit_is_enforced_against_the_database(
    db_session, settings, conversation
) -> None:
    settings.CONTEXT_RECENT_MESSAGE_LIMIT = 5
    await add_messages(db_session, conversation, 20)

    package = await service(db_session, settings).build(
        current_message="anything", conversation_id=conversation.id
    )

    assert len(package.recent_conversation) == 5
    # The latest five, chronologically ordered.
    assert [m.content for m in package.recent_conversation] == [
        "turn 15", "turn 16", "turn 17", "turn 18", "turn 19"
    ]


async def test_without_a_conversation_id_only_long_term_knowledge_appears(
    db_session, settings, conversation
) -> None:
    await seed_knowledge(db_session, conversation)

    package = await service(db_session, settings).build(
        current_message="What database does Mai use?", conversation_id=None
    )

    assert not package.has_recent_conversation
    assert package.has_long_term_knowledge


# --- Failure isolation ------------------------------------------------------


async def test_retrieval_failure_leaves_message_and_conversation(
    db_session, settings, conversation, monkeypatch
) -> None:
    await add_messages(db_session, conversation, 3)

    async def boom(*args, **kwargs):
        raise OSError("retrieval subsystem is down")

    monkeypatch.setattr("app.retrieval.service.RetrievalService.retrieve", boom)

    package = await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert package.current_message == "What database does Mai use?"
    assert package.has_recent_conversation
    assert not package.has_long_term_knowledge
    assert "long_term_knowledge" in package.metadata.degraded_sources


async def test_conversation_failure_leaves_message_and_knowledge(
    db_session, settings, conversation, monkeypatch
) -> None:
    await seed_knowledge(db_session, conversation)

    async def boom(*args, **kwargs):
        raise OSError("message table unavailable")

    monkeypatch.setattr(
        "app.services.conversation_service.ConversationService.get_messages", boom
    )

    package = await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert package.current_message == "What database does Mai use?"
    assert not package.has_recent_conversation
    assert package.has_long_term_knowledge
    assert "recent_conversation" in package.metadata.degraded_sources


async def test_all_optional_sources_failing_still_yields_a_valid_package(
    db_session, settings, conversation, monkeypatch
) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("everything is down")

    monkeypatch.setattr("app.retrieval.service.RetrievalService.retrieve", boom)
    monkeypatch.setattr(
        "app.services.conversation_service.ConversationService.get_messages", boom
    )

    package = await service(db_session, settings).build(
        current_message="The user's message must always survive.",
        conversation_id=conversation.id,
    )

    assert package.current_message == "The user's message must always survive."
    assert package.is_minimal
    assert set(package.metadata.degraded_sources) == {
        "recent_conversation", "long_term_knowledge"
    }


async def test_build_never_raises_for_an_unknown_conversation(
    db_session, settings
) -> None:
    package = await service(db_session, settings).build(
        current_message="hello", conversation_id=uuid.uuid4()
    )
    assert package.current_message == "hello"


# --- Read-only guarantee ----------------------------------------------------


async def test_assembly_mutates_nothing(
    db_session, settings, conversation
) -> None:
    """Context assembly is a read-and-transform layer.

    Everything stays inside one transaction: assembly writes nothing, so there
    is nothing to commit, and avoiding commit/rollback churn keeps the shared
    test connection in a clean state.
    """
    await seed_knowledge(db_session, conversation)
    await add_messages(db_session, conversation, 4)

    async def snapshot():
        counts = {}
        for model in (Memory, Entity, MemoryEntity, Relationship, RelationshipEvidence):
            result = await db_session.execute(select(func.count()).select_from(model))
            counts[model.__name__] = result.scalar_one()
        # Columns, not ORM instances: instances would need a refresh after any
        # expiry, which cannot run outside async context.
        rows = [
            tuple(row)
            for row in (
                await db_session.execute(
                    select(Memory.id, Memory.content, Memory.updated_at).order_by(
                        Memory.id
                    )
                )
            ).all()
        ]
        return counts, rows

    before = await snapshot()

    await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    # Row counts unchanged, and no memory's content or updated_at was touched.
    assert await snapshot() == before


async def test_assembly_issues_no_write_statements(
    db_session, settings, conversation
) -> None:
    await seed_knowledge(db_session, conversation)
    await add_messages(db_session, conversation, 3)
    await db_session.commit()

    writes = []
    engine = db_session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, params, context, executemany):
        head = statement.strip().upper()
        if head.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    try:
        await service(db_session, settings).build(
            current_message="What database does Mai use?",
            conversation_id=conversation.id,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert writes == [], f"assembly issued writes: {writes}"


# --- No model calls ---------------------------------------------------------


async def test_assembly_makes_no_model_call(
    db_session, settings, conversation, fake_provider
) -> None:
    await seed_knowledge(db_session, conversation)

    await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert fake_provider.calls == []
    assert fake_provider.extraction_calls == []
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []


def test_the_context_package_never_imports_the_llm_layer() -> None:
    """Structural guarantee, not a behavioural one."""
    import ast
    import pathlib

    for path in pathlib.Path("app/context").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            if module and "app.llm" in module:
                raise AssertionError(f"{path} imports the LLM layer: {module}")


# --- Stage 2D is consumed, not duplicated -----------------------------------


async def test_retrieval_is_called_exactly_once(
    db_session, settings, conversation, monkeypatch
) -> None:
    """Stage 3A must not re-run retrieval."""
    from app.retrieval.service import RetrievalService

    calls = {"n": 0}
    original = RetrievalService.retrieve

    async def counting(self, query):
        calls["n"] += 1
        return await original(self, query)

    monkeypatch.setattr(RetrievalService, "retrieve", counting)
    await seed_knowledge(db_session, conversation)

    await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert calls["n"] == 1


async def test_query_count_stays_bounded_with_a_large_dataset(
    db_session, settings, conversation
) -> None:
    """No N+1: assembly adds a fixed number of queries on top of retrieval."""
    await seed_knowledge(db_session, conversation)
    await add_messages(db_session, conversation, 120)

    for index in range(120):
        content = f"User noted detail number {index} about PostgreSQL."
        db_session.add(Memory(
            content=content, normalized_content=content.lower().rstrip("."),
            memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
            importance_score=5, confidence_score=0.8,
            source_conversation_id=conversation.id,
        ))
    await db_session.commit()

    statements = []
    engine = db_session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    try:
        package = await service(db_session, settings).build(
            current_message="What database does Mai use?",
            conversation_id=conversation.id,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Stage 2D's eight, plus one for recent conversation.
    assert len(statements) <= 10, f"too many queries: {len(statements)}"
    assert len(package.recent_conversation) <= settings.CONTEXT_RECENT_MESSAGE_LIMIT
    assert package.metadata.characters.total <= settings.CONTEXT_MAX_TOTAL_CHARS


# --- Configuration ----------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "value", "attribute"),
    [
        ("CONTEXT_MAX_MEMORY_ITEMS", 1, "memories"),
        ("CONTEXT_MAX_ENTITY_ITEMS", 1, "entities"),
        ("CONTEXT_MAX_RELATIONSHIP_ITEMS", 0, "relationships"),
    ],
)
async def test_category_limits_change_behaviour(
    db_session, settings, conversation, setting, value, attribute
) -> None:
    await seed_knowledge(db_session, conversation)
    setattr(settings, setting, value)

    package = await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert len(getattr(package, attribute)) <= value


async def test_total_budget_changes_behaviour(
    db_session, settings, conversation
) -> None:
    await seed_knowledge(db_session, conversation)
    await add_messages(db_session, conversation, 10)
    settings.CONTEXT_MAX_TOTAL_CHARS = 150

    package = await service(db_session, settings).build(
        current_message="What database does Mai use?",
        conversation_id=conversation.id,
    )

    assert package.metadata.characters.total <= 150
    assert package.current_message == "What database does Mai use?"


async def test_budget_limits_are_reported_in_metadata(
    db_session, settings, conversation
) -> None:
    settings.CONTEXT_MAX_MEMORY_ITEMS = 4
    package = await service(db_session, settings).build(
        current_message="anything", conversation_id=conversation.id
    )
    assert package.metadata.budget.max_memory_items == 4
    assert package.metadata.budget.max_total_chars == settings.CONTEXT_MAX_TOTAL_CHARS
