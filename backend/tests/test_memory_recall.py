"""Memory recall, end to end: persistence -> retrieval -> context -> prompt.

Written for the 2026-10-13 recall failure. A user answered the assistant's
question about their ideal partner with a short description; extraction saw
that answer *without* the question and stored the description as a fact about
the user. Asked later "what is my ideal type of woman?", Mai retrieved that
memory, sent it to the model -- and the model rightly found no preference in
it. Separately, the reply had claimed "I've saved that" before anything was
saved, and a retrieval failure would have looked exactly like "no memories".

Every case drives the real code: the API, the database (SQLite, per test),
background extraction, retrieval, assembly and the prompt formatter. Only the
model is a fake, so assertions read what the provider was actually *sent*.
All personal data here is synthetic.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.database.models.message import Message, MessageRole
from app.memory.deduplication import normalize
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.memory.service import MemoryService
from app.prompt.formatter import (
    MEMORY_UNAVAILABLE_NOTE,
    REFERENCE_HEADER,
    knowledge_block,
)
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService
from app.synthesis.execution_truth import Channel, claims, validate, record_for_turn
from tests.scopes import ADMIN_SCOPE

pytestmark = pytest.mark.anyio

NOTHING = json.dumps({"should_store_memory": False, "memories": []})


def remember(content: str, kind: str = "preference", importance: int = 8) -> str:
    return json.dumps({"should_store_memory": True, "memories": [{
        "content": content, "memory_type": kind,
        "importance_score": importance, "confidence_score": 0.95,
    }]})


async def say(client: AsyncClient, conversation_id, text: str) -> dict:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": text}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def new_conversation(client: AsyncClient):
    response = await client.post("/api/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


def reference(fake_provider) -> str:
    return knowledge_block(fake_provider.last_call) or ""


async def store_memory(session, content, kind=MemoryType.PREFERENCE, importance=8,
                       status=MemoryStatus.ACTIVE, age_days=0, conversation_id=None):
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    if conversation_id is None:
        # Live memories must name their source conversation (provenance check).
        conversation_id = (await ConversationService(session, ADMIN_SCOPE).create_conversation()).id
    memory = Memory(
        content=content, normalized_content=normalize(content), memory_type=kind,
        status=status, importance_score=importance, confidence_score=0.95,
        source_conversation_id=conversation_id, created_at=created, updated_at=created, **ADMIN_SCOPE.write_columns(),
    )
    session.add(memory)
    await session.flush()
    return memory


# --- The original failure: an answer extracted without its question -----------


async def test_extraction_sees_the_question_a_short_answer_responds_to(
    db_session, fake_provider, settings
) -> None:
    """Root cause. Without the earlier turns the extractor read "tall, wears
    glasses" as a description of the user. It must receive the question."""
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = [
        (MessageRole.USER, "Please remember my type."),
        (MessageRole.ASSISTANT, "Of course. What is your ideal partner like?"),
        (MessageRole.USER, "Tall, wears glasses, slender build."),
    ]
    stored = []
    for offset, (role, text) in enumerate(rows):
        message = Message(conversation_id=conversation.id, role=role, content=text,
                          created_at=base + timedelta(seconds=offset))
        db_session.add(message)
        stored.append(message)
    await db_session.flush()
    fake_provider.extraction_reply = remember(
        "User's ideal partner is tall, wears glasses and has a slender build.")

    await MemoryService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE
                        ).extract_and_store(
        conversation_id=conversation.id,
        user_message=rows[2][1],
        assistant_message="Got it.",
        source_message_id=stored[2].id,
        scope=ADMIN_SCOPE,
    )

    prompt = fake_provider.extraction_calls[-1][-1].content
    earlier, _, rest = prompt.partition("USER MESSAGE")
    assert "EARLIER CONTEXT (for understanding only" in earlier
    assert "assistant: Of course. What is your ideal partner like?" in earlier
    assert "user: Please remember my type." in earlier
    # The answer itself is the extraction source, not part of the context.
    assert "Tall, wears glasses" not in earlier and "Tall, wears glasses" in rest


async def test_extraction_context_is_bounded_and_excludes_the_turn_itself(
    db_session, fake_provider, settings
) -> None:
    settings.MEMORY_EXTRACTION_CONTEXT_MESSAGES = 2
    settings.MEMORY_EXTRACTION_CONTEXT_CHARS = 60
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    messages = []
    for i in range(6):
        m = Message(conversation_id=conversation.id,
                    role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                    content=f"message number {i} " + "x" * 40,
                    created_at=base + timedelta(seconds=i))
        db_session.add(m)
        messages.append(m)
    await db_session.flush()
    fake_provider.extraction_reply = NOTHING
    await MemoryService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE
                        ).extract_and_store(conversation.id, "now", "ok",
                                            source_message_id=messages[4].id, scope=ADMIN_SCOPE)
    earlier = fake_provider.extraction_calls[-1][-1].content.partition("USER MESSAGE")[0]
    assert "message number 2" in earlier and "message number 3" in earlier
    assert "message number 1" not in earlier       # bounded to 2
    assert "message number 4" not in earlier       # the analysed turn itself
    assert "message number 5" not in earlier       # nothing after it
    assert "x" * 60 not in earlier                 # each line capped


async def test_the_original_scenario_recalls_the_preference_in_a_new_conversation(
    client: AsyncClient, fake_provider, settings
) -> None:
    """End to end: question, short answer, extraction with context, then a
    cross-conversation question that retrieves it into the prompt."""
    first = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    fake_provider.reply = "Of course. What is your ideal partner like?"
    await say(client, first, "Please remember my ideal type of woman.")

    fake_provider.extraction_reply = remember(
        "User's ideal type of woman is tall, wears glasses and has a slender build.")
    fake_provider.reply = "Thanks for telling me."
    await say(client, first, "Tall, wears glasses, slender build.")
    assert "What is your ideal partner like?" in fake_provider.extraction_calls[-1][-1].content

    second = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, second, "What is my ideal type of woman?")
    block = reference(fake_provider)
    assert "wears glasses" in block and "slender build" in block
    # The new conversation has no history: the memory is the only source.
    assert not any("Tall, wears glasses, slender build." == m.content
                   for m in fake_provider.last_call)


def test_the_extraction_window_is_on_and_bounded_by_default(settings) -> None:
    from app.core.config import Settings

    defaults = Settings(_env_file=None)
    assert defaults.MEMORY_EXTRACTION_CONTEXT_MESSAGES == 4
    assert defaults.MEMORY_EXTRACTION_CONTEXT_CHARS == 500
    for bad in ({"MEMORY_EXTRACTION_CONTEXT_MESSAGES": 11},
                {"MEMORY_EXTRACTION_CONTEXT_MESSAGES": -1},
                {"MEMORY_EXTRACTION_CONTEXT_CHARS": 2001},
                {"MEMORY_EXTRACTION_CONTEXT_MESSAGES": "all"}):
        with pytest.raises(Exception):
            Settings(_env_file=None, **bad)


async def _turns(session, conversation, rows, start=None):
    """Messages with explicit, strictly increasing timestamps."""
    start = start or datetime.now(timezone.utc) - timedelta(minutes=10)
    stored = []
    for offset, (role, text) in enumerate(rows):
        message = Message(conversation_id=conversation.id, role=role, content=text,
                          created_at=start + timedelta(seconds=offset))
        session.add(message)
        stored.append(message)
    await session.flush()
    return stored


async def _extract(session, fake_provider, settings, conversation, source, reply):
    fake_provider.extraction_reply = reply
    return await MemoryService(session=session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE
                               ).extract_and_store(
        conversation_id=conversation.id, user_message=source.content,
        assistant_message="Thanks.", source_message_id=source.id, scope=ADMIN_SCOPE)


def _sent_context(fake_provider) -> str:
    return fake_provider.extraction_calls[-1][-1].content.partition("USER MESSAGE")[0]


@pytest.mark.parametrize("question,answer,memory", [
    ("Which programming language do you prefer?", "Python.",
     "User prefers the Python programming language."),
    ("What is your ideal partner like?", "Kind, curious and fond of hiking.",
     "User's ideal partner is kind, curious and fond of hiking."),
    ("Do you prefer working from home or from the office?", "Home, mostly.",
     "User prefers working from home most of the time."),
])
async def test_a_short_answer_is_extracted_with_its_question(
    db_session, fake_provider, settings, question, answer, memory
) -> None:
    """The extractor receives the question; what is stored is exactly what
    extraction returned, persisted and readable from a fresh query. (The
    model's interpretation is mocked here; only a live run proves it.)"""
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    _, source = await _turns(db_session, conversation, [
        (MessageRole.ASSISTANT, question), (MessageRole.USER, answer)])
    stored = await _extract(db_session, fake_provider, settings, conversation, source,
                            remember(memory))
    assert f"assistant: {question}" in _sent_context(fake_provider)
    assert [m.content for m in stored] == [memory]
    await db_session.commit()
    row = (await db_session.execute(select(Memory))).scalars().one()
    assert row.content == memory and row.source_message_id == source.id
    assert row.source_conversation_id == conversation.id


async def test_a_short_answer_with_nothing_before_it_gets_no_context(
    db_session, fake_provider, settings
) -> None:
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    (source,) = await _turns(db_session, conversation, [(MessageRole.USER, "Python.")])
    await _extract(db_session, fake_provider, settings, conversation, source, NOTHING)
    prompt = fake_provider.extraction_calls[-1][-1].content
    assert "EARLIER CONTEXT (for understanding only" not in prompt and "Python." in prompt


async def test_another_conversations_turns_never_enter_the_context(
    db_session, fake_provider, settings
) -> None:
    other = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    await _turns(db_session, other, [
        (MessageRole.ASSISTANT, "OTHER-CONVERSATION question?"),
        (MessageRole.USER, "OTHER-CONVERSATION answer.")])
    mine = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    (source,) = await _turns(db_session, mine, [(MessageRole.USER, "Python.")],
                             start=datetime.now(timezone.utc))
    await _extract(db_session, fake_provider, settings, mine, source, NOTHING)
    assert "OTHER-CONVERSATION" not in fake_provider.extraction_calls[-1][-1].content


async def test_a_source_message_from_another_conversation_yields_no_context(
    db_session, fake_provider, settings
) -> None:
    """A mismatched (conversation, message) pair is refused, not trusted.
    Since Stage 7B unit 4 the whole turn is refused before extraction: the
    source message must be a message of the given conversation, so neither
    conversation's text reaches the model and nothing is stored."""
    mine = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    await _turns(db_session, mine, [(MessageRole.ASSISTANT, "MINE-EARLIER question?")],
                 start=datetime.now(timezone.utc) - timedelta(minutes=30))
    other = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    _, foreign = await _turns(db_session, other, [
        (MessageRole.ASSISTANT, "FOREIGN question?"), (MessageRole.USER, "Answer.")])
    stored = await _extract(db_session, fake_provider, settings, mine, foreign, NOTHING)
    assert fake_provider.extraction_calls == []
    assert not stored


async def test_a_failed_extraction_stores_nothing(
    db_session, fake_provider, settings
) -> None:
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    _, source = await _turns(db_session, conversation, [
        (MessageRole.ASSISTANT, "Which language do you prefer?"), (MessageRole.USER, "Python.")])
    fake_provider.extraction_error = RuntimeError("synthetic provider failure")
    stored = await _extract(db_session, fake_provider, settings, conversation, source, NOTHING)
    assert stored == []
    assert (await db_session.execute(select(Memory))).scalars().all() == []


async def test_a_failed_database_write_stores_nothing_and_reports_nothing(
    db_session, fake_provider, settings, monkeypatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    _, source = await _turns(db_session, conversation, [
        (MessageRole.ASSISTANT, "Which language do you prefer?"), (MessageRole.USER, "Python.")])
    await db_session.commit()

    real_flush = db_session.flush

    async def failing_flush(*args, **kwargs):
        if any(isinstance(obj, Memory) for obj in db_session.new):
            raise SQLAlchemyError("synthetic write failure")
        return await real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", failing_flush)
    stored = await _extract(db_session, fake_provider, settings, conversation, source,
                            remember("User prefers the Python programming language."))
    assert stored == []
    monkeypatch.setattr(db_session, "flush", real_flush)
    await db_session.rollback()
    assert (await db_session.execute(select(Memory))).scalars().all() == []


async def test_a_save_claim_with_a_failed_extraction_is_corrected(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Persistence failed and the model said it saved: the user must see
    neither the claim nor a stored memory."""
    conversation = await new_conversation(client)
    fake_provider.extraction_error = RuntimeError("synthetic provider failure")
    fake_provider.replies = ["I've saved that.", "I've stored that for you."]
    body = await say(client, conversation, "Python.")
    content = body["assistant_message"]["content"]
    # Neither model claim reached the user; the application's line did.
    assert content not in ("I've saved that.", "I've stored that for you.")
    assert content.startswith("I can't confirm that I've saved that.")
    async with session_factory() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []


async def test_a_plural_in_the_memory_still_matches_a_singular_question(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Contributing cause: "woman" never matched "women" by substring."""
    async with session_factory() as session:
        await store_memory(session, "User is attracted to women who are tall and wear glasses.")
        await session.commit()
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my ideal type of woman?")
    assert "wear glasses" in reference(fake_provider)


# --- Scenarios 1, 2, 4, 5, 6, 7 -----------------------------------------------


async def test_same_conversation_recall(client: AsyncClient, fake_provider) -> None:
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = remember("User's favorite programming language is Python.")
    await say(client, conversation, "My favorite programming language is Python.")
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    sent = [m.content for m in fake_provider.last_call]
    assert "Python" in reference(fake_provider)                 # long-term memory
    assert "My favorite programming language is Python." in sent  # and the history


async def test_cross_conversation_recall(client: AsyncClient, fake_provider) -> None:
    first = await new_conversation(client)
    fake_provider.extraction_reply = remember(
        "User prefers concise explanations with practical examples.")
    await say(client, first, "I prefer concise explanations with practical examples.")
    second = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, second, "What kind of explanations do I prefer?")
    assert "concise explanations with practical examples" in reference(fake_provider)


@pytest.mark.xfail(strict=True, reason=(
    "Known limit of deterministic lexical retrieval, still present after the "
    "word-form and preference-term changes: 'explaining' reduces to 'explain', "
    "which is not a form of 'explanations', and 'detail' is not 'concise'. "
    "Recorded, not papered over; a semantic retriever would be a separate, "
    "approved change."))
async def test_indirect_question_without_shared_words(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        await store_memory(session, "User prefers concise explanations with practical examples.")
        await session.commit()
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "How much detail should you give me when explaining something?")
    assert "concise explanations" in reference(fake_provider)


async def test_the_relevant_memory_is_chosen_among_unrelated_ones(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        for text in ("User lives near the sea.", "User owns a grey bicycle.",
                     "User studies the cello on weekends.", "User dislikes cold weather."):
            await store_memory(session, text, importance=9)
        await store_memory(session, "User's favorite programming language is Python.", importance=6)
        await session.commit()
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    block = reference(fake_provider)
    assert "Python" in block
    assert "bicycle" not in block and "cello" not in block


async def test_an_updated_preference_wins_over_the_superseded_one(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """The existing Stage 3C lifecycle: SUPERSEDED is excluded by default."""
    async with session_factory() as session:
        await store_memory(session, "User's favorite programming language is Java.",
                           status=MemoryStatus.SUPERSEDED, age_days=30)
        await store_memory(session, "User's favorite programming language is Rust.")
        await session.commit()
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    block = reference(fake_provider)
    assert "Rust" in block and "Java" not in block


async def test_a_fact_never_given_reaches_the_model_as_nothing(
    client: AsyncClient, fake_provider
) -> None:
    """No evidence, no reference block, no "memory unavailable" note: the
    model is told nothing it could mistake for knowledge."""
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    contents = [m.content for m in fake_provider.last_call]
    assert reference(fake_provider) == ""
    assert MEMORY_UNAVAILABLE_NOTE not in contents


# --- Scenario 8: a fresh session --------------------------------------------------


async def test_a_committed_memory_is_retrievable_from_a_fresh_session(
    session_factory, settings
) -> None:
    async with session_factory() as writer:
        await store_memory(writer, "User's favorite tea is genmaicha.")
        await writer.commit()
    async with session_factory() as reader:   # new session, empty identity map
        result = await RetrievalService(reader, settings, scope=ADMIN_SCOPE).retrieve("Which tea is my favorite?")
    assert [m.content for m in result.memories] == ["User's favorite tea is genmaicha."]


# --- Scenario 9: a retrieval failure is not "no memories" ---------------------------


async def test_a_failed_memory_search_is_reported_to_the_model_as_unavailable(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    async with session_factory() as session:
        await store_memory(session, "User's favorite programming language is Python.")
        await session.commit()

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic memory query failure")

    monkeypatch.setattr("app.retrieval.retrievers.MemoryRetriever.collect", broken)
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    contents = [m.content for m in fake_provider.last_call]
    assert MEMORY_UNAVAILABLE_NOTE in contents
    assert not any(REFERENCE_HEADER in c for c in contents)  # no knowledge leaks in


async def test_a_total_retrieval_failure_is_reported_to_the_model_as_unavailable(
    client: AsyncClient, fake_provider, monkeypatch
) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic retrieval outage")

    monkeypatch.setattr("app.retrieval.service.RetrievalService.retrieve", broken)
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    assert MEMORY_UNAVAILABLE_NOTE in [m.content for m in fake_provider.last_call]


async def test_a_failed_inner_source_is_recorded_on_the_context_package(
    db_session, settings, monkeypatch
) -> None:
    from app.context.service import ContextService

    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr("app.retrieval.retrievers.MemoryRetriever.collect", broken)
    package = await ContextService(db_session, settings, scope=ADMIN_SCOPE).build("What is my favorite tea?")
    assert "retrieval:memories" in package.metadata.degraded_sources


# --- Scenario 10: relevance under competition and budget ---------------------------


async def test_the_best_match_survives_a_crowd_of_more_important_partial_matches(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """55 important memories share one query word; one less important memory
    matches every query word. The bounded candidate pool must not drop it."""
    async with session_factory() as session:
        for i in range(55):
            await store_memory(session, f"User's favorite snack number {i} is a fruit.",
                               importance=10)
        await store_memory(session, "User's favorite programming language is Python.",
                           importance=5, age_days=90)
        await session.commit()
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    await say(client, conversation, "What is my favorite programming language?")
    assert "programming language is Python" in reference(fake_provider)


# --- Scenario 11: ownership and authentication --------------------------------------


async def test_memory_and_context_endpoints_require_authentication(
    client: AsyncClient,
) -> None:
    conversation = await new_conversation(client)
    for method, path in (
        ("GET", "/api/memories"),
        ("GET", f"/api/conversations/{conversation}"),
        ("GET", f"/api/conversations/{conversation}/context-preview"),
        ("POST", "/api/context/debug"),
        ("POST", "/api/retrieval/debug"),
        ("POST", "/api/prompt/debug"),
    ):
        response = await client.request(
            method, path, headers={"Authorization": ""},
            **({"json": {"message": "hi", "query": "hi"}} if method == "POST" else {}),
        )
        assert response.status_code == 401, (method, path)


async def test_a_forged_owner_header_changes_nothing(client: AsyncClient) -> None:
    forged = {"X-Owner-Id": "00000000-0000-0000-0000-000000000099",
              "X-User-Id": "00000000-0000-0000-0000-000000000099"}
    plain = await client.get("/api/memories")
    spoofed = await client.get("/api/memories", headers=forged)
    assert spoofed.status_code == plain.status_code == 200
    assert spoofed.json() == plain.json()


# --- Scenario 12: conversation history, in order, through both paths ----------------


async def test_history_is_ordered_in_the_api_and_in_the_prompt(
    client: AsyncClient, fake_provider
) -> None:
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    for text in ("first note", "second note", "third note"):
        await say(client, conversation, text)
    api = (await client.get(f"/api/conversations/{conversation}")).json()["messages"]
    users = [m["content"] for m in api if m["role"] == "user"]
    assert users == ["first note", "second note", "third note"]
    sent = [m.content for m in fake_provider.last_call]
    assert sent.index("first note") < sent.index("second note") < len(sent) - 1
    assert sent[-1] == "third note"


# --- Phase C: "I've saved that" is never established on a reply ---------------------


@pytest.mark.parametrize("reply", [
    "Got it! I've saved your description.",
    "Noted -- I have stored that in my memory.",
    "I'll remember that you prefer tea.",
    "Sure, I'll keep that in mind.",
])
def test_a_save_claim_is_never_licensed_on_a_reply(reply) -> None:
    assert Channel.MEMORY in claims(reply)
    verdict = validate(reply, record_for_turn())
    assert not verdict.ok and Channel.MEMORY in verdict.violations


@pytest.mark.parametrize("reply", [
    "I remember you said you like Python.",
    "I haven't saved anything yet.",
    "Should I remember that?",
    "Your summary has been saved to notes.md.",
    "Your favorite language is Python.",
])
def test_recall_negation_and_file_saves_are_not_save_claims(reply) -> None:
    assert Channel.MEMORY not in claims(reply)


async def test_a_reply_claiming_a_save_is_corrected_before_the_user_sees_it(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """The model claims a save on both attempts; the application answers from
    the record instead -- and nothing was stored, so it must not say so."""
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    fake_provider.replies = ["Got it, I've saved your description.",
                             "Done, I've stored that in my memory."]
    body = await say(client, conversation, "Tall, wears glasses, slender build.")
    content = body["assistant_message"]["content"]
    assert "saved your description" not in content
    assert "can't confirm that I've saved" in content
    async with session_factory() as session:
        assert (await session.execute(select(Memory))).scalars().all() == []


async def test_a_corrected_retry_is_kept(client: AsyncClient, fake_provider) -> None:
    conversation = await new_conversation(client)
    fake_provider.extraction_reply = NOTHING
    fake_provider.replies = ["Got it, I've saved that.", "Thanks -- that's a clear description."]
    body = await say(client, conversation, "Tall, wears glasses, slender build.")
    assert body["assistant_message"]["content"] == "Thanks -- that's a clear description."


def test_the_execution_note_tells_the_model_saving_happens_after_the_reply() -> None:
    from app.synthesis.execution_truth import render_note

    note = render_note(record_for_turn())
    assert "saving anything to long-term memory" in note
    assert "cannot be confirmed, so treat it as having NOT happened" in note
