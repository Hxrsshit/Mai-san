"""The keyword search is bounded, whatever the message.

Found in Stage 0 C4 validation (2026-10-16): building the keyword query
recomputed every keyword's word forms once per candidate form, which is
quadratic in a message's distinct words -- 1,000 of them took over ten
seconds, on the request path -- and a 32,000-character message produced a
statement of over 43,000 bound parameters, above asyncpg's limit of 32,767.
This file pins the fix: each keyword's forms are computed once, and at most
`MAX_QUERY_KEYWORDS` keywords reach a query. Statements here are the real
ones `MemoryRetriever` builds, compiled for the production dialect.
"""

import random
import string
import time
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.dialects.postgresql import asyncpg as pg_asyncpg

from app.entities.models import Entity, EntityStatus, EntityType, MemoryEntity
from app.memory.deduplication import normalize
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.retrieval import query_normalizer
from app.retrieval.query_normalizer import (
    MAX_QUERY_KEYWORDS,
    candidate_forms,
    concept_group,
    extract_keywords,
    keyword_form_groups,
    normalize_query,
    query_keywords,
    word_forms,
)
from app.retrieval.retrievers import MemoryRetriever
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService
from tests.scopes import ADMIN_SCOPE

pytestmark = pytest.mark.anyio

#: The most bound parameters asyncpg (and so PostgreSQL's wire protocol) allows.
ASYNCPG_PARAMETER_LIMIT = 32_767
#: The longest message the API accepts (`schemas/message.py`).
MAX_MESSAGE_CHARS = 32_000
#: Worst case with the cap, from the bounds in `MAX_QUERY_KEYWORDS`'s comment;
#: asserted so that growth is noticed long before it nears the driver limit.
BOUNDED_PARAMETERS = 3_500

TEA = "User's favourite tea is jasmine tea."


class CapturingSession:
    """Records the statement instead of running it; returns no rows."""

    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        return _Result()


def filled(prefix: str, words) -> str:
    """`prefix`, then `words` repeated, cut to exactly the API maximum."""
    text = prefix
    words = list(words)
    i = 0
    while len(text) < MAX_MESSAGE_CHARS:
        text += " " + words[i % len(words)]
        i += 1
    return text[:MAX_MESSAGE_CHARS]


def distinct_words(count: int, seed: int = 1, suffixes=("",)) -> list:
    rnd = random.Random(seed)
    out = []
    while len(out) < count:
        stem = "".join(rnd.choice(string.ascii_lowercase) for _ in range(rnd.randint(4, 9)))
        out.append(stem + suffixes[len(out) % len(suffixes)])
    return out


async def keyword_statement(message: str):
    """The statement the keyword search sends for `message`, or None."""
    session = CapturingSession()
    keywords = extract_keywords(normalize_query(message))
    await MemoryRetriever(session, pool_size=50)._collect_by_keyword({}, keywords)
    return session.statements[0] if session.statements else None


def compiled(statement):
    return statement.compile(dialect=pg_asyncpg.dialect())


#: Adversarial 32,000-character messages: as many distinct words as fit, words
#: whose inflections multiply their forms, and the preference and concept
#: words that add whole families of patterns.
LONG_MESSAGES = {
    "distinct words": filled("Which tea do I prefer?", distinct_words(5_000)),
    "inflected words": filled(
        "What do I like?", distinct_words(5_000, seed=2, suffixes=("ing", "ed", "ies", "ches", "s"))
    ),
    "preference and concept words": filled(
        "What kind of partner do I prefer?",
        sorted(query_normalizer.PREFERENCE_TERMS | query_normalizer.CONCEPT_GROUPS[0])
        + distinct_words(3_000, seed=3, suffixes=("ing", "ed")),
    ),
}


# --- A: the statement stays far below the driver's parameter limit ------------------


@pytest.mark.parametrize("name", sorted(LONG_MESSAGES))
async def test_a_maximum_length_message_builds_a_bounded_statement(name) -> None:
    message = LONG_MESSAGES[name]
    assert len(message) == MAX_MESSAGE_CHARS
    assert len(extract_keywords(normalize_query(message))) > 10 * MAX_QUERY_KEYWORDS

    parameters = len(compiled(await keyword_statement(message)).params)

    assert parameters < ASYNCPG_PARAMETER_LIMIT
    assert parameters <= BOUNDED_PARAMETERS, parameters


async def test_the_statement_size_does_not_grow_past_the_cap() -> None:
    """The same parameter count at 25 distinct words and at thousands."""
    words = distinct_words(4_000, seed=5)
    just_over = await keyword_statement(" ".join(words[: MAX_QUERY_KEYWORDS + 1]))
    maximum = await keyword_statement(filled(" ".join(words[:MAX_QUERY_KEYWORDS]), words))
    assert len(compiled(just_over).params) == len(compiled(maximum).params)


# --- B: the work to build it is bounded, measured by calls --------------------------


@pytest.mark.parametrize("name", sorted(LONG_MESSAGES))
async def test_word_forms_are_computed_once_per_used_keyword(monkeypatch, name) -> None:
    calls = {"count": 0}
    real = query_normalizer.word_forms

    def counting(keyword):
        calls["count"] += 1
        return real(keyword)

    monkeypatch.setattr(query_normalizer, "word_forms", counting)
    statement = await keyword_statement(LONG_MESSAGES[name])
    assert statement is not None
    # Once in `candidate_forms` and once in `keyword_form_groups`, for each of
    # at most MAX_QUERY_KEYWORDS keywords. Before the fix this was one call
    # per (keyword, form) pair: millions for these messages.
    assert calls["count"] <= 2 * MAX_QUERY_KEYWORDS, calls["count"]


async def test_building_a_maximum_length_query_is_fast() -> None:
    """A generous budget, not a benchmark: uncapped, the first message here
    took minutes. Measured well under a tenth of a second."""
    started = time.perf_counter()
    for message in LONG_MESSAGES.values():
        compiled(await keyword_statement(message))
    assert time.perf_counter() - started < 5.0


def test_per_keyword_groups_equal_the_original_definition() -> None:
    """The faster computation gives exactly what the per-pair scan gave."""
    for message in ("Which tea do I prefer?", "What kind of partner do I prefer?",
                    "Which teas did I like, and which women or wives did I love?",
                    "explaining batteries churches tried configured dancing"):
        keywords = query_keywords(extract_keywords(normalize_query(message)))
        forms = candidate_forms(keywords)
        original = [
            [form for form in forms if form in word_forms(k) or form in concept_group(k)]
            for k in keywords
        ]
        assert keyword_form_groups(keywords, forms) == original, message


# --- C: the cap is deterministic and enforced ---------------------------------------


def test_the_cap_keeps_the_first_distinct_keywords_in_order() -> None:
    assert MAX_QUERY_KEYWORDS == 24
    words = distinct_words(100, seed=7)
    assert query_keywords(words) == tuple(words[:MAX_QUERY_KEYWORDS])
    assert query_keywords(words) == query_keywords(list(words))      # same input, same cut
    repeated = [words[0], words[0], words[1]] + words[2:]
    assert query_keywords(repeated) == tuple(words[:MAX_QUERY_KEYWORDS])
    assert query_keywords(words[:5]) == tuple(words[:5])            # short: unchanged
    assert query_keywords([]) == ()


async def test_a_keyword_past_the_cap_never_reaches_the_query() -> None:
    words = distinct_words(MAX_QUERY_KEYWORDS, seed=11)
    statement = await keyword_statement(" ".join(words + ["zebra"]))
    patterns = " ".join(str(v) for v in compiled(statement).params.values())
    assert f" {words[-1]} " in patterns
    assert "zebra" not in patterns


async def test_a_short_question_is_untouched_by_the_cap(monkeypatch) -> None:
    """Required F: below the cap, the statement is identical with or without it."""
    question = "Which kind of tea or partner do I prefer and enjoy?"
    capped = compiled(await keyword_statement(question))
    monkeypatch.setattr(query_normalizer, "MAX_QUERY_KEYWORDS", 10_000)
    uncapped = compiled(await keyword_statement(question))
    assert str(capped) == str(uncapped)
    assert capped.params == uncapped.params


# --- E: user text is bound, never written into the SQL ------------------------------


async def test_message_words_are_bound_parameters_not_sql_text() -> None:
    """Quotes and SQL in a message never reach the statement text. (`_` and
    `%` are LIKE wildcards inside a bound pattern: at worst a wider candidate
    pool, which `keyword_matches` then filters exactly.)"""
    statement = compiled(await keyword_statement(
        "Which xyzzy_tea do I prefer'); SELECT plughword; -- ?"))
    sql = str(statement).lower()
    for sentinel in ("xyzzy", "plughword", "select plughword", "');"):
        assert sentinel not in sql
    values = " ".join(str(value) for value in statement.params.values())
    assert " xyzzy_tea " in values and " plughword " in values


# --- End to end: a maximum-length message, through the real retrieval service --------


async def store(session, items):
    conversation = (await ConversationService(session, ADMIN_SCOPE).create_conversation()).id
    now = datetime.now(timezone.utc)
    for i, (text, importance) in enumerate(items):
        created = now - timedelta(days=i + 1)
        session.add(Memory(
            content=text, normalized_content=normalize(text),
            memory_type=MemoryType.PREFERENCE, status=MemoryStatus.ACTIVE,
            importance_score=importance, confidence_score=0.95,
            source_conversation_id=conversation, created_at=created, updated_at=created,
        ))
    await session.flush()


async def test_a_maximum_length_message_still_retrieves_and_is_not_degraded(
    db_session, settings
) -> None:
    await store(db_session, [(TEA, 5), ("User prefers working in a small team.", 9),
                             ("User likes hiking on weekends.", 7)])
    message = LONG_MESSAGES["distinct words"]       # opens with "Which tea do I prefer?"

    result = await RetrievalService(db_session, settings).retrieve(message)

    assert result.metadata.degraded_sources == []
    assert [m.content for m in result.memories][:1] == [TEA]


async def test_ranking_scores_a_long_message_against_the_keywords_searched(
    db_session, settings
) -> None:
    """The ranker uses the same capped keywords as the search, so a long
    message scores its memories exactly as a message of just those keywords."""
    await store(db_session, [(TEA, 5), ("User drinks green tea every morning.", 3)])
    message = LONG_MESSAGES["distinct words"]
    searched = " ".join(query_keywords(extract_keywords(normalize_query(message))))

    long_result = await RetrievalService(db_session, settings).retrieve(message)
    short_result = await RetrievalService(db_session, settings).retrieve(searched)

    def scores(result):
        # Rounded: recency moves by microseconds between the two calls.
        return [(m.content, round(m.score.text_relevance, 9), round(m.score.final_score, 6))
                for m in result.memories]

    assert scores(long_result) == scores(short_result)
    assert long_result.memories[0].score.text_relevance > 0


# --- The debug view shows the pool chat ranks, never the union of its sources -------


async def test_the_debug_view_is_bounded_when_its_sources_disagree(
    client: AsyncClient, session_factory, settings
) -> None:
    """Each source (entities, relationships, keywords) is capped separately,
    so their union can reach three pools. Chat ranks only the first pool's
    worth, sources in that order; the debug view returned the whole union --
    100 candidates here, at committed HEAD too. It must show what chat ranks:
    the same bound, the same order, so the same memories."""
    async with session_factory() as session:
        conversation = await ConversationService(session, ADMIN_SCOPE).create_conversation()
        for index in range(150):
            entity = Entity(canonical_name=f"Tool{index}", normalized_name=f"tool{index}",
                            entity_type=EntityType.TECHNOLOGY, status=EntityStatus.ACTIVE)
            session.add(entity)
            await session.flush()
            memory = Memory(content=f"Mai uses Tool{index}.", normalized_content=f"mai uses tool{index}",
                            memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                            importance_score=8, confidence_score=0.9,
                            source_conversation_id=conversation.id)
            session.add(memory)
            await session.flush()
            session.add(MemoryEntity(memory_id=memory.id, entity_id=entity.id))
        # Keyword-only memories, more important than any entity-linked one:
        # the keyword source fills its own pool with them.
        for index in range(60):
            session.add(Memory(content=f"User saw a zebra {index}.",
                               normalized_content=f"user saw a zebra {index}",
                               memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                               importance_score=10, confidence_score=0.9,
                               source_conversation_id=conversation.id))
        await session.commit()

    query = "zebra " + " ".join(f"Tool{index}" for index in range(150))
    response = await client.post("/api/retrieval/debug", json={"query": query[:4000]})
    assert response.status_code == 200
    body = response.json()
    candidates = body["candidate_memories"]
    pool_size = settings.RETRIEVAL_CANDIDATE_POOL_SIZE

    assert len(candidates) == pool_size                        # bounded, and full
    assert len(candidates) == body["metadata"]["candidate_memories"]   # what chat ranked
    # Sources in chat's order: entity matches fill the pool first, so the
    # keyword source's extra memories are beyond the bound in both paths.
    assert all(c["content"].startswith("Mai uses Tool") for c in candidates)
    selected = {m["id"] for m in body["selected_memories"]}
    assert selected and selected <= {c["id"] for c in candidates}
