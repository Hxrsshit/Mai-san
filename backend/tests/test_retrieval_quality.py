"""Retrieval quality: the relevant memory, not the merely similar-sounding one.

Live diagnostics (2026-10-14) showed "Which tea do I prefer?" ranking the
memory "User's favourite tea is jasmine tea." fifth of nine. Three measured
causes: keyword matching was by substring ("tea" matched "team", "steady",
"instead"); a generic word like "prefer" counted as much as the subject, so
every "User prefers ..." memory tied with the tea memory and importance
decided; and "prefer" did not match "favourite". This file pins the fix --
whole-word matching with bounded word forms, weighted query terms, one small
preference family and one concept group -- against synthetic data, through
the real retrieval, assembly and prompt code. Budgets are unchanged.
"""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.context.service import ContextService
from app.memory.deduplication import normalize
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.prompt.formatter import PromptFormatter, knowledge_block
from app.retrieval.query_normalizer import keyword_matches, term_kind, word_forms
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService

pytestmark = pytest.mark.anyio

NOTHING = json.dumps({"should_store_memory": False, "memories": []})
TEA = "User's favourite tea is jasmine tea."
PARTNER = "User's ideal partner is kind, curious and enjoys hiking."
SISTER = "User's sister is a woman engineer who lives abroad."
#: Higher importance than the targets, sharing only generic or substring words.
DECOYS = [
    ("User prefers concise explanations with practical examples.", 9),
    ("User prefers dark mode in every editor.", 8),
    ("User prefers working in a small team.", 9),
    ("User likes to keep notes instead of relying on memory.", 8),
    ("User enjoys a steady morning routine.", 8),
    ("User's favourite programming language is Python.", 8),
    ("User prefers to be called by their first name.", 9),
    ("User likes hiking on weekends.", 7),
    ("User prefers meetings in the afternoon.", 8),
    ("User loves science fiction novels.", 7),
    ("User would like to learn Japanese.", 8),
    ("User prefers a standing desk while working.", 9),
]


async def store(session, items, conversation_id=None):
    if conversation_id is None:
        conversation_id = (await ConversationService(session).create_conversation()).id
    now = datetime.now(timezone.utc)
    for i, (text, importance) in enumerate(items):
        created = now - timedelta(days=i + 1)
        session.add(Memory(
            content=text, normalized_content=normalize(text),
            memory_type=MemoryType.PREFERENCE, status=MemoryStatus.ACTIVE,
            importance_score=importance, confidence_score=0.95,
            source_conversation_id=conversation_id, created_at=created, updated_at=created,
        ))
    await session.flush()


@pytest.fixture
async def library(db_session):
    await store(db_session, DECOYS + [(TEA, 5), (PARTNER, 6), (SISTER, 7)])
    return db_session


async def ranked(session, settings, query):
    return [m.content for m in (await RetrievalService(session, settings).retrieve(query)).memories]


# --- 1-4: the tea regression, four phrasings ----------------------------------


@pytest.mark.parametrize("query", [
    "Which tea do I prefer?",
    "What is my favourite tea?",
    "Which kind of tea do I like?",
    "Tell me the tea I said I liked.",
])
async def test_the_tea_memory_ranks_first_for_every_phrasing(library, settings, query) -> None:
    order = await ranked(library, settings, query)
    assert order and order[0] == TEA, order


async def test_generic_preference_memories_do_not_compete(library, settings) -> None:
    """Required 1 and 14: sharing only "prefer" makes no memory a candidate
    when the question names its subject."""
    order = await ranked(library, settings, "Which tea do I prefer?")
    assert order == [TEA]


# --- 5: the partner regression ------------------------------------------------


@pytest.mark.parametrize("query", [
    "What is my ideal type of woman?",
    "What kind of partner do I prefer?",
    "What did I say about my ideal partner?",
])
async def test_the_partner_preference_ranks_first(library, settings, query) -> None:
    order = await ranked(library, settings, query)
    assert order and order[0] == PARTNER, order


async def test_a_concept_word_without_a_preference_is_not_a_partner(library, settings) -> None:
    """Required 8: "woman" stands for "partner" only beside a preference."""
    assert SISTER not in await ranked(library, settings, "What kind of partner do I prefer?")


# --- 6-8: matching rules ---------------------------------------------------------


@pytest.mark.parametrize("memory", [
    "user prefers working in a small team",
    "user likes to keep notes instead of relying on memory",
    "user enjoys a steady morning routine",
    "user once had a heated argument",
])
def test_a_word_inside_a_longer_word_is_not_a_match(memory) -> None:
    assert not keyword_matches("tea", set(memory.split()))


@pytest.mark.parametrize("query", [
    "WHICH TEA DO I PREFER??", "which tea do i prefer", "Which teas do I like?",
    "Which tea do I enjoy...", "Tea -- which one do I favour, my favorite?",
])
async def test_case_punctuation_and_word_forms(library, settings, query) -> None:
    order = await ranked(library, settings, query)
    assert order and order[0] == TEA, (query, order)


def test_word_forms_are_bounded_and_whole_words() -> None:
    forms = word_forms("like")
    assert {"like", "likes", "liked", "liking"} <= forms
    assert all(" " not in form and len(form) >= 3 for form in forms)
    assert len(forms) < 12
    assert "liked" in word_forms("liked") and "like" in word_forms("liked")
    assert {"tea", "teas"} <= word_forms("tea")


def test_preference_words_are_equivalent_but_subjects_are_not() -> None:
    words = set("user s favourite tea is jasmine tea".split())
    assert keyword_matches("prefer", words) and keyword_matches("liked", words)
    assert not keyword_matches("coffee", words)
    assert term_kind("tea") == "subject" and term_kind("prefer") == "preference"
    assert term_kind("kind") == "frame"


async def test_a_synonym_does_not_pull_in_an_unrelated_subject(library, settings) -> None:
    """Required 8: "like" finds the tea memory, never "User likes hiking"."""
    order = await ranked(library, settings, "Which kind of tea do I like?")
    assert "User likes hiking on weekends." not in order


async def test_a_subject_word_counts_for_more_than_a_preference_word(
    db_session, settings
) -> None:
    """Both memories match two of the three words in "which green tea do I
    prefer?". The one matching the subject twice answers it; the one matching
    "green" and "prefer" is about something else. Equal word weights made them
    tie, and importance picked the wrong one."""
    await store(db_session, [("User prefers green smoothies after the gym.", 9),
                             ("User drinks green tea every morning.", 3)])
    order = await ranked(db_session, settings, "Which green tea do I prefer?")
    assert order[0] == "User drinks green tea every morning."


# --- 9-12: importance, candidate selection, determinism, budget -----------------------


async def test_importance_does_not_bury_the_relevant_memory(db_session, settings) -> None:
    """Twenty maximally important memories share the subject but not the
    preference; the answer has importance 2. Relevance must decide."""
    await store(db_session, [(f"User mentioned tea in passing during meeting {i}.", 10)
                             for i in range(20)] + [(TEA, 2)])
    order = await ranked(db_session, settings, "What is my favourite tea?")
    assert order[0] == TEA


async def test_a_crowd_sharing_the_subject_cannot_push_it_out_of_the_pool(
    db_session, settings
) -> None:
    """Required 10. Fifty-five important memories mention tea without a
    preference; the pool holds fifty. The one that answers the question must
    still reach ranking -- and win it."""
    await store(db_session, [(f"User drinks green tea at desk number {i}.", 10) for i in range(55)]
                + [(TEA, 3)])
    assert settings.RETRIEVAL_CANDIDATE_POOL_SIZE == 50
    order = await ranked(db_session, settings, "Which tea do I prefer?")
    assert order[0] == TEA


async def test_ranking_is_deterministic_whatever_the_insertion_order(
    db_session, session_factory, settings
) -> None:
    items = DECOYS + [(TEA, 5), (PARTNER, 6), (SISTER, 7)] + [
        (f"User drinks tea flavour {i}.", 6) for i in range(8)]
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)
    await store(db_session, shuffled)
    first = [m.id for m in (await RetrievalService(db_session, settings).retrieve(
        "Which tea do I prefer?")).memories]
    again = [m.id for m in (await RetrievalService(db_session, settings).retrieve(
        "Which tea do I prefer?")).memories]
    assert first == again and len(first) > 1


async def test_the_relevant_memory_survives_the_unchanged_budget(db_session, settings) -> None:
    """Required 12: more matching candidates than the budget allows."""
    assert settings.RETRIEVAL_MAX_MEMORIES == 10
    await store(db_session, [(f"User drinks green tea at desk number {i}.", 10) for i in range(25)]
                + [(TEA, 3)])
    package = await ContextService(db_session, settings).build("Which tea do I prefer?")
    contents = [m.content for m in package.memories]
    assert len(contents) <= 10 and contents[0] == TEA


# --- 13-14: what actually reaches the model ---------------------------------------------


async def test_the_prompt_carries_the_relevant_memory_and_not_the_generic_ones(
    library, settings
) -> None:
    package = await ContextService(library, settings).build("Which tea do I prefer?")
    block = knowledge_block(PromptFormatter(settings.MAI_SYSTEM_PROMPT).format(package).messages)
    assert TEA in block
    for text, _ in DECOYS:
        assert text not in block


async def test_end_to_end_the_provider_request_carries_the_memory(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        await store(session, DECOYS + [(TEA, 5)])
        await session.commit()
    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    fake_provider.extraction_reply = NOTHING
    response = await client.post(f"/api/conversations/{conversation}/messages",
                                 json={"content": "Which kind of tea do I like?"})
    assert response.status_code == 201
    block = knowledge_block(fake_provider.last_call)
    assert TEA in block
    assert "User prefers working in a small team." not in block   # "team" is not "tea"
    assert "User likes hiking on weekends." not in block          # "like" is not the subject


# --- A question with no subject still works ------------------------------------------


async def test_a_question_with_only_a_preference_word_falls_back_to_it(library, settings) -> None:
    """ "What do I like?" names no subject: every preference memory is a
    candidate, and the unchanged budget then keeps the best ten."""
    result = await RetrievalService(library, settings).retrieve("What do I like?")
    preference_memories = len(DECOYS) + 2       # all decoys, tea and partner
    assert result.metadata.candidate_memories == preference_memories
    assert SISTER not in [m.content for m in result.memories]   # no preference word
    assert 0 < len(result.memories) <= settings.RETRIEVAL_MAX_MEMORIES
