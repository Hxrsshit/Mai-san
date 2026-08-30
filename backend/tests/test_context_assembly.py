"""Context assembly: structure, priority, ordering and budgets.

These exercise the assembler directly with synthetic retrieval results, so the
budgeting and ordering guarantees are tested without a database.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.context.assembler import ContextAssembler
from app.context.budget import BudgetLimits, ContextBudgeter, character_sizer
from app.context.schemas import ContextRole
from app.retrieval.schemas import (
    RetrievalResult,
    RetrievedEntity,
    RetrievedMemory,
    RetrievedRelationship,
    ScoreBreakdown,
)


def limits(**overrides) -> BudgetLimits:
    base = dict(
        recent_message_limit=12, max_memory_items=10, max_entity_items=10,
        max_relationship_items=10, max_total_chars=10000,
    )
    base.update(overrides)
    return BudgetLimits(**base)


class FakeMessage:
    """Stands in for an ORM Message row."""

    def __init__(self, role, content, minutes_ago=0):
        self.role = role
        self.content = content
        self.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def memory(content, rank, score=0.9, importance=7, confidence=0.9) -> RetrievedMemory:
    return RetrievedMemory(
        id=uuid.uuid4(), content=content, memory_type="semantic",
        importance_score=importance, confidence_score=confidence,
        created_at=datetime.now(timezone.utc),
        score=ScoreBreakdown(final_score=score), rank=rank,
    )


def entity(name, rank, kind="technology", description=None) -> RetrievedEntity:
    return RetrievedEntity(
        id=uuid.uuid4(), canonical_name=name, entity_type=kind,
        description=description, match_strength=1.0, matched_via="canonical",
        matched_text=name.lower(), rank=rank,
    )


def relationship(source, kind, target, rank, score=0.9) -> RetrievedRelationship:
    return RetrievedRelationship(
        id=uuid.uuid4(), source_name=source, relationship_type=kind,
        target_name=target, confidence_score=0.9, score=score, rank=rank,
    )


def result(memories=(), entities=(), relationships=()) -> RetrievalResult:
    return RetrievalResult(
        query="q", memories=list(memories), matched_entities=list(entities),
        relationships=list(relationships),
    )


def assemble(current="What stack am I using?", messages=(), retrieval=None, **limit_overrides):
    return ContextAssembler(limits(**limit_overrides)).assemble(
        current_message=current, recent_messages=messages, retrieval=retrieval
    )


# --- Structure --------------------------------------------------------------


def test_categories_stay_separate() -> None:
    """Short-term and long-term context must not be flattened together."""
    package = assemble(
        messages=[FakeMessage("user", "earlier"), FakeMessage("assistant", "reply")],
        retrieval=result(
            memories=[memory("User uses PostgreSQL.", 1)],
            entities=[entity("PostgreSQL", 1)],
            relationships=[relationship("Mai", "USES", "PostgreSQL", 1)],
        ),
    )

    assert package.current_message == "What stack am I using?"
    assert len(package.recent_conversation) == 2
    assert len(package.memories) == 1
    assert len(package.entities) == 1
    assert len(package.relationships) == 1
    # Each category is its own list, not merged.
    assert package.has_recent_conversation and package.has_long_term_knowledge


def test_retrieved_knowledge_is_marked_as_reference_not_instruction() -> None:
    """Retrieved content must never be usable as an instruction."""
    package = assemble(
        messages=[FakeMessage("user", "hi")],
        retrieval=result(
            memories=[memory("User uses PostgreSQL.", 1)],
            entities=[entity("PostgreSQL", 1)],
            relationships=[relationship("Mai", "USES", "PostgreSQL", 1)],
        ),
    )

    for item in [*package.memories, *package.entities, *package.relationships]:
        assert item.context_role is ContextRole.REFERENCE
    for message in package.recent_conversation:
        assert message.context_role is ContextRole.CONVERSATION
    # Nothing in the package is marked as an instruction.
    assert not any(
        getattr(i, "context_role", None) is ContextRole.INSTRUCTION
        for i in [*package.memories, *package.entities, *package.relationships,
                  *package.recent_conversation]
    )


# --- Current message priority -----------------------------------------------


def test_the_current_message_is_preserved_exactly() -> None:
    """Never normalized, rewritten or truncated."""
    original = "  I switched to Groq!!!  "
    package = assemble(current=original, retrieval=result(
        memories=[memory("User uses OpenRouter.", 1)]
    ))
    assert package.current_message == original


def test_old_knowledge_does_not_replace_the_current_message() -> None:
    """The specification's exact scenario."""
    package = assemble(
        current="I switched to Groq.",
        retrieval=result(memories=[memory("User uses OpenRouter.", 1)]),
    )

    assert package.current_message == "I switched to Groq."
    # The stale memory is present as reference, clearly separate.
    assert package.memories[0].content == "User uses OpenRouter."
    assert package.memories[0].context_role is ContextRole.REFERENCE


def test_the_current_message_survives_an_impossible_budget() -> None:
    package = assemble(
        current="A message longer than the entire budget allows.",
        messages=[FakeMessage("user", "x" * 500)],
        retrieval=result(memories=[memory("y" * 500, 1)]),
        max_total_chars=10,
    )

    assert package.current_message == "A message longer than the entire budget allows."
    assert package.is_minimal
    assert package.metadata.dropped_count > 0


# --- Recent conversation ----------------------------------------------------


def test_recent_messages_keep_chronological_order() -> None:
    messages = [FakeMessage("user", f"message {i}", minutes_ago=10 - i) for i in range(6)]
    package = assemble(messages=messages)

    assert [m.content for m in package.recent_conversation] == [
        f"message {i}" for i in range(6)
    ]


def test_recent_message_limit_keeps_the_latest() -> None:
    """The oldest turns are dropped, not the newest."""
    messages = [FakeMessage("user", f"message {i}") for i in range(10)]
    package = assemble(messages=messages, recent_message_limit=5)

    assert len(package.recent_conversation) == 5
    assert [m.content for m in package.recent_conversation] == [
        "message 5", "message 6", "message 7", "message 8", "message 9"
    ]
    dropped = [d for d in package.metadata.dropped_items if d.category == "recent_message"]
    assert len(dropped) == 5


def test_message_roles_and_timestamps_are_preserved() -> None:
    package = assemble(messages=[FakeMessage("assistant", "hello there")])
    message = package.recent_conversation[0]
    assert message.role == "assistant"
    assert message.content == "hello there"
    assert message.created_at is not None


# --- Ranking order preserved ------------------------------------------------


def test_stage_2d_ranking_order_is_preserved() -> None:
    """Stage 3A must not reorder what Stage 2D ranked."""
    memories = [memory(f"memory {i}", rank=i, score=1.0 - i * 0.1) for i in range(1, 6)]
    package = assemble(retrieval=result(memories=memories))

    assert [m.content for m in package.memories] == [f"memory {i}" for i in range(1, 6)]
    assert [m.retrieval_rank for m in package.memories] == [1, 2, 3, 4, 5]


def test_retrieval_scores_are_carried_through_not_recomputed() -> None:
    package = assemble(retrieval=result(memories=[memory("m", 1, score=0.7331)]))
    assert package.memories[0].retrieval_score == pytest.approx(0.7331)


def test_relationships_connecting_matched_entities_are_flagged() -> None:
    """Flagged for Stage 3B, but the order stays Stage 2D's."""
    package = assemble(retrieval=result(
        entities=[entity("Mai", 1, "project"), entity("PostgreSQL", 2)],
        relationships=[
            relationship("Mai", "USES", "PostgreSQL", 1),
            relationship("Mai", "USES", "Redis", 2),
        ],
    ))

    assert package.relationships[0].connects_matched_entities is True
    assert package.relationships[1].connects_matched_entities is False
    # Order unchanged.
    assert [r.retrieval_rank for r in package.relationships] == [1, 2]


# --- Category limits --------------------------------------------------------


def test_category_limits_keep_the_highest_ranked() -> None:
    package = assemble(
        retrieval=result(
            memories=[memory(f"memory {i}", i) for i in range(1, 7)],
            entities=[entity(f"Entity{i}", i) for i in range(1, 6)],
            relationships=[relationship("A", "USES", f"B{i}", i) for i in range(1, 6)],
        ),
        max_memory_items=3, max_entity_items=2, max_relationship_items=2,
    )

    assert len(package.memories) == 3
    assert len(package.entities) == 2
    assert len(package.relationships) == 2
    assert [m.retrieval_rank for m in package.memories] == [1, 2, 3]
    assert [e.retrieval_rank for e in package.entities] == [1, 2]


def test_category_limits_are_independent() -> None:
    """One category cannot consume another's allowance."""
    package = assemble(
        retrieval=result(
            memories=[memory(f"m{i}", i) for i in range(1, 21)],
            entities=[entity(f"E{i}", i) for i in range(1, 3)],
        ),
        max_memory_items=5, max_entity_items=10,
    )
    assert len(package.memories) == 5
    assert len(package.entities) == 2


def test_dropped_items_are_recorded_with_their_reason() -> None:
    package = assemble(
        retrieval=result(memories=[memory(f"m{i}", i) for i in range(1, 6)]),
        max_memory_items=2,
    )
    dropped = [d for d in package.metadata.dropped_items if d.category == "memory"]
    assert len(dropped) == 3
    assert all(d.reason == "category_limit" for d in dropped)
    assert [d.retrieval_rank for d in dropped] == [3, 4, 5]


# --- Total budget -----------------------------------------------------------


def test_total_budget_is_never_exceeded() -> None:
    package = assemble(
        current="short",
        messages=[FakeMessage("user", "conversation " * 20) for _ in range(5)],
        retrieval=result(
            memories=[memory("memory text " * 20, i) for i in range(1, 6)],
            entities=[entity(f"Entity{i}", i) for i in range(1, 4)],
            relationships=[relationship("A", "USES", f"B{i}", i) for i in range(1, 4)],
        ),
        max_total_chars=600,
    )

    assert package.metadata.characters.total <= 600


def test_lowest_ranked_knowledge_is_dropped_first() -> None:
    package = assemble(
        current="short",
        retrieval=result(memories=[memory(f"memory number {i} " * 5, i) for i in range(1, 6)]),
        max_total_chars=250,
    )

    ranks = [m.retrieval_rank for m in package.memories]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1, "the top-ranked memory should survive"
    budget_drops = [d for d in package.metadata.dropped_items if d.reason == "total_budget"]
    assert budget_drops


def test_long_term_knowledge_yields_before_recent_conversation() -> None:
    """The exchange the user is in matters more than background knowledge."""
    package = assemble(
        current="short",
        messages=[FakeMessage("user", "recent turn")],
        retrieval=result(
            memories=[memory("m" * 100, 1)],
            entities=[entity("E" * 40, 1)],
            relationships=[relationship("A" * 30, "USES", "B" * 30, 1)],
        ),
        max_total_chars=120,
    )

    assert package.has_recent_conversation
    assert not package.has_long_term_knowledge


def test_character_counts_are_reported_per_category() -> None:
    package = assemble(
        current="hello",
        messages=[FakeMessage("user", "hi")],
        retrieval=result(memories=[memory("a memory", 1)]),
    )
    counts = package.metadata.characters
    assert counts.current_message == 5
    assert counts.recent_conversation > 0
    assert counts.memories == len("a memory")
    assert counts.total == (
        counts.current_message + counts.recent_conversation + counts.memories
        + counts.entities + counts.relationships
    )


# --- Item integrity ---------------------------------------------------------


def test_items_are_never_partially_truncated() -> None:
    """A memory appears completely or not at all."""
    texts = [f"This is memory number {i} with a reasonable amount of text." for i in range(1, 8)]
    package = assemble(
        current="q",
        retrieval=result(memories=[memory(t, i) for i, t in enumerate(texts, 1)]),
        max_total_chars=200,
    )

    for item in package.memories:
        assert item.content in texts, "memory text was altered"
        assert item.content.endswith("."), "memory text was cut"


def test_memory_text_is_never_altered() -> None:
    original = "  User uses PostgreSQL — with an em dash and  spacing.  "
    package = assemble(retrieval=result(memories=[memory(original, 1)]))
    assert package.memories[0].content == original


# --- Compact representations ------------------------------------------------


def test_entities_stay_compact() -> None:
    package = assemble(retrieval=result(entities=[entity("PostgreSQL", 1)]))
    item = package.entities[0]

    assert item.render() == "PostgreSQL (technology)"
    exposed = set(item.model_dump())
    # No database internals beyond the id.
    assert "created_at" not in exposed and "updated_at" not in exposed
    assert "normalized_name" not in exposed and "status" not in exposed
    assert "aliases" not in exposed


def test_relationships_stay_compact_and_carry_no_evidence() -> None:
    package = assemble(retrieval=result(
        relationships=[relationship("Mai", "USES", "PostgreSQL", 1)]
    ))
    item = package.relationships[0]

    assert item.render() == "Mai USES PostgreSQL"
    exposed = set(item.model_dump())
    assert "evidence" not in exposed
    assert "source_entity_id" not in exposed and "target_entity_id" not in exposed
    assert "created_at" not in exposed


def test_memories_keep_the_useful_metadata_only() -> None:
    package = assemble(retrieval=result(memories=[memory("m", 1, importance=9, confidence=0.88)]))
    item = package.memories[0]

    assert item.importance_score == 9
    assert item.confidence_score == pytest.approx(0.88)
    exposed = set(item.model_dump())
    assert "normalized_content" not in exposed
    assert "source_conversation_id" not in exposed
    assert "status" not in exposed


# --- Missing sources --------------------------------------------------------


def test_assembly_works_without_retrieval() -> None:
    package = assemble(messages=[FakeMessage("user", "hi")], retrieval=None)
    assert package.current_message
    assert package.has_recent_conversation
    assert not package.has_long_term_knowledge


def test_assembly_works_without_conversation() -> None:
    package = assemble(messages=[], retrieval=result(memories=[memory("m", 1)]))
    assert package.current_message
    assert not package.has_recent_conversation
    assert package.has_long_term_knowledge


def test_assembly_works_with_neither() -> None:
    package = assemble(messages=[], retrieval=None)
    assert package.current_message == "What stack am I using?"
    assert package.is_minimal


# --- Determinism ------------------------------------------------------------


def test_assembly_is_deterministic() -> None:
    args = dict(
        messages=[FakeMessage("user", f"m{i}") for i in range(4)],
        retrieval=result(
            memories=[memory(f"memory {i}", i) for i in range(1, 5)],
            entities=[entity(f"E{i}", i) for i in range(1, 4)],
        ),
        max_memory_items=2,
    )
    first = assemble(**args)
    second = assemble(**args)

    assert [m.id for m in first.memories] == [m.id for m in second.memories]
    assert first.metadata.characters.total == second.metadata.characters.total
    assert first.metadata.dropped_count == second.metadata.dropped_count


# --- Sizer indirection ------------------------------------------------------


def test_the_sizer_can_be_replaced_for_token_budgeting_later() -> None:
    """Accounting routes through one function, so tokens can replace chars."""
    def word_sizer(text: str) -> int:
        return len((text or "").split())

    budgeter = ContextBudgeter(limits(max_total_chars=5), sizer=word_sizer)
    outcome = budgeter.apply(
        current_message="one two",
        recent_conversation=[],
        memories=[
            __import__("app.context.schemas", fromlist=["ContextMemory"]).ContextMemory(
                id=uuid.uuid4(), content="three four five six seven eight",
                memory_type="semantic", importance_score=7, confidence_score=0.9,
                retrieval_rank=1,
            )
        ],
        entities=[], relationships=[],
    )

    # Two words for the message leaves three; the six-word memory cannot fit.
    assert outcome.memories == []
    assert character_sizer("abc") == 3
