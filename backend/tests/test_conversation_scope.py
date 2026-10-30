"""Stage 7B unit 4: conversations and messages are bounded by a knowledge scope.

A conversation belongs to the scope that created it. Another member of the
same organisation, a member of another organisation, and an unattributed
legacy conversation are all "not found" -- by id, by enumeration, for
messages and for the memory-extraction context -- exactly like a missing id.
All rows are synthetic.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select

from app.core.errors import ConversationNotFoundError
from app.database.models import Conversation, Message, MessageRole
from app.identity.scope import InvalidScope
from app.memory.service import MemoryService
from app.services.conversation_service import ConversationService
from tests.scopes import ADMIN_SCOPE, COLLEAGUE_SCOPE, OUTSIDER_SCOPE

pytestmark = pytest.mark.anyio

OTHERS = [pytest.param(COLLEAGUE_SCOPE, id="colleague"), pytest.param(OUTSIDER_SCOPE, id="outsider")]


async def _admin_conversation(session):
    service = ConversationService(session, ADMIN_SCOPE)
    conversation = await service.create_conversation("admin's")
    await service.add_message(conversation.id, MessageRole.USER, "my private note")
    await session.commit()
    return conversation.id


def test_the_service_refuses_anything_but_a_scope() -> None:
    for bogus in (None, "personal", ADMIN_SCOPE.write_columns(), uuid.uuid4()):
        with pytest.raises(InvalidScope):
            ConversationService(object(), bogus)  # type: ignore[arg-type]


async def test_a_new_conversation_carries_its_creators_scope(db_session) -> None:
    conversation_id = await _admin_conversation(db_session)
    row = (await db_session.execute(
        select(Conversation.organisation_id, Conversation.scope_id, Conversation.scope_visibility)
        .where(Conversation.id == conversation_id))).one()
    assert tuple(row) == (ADMIN_SCOPE.organisation_id, ADMIN_SCOPE.scope_id, "personal")


@pytest.mark.parametrize("other", OTHERS)
async def test_another_scope_cannot_read_the_conversation(session_factory, other) -> None:
    async with session_factory() as session:
        conversation_id = await _admin_conversation(session)
    async with session_factory() as session:
        service = ConversationService(session, other)
        with pytest.raises(ConversationNotFoundError):
            await service.get_conversation(conversation_id)
        with pytest.raises(ConversationNotFoundError):
            await service.get_messages(conversation_id)
        assert await service.list_conversations() == []
        assert await service.count_conversations() == 0


@pytest.mark.parametrize("other", OTHERS)
async def test_another_scope_cannot_change_the_conversation(session_factory, other) -> None:
    async with session_factory() as session:
        conversation_id = await _admin_conversation(session)
    async with session_factory() as session:
        service = ConversationService(session, other)
        with pytest.raises(ConversationNotFoundError):
            await service.rename_conversation(conversation_id, "taken over")
        with pytest.raises(ConversationNotFoundError):
            await service.add_message(conversation_id, MessageRole.USER, "injected")
        with pytest.raises(ConversationNotFoundError):
            await service.delete_conversation(conversation_id)
        await session.commit()
    async with session_factory() as session:
        admin = ConversationService(session, ADMIN_SCOPE)
        conversation = await admin.get_conversation(conversation_id)
        assert conversation.title == "admin's"
        assert [m.content for m in await admin.get_messages(conversation_id)] == ["my private note"]


async def test_a_conversation_already_loaded_in_the_session_is_still_refused(db_session) -> None:
    """The identity map would hand back a loaded row without a filter; the
    service must not use it."""
    conversation_id = await _admin_conversation(db_session)
    await ConversationService(db_session, ADMIN_SCOPE).get_conversation(conversation_id)
    with pytest.raises(ConversationNotFoundError):
        await ConversationService(db_session, COLLEAGUE_SCOPE).get_conversation(conversation_id)


async def test_a_legacy_unattributed_conversation_is_quarantined(db_session) -> None:
    """No scope means unattributed: no member sees it, not even the Admin."""
    legacy = uuid.uuid4()
    await db_session.execute(insert(Conversation.__table__).values(id=legacy, title="legacy"))
    await db_session.commit()
    service = ConversationService(db_session, ADMIN_SCOPE)
    with pytest.raises(ConversationNotFoundError):
        await service.get_conversation(legacy)
    assert legacy not in [c.id for c in await service.list_conversations()]


async def test_each_scope_sees_only_its_own_conversations(session_factory) -> None:
    async with session_factory() as session:
        for scope in (ADMIN_SCOPE, COLLEAGUE_SCOPE, OUTSIDER_SCOPE):
            await ConversationService(session, scope).create_conversation(f"{scope.scope_id}")
        await session.commit()
    async with session_factory() as session:
        for scope in (ADMIN_SCOPE, COLLEAGUE_SCOPE, OUTSIDER_SCOPE):
            titles = [c.title for c in await ConversationService(session, scope).list_conversations()]
            assert titles == [str(scope.scope_id)]


async def test_extraction_context_is_empty_outside_the_scope(db_session, fake_provider, settings) -> None:
    """The extractor sees earlier turns only from a conversation of the
    turn's own scope."""
    service = ConversationService(db_session, ADMIN_SCOPE)
    conversation = await service.create_conversation("context")
    await service.add_message(conversation.id, MessageRole.USER, "earlier secret")
    await service.add_message(conversation.id, MessageRole.ASSISTANT, "earlier reply")
    source = await service.add_message(conversation.id, MessageRole.USER, "now")
    await db_session.commit()
    memory = MemoryService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE)

    own = await memory._earlier_context(conversation.id, source.id, ADMIN_SCOPE)
    assert any("earlier secret" in line for line in own)
    for other in (COLLEAGUE_SCOPE, OUTSIDER_SCOPE):
        assert await memory._earlier_context(conversation.id, source.id, other) == []


async def test_the_api_returns_404_for_another_members_conversation(
    client: AsyncClient, session_factory
) -> None:
    """The authenticated Admin (the client) cannot reach a colleague's
    conversation, and it is not enumerated."""
    async with session_factory() as session:
        theirs = await ConversationService(session, COLLEAGUE_SCOPE).create_conversation("theirs")
        await session.commit()
    assert (await client.get(f"/api/conversations/{theirs.id}")).status_code == 404
    assert (await client.delete(f"/api/conversations/{theirs.id}")).status_code == 404
    listed = (await client.get("/api/conversations")).json()
    assert str(theirs.id) not in str(listed)
    async with session_factory() as session:
        still = (await session.execute(select(Conversation.id).where(Conversation.id == theirs.id))).first()
        assert still is not None
        assert (await session.execute(select(Message.id).where(Message.conversation_id == theirs.id))).first() is None


async def test_a_model_named_source_message_from_another_conversation_is_not_trusted(
    db_session, fake_provider, settings
) -> None:
    """The extractor's output may name a source message, but only one of this
    turn's own conversation counts. Naming another member's message falls
    back to the turn's own user message, so model output cannot point a
    memory's provenance into another scope."""
    import json

    theirs_service = ConversationService(db_session, COLLEAGUE_SCOPE)
    theirs = await theirs_service.create_conversation("colleague's")
    foreign = await theirs_service.add_message(theirs.id, MessageRole.USER, "their message")
    mine_service = ConversationService(db_session, ADMIN_SCOPE)
    mine = await mine_service.create_conversation("admin's")
    own = await mine_service.add_message(mine.id, MessageRole.USER, "I prefer tea.")
    fake_provider.extraction_reply = json.dumps({"should_store_memory": True, "memories": [{
        "content": "User prefers tea.", "memory_type": "preference",
        "importance_score": 8, "confidence_score": 0.9,
        "source_message_id": str(foreign.id),
    }]})

    stored = await MemoryService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE) \
        .extract_and_store(conversation_id=mine.id, user_message="I prefer tea.",
                           assistant_message="Noted.", source_message_id=own.id, scope=ADMIN_SCOPE)

    assert len(stored) == 1
    assert stored[0].source_message_id == own.id
    assert stored[0].source_conversation_id == mine.id


# --- HTTP layer (security review F5) ------------------------------------------


async def _colleagues(session_factory):
    async with session_factory() as session:
        service = ConversationService(session, COLLEAGUE_SCOPE)
        theirs = await service.create_conversation("theirs")
        await service.add_message(theirs.id, MessageRole.USER, "their secret")
        await session.commit()
    return theirs.id


async def test_the_api_cannot_rename_read_or_post_to_another_members_conversation(
    client: AsyncClient, session_factory, fake_provider
) -> None:
    theirs = await _colleagues(session_factory)
    assert (await client.patch(f"/api/conversations/{theirs}", json={"title": "x"})).status_code == 404
    assert (await client.get(f"/api/conversations/{theirs}/messages")).status_code == 404
    before = (len(fake_provider.calls), len(fake_provider.intent_calls))
    posted = await client.post(f"/api/conversations/{theirs}/messages", json={"content": "hi"})
    assert posted.status_code == 404
    # Refused before understanding: no model call of any kind.
    assert (len(fake_provider.calls), len(fake_provider.intent_calls)) == before
    preview = await client.get(f"/api/conversations/{theirs}/context-preview", params={"q": "secret"})
    assert "their secret" not in preview.text
    async with session_factory() as session:
        admin_view = ConversationService(session, COLLEAGUE_SCOPE)
        assert (await admin_view.get_conversation(theirs)).title == "theirs"
        assert [m.content for m in await admin_view.get_messages(theirs)] == ["their secret"]


async def test_debug_endpoints_do_not_read_another_members_history(
    client: AsyncClient, session_factory
) -> None:
    theirs = await _colleagues(session_factory)
    for path in ("/api/context/debug", "/api/prompt/debug"):
        response = await client.post(path, json={"message": "what did I say?", "conversation_id": str(theirs)})
        assert response.status_code in (200, 404), (path, response.status_code)  # not a 422
        assert "their secret" not in response.text, path


async def test_a_mismatched_turn_scope_is_refused_before_anything_runs(
    db_session, fake_provider, settings
) -> None:
    """Review F3: the turn and its extraction share one scope."""
    from fastapi import BackgroundTasks

    from app.api.routes.conversations import send_conversation_turn
    from app.services.chat_service import ChatService

    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    chat = ChatService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE)
    with pytest.raises(InvalidScope):
        await send_conversation_turn(
            conversation_id=conversation.id, content="hi", chat=chat,
            background_tasks=BackgroundTasks(), settings=settings, provider=fake_provider,
            session_factory=None, session=db_session, scope=COLLEAGUE_SCOPE,
        )
    assert fake_provider.calls == []
    with pytest.raises(InvalidScope):
        ChatService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE,
                    conversation_service=ConversationService(db_session, COLLEAGUE_SCOPE))


async def test_extraction_stores_nothing_for_a_turn_outside_its_scope(
    db_session, fake_provider, settings
) -> None:
    """Review F1: the scope bounds the write, not only the context -- even
    with no earlier context and no source message."""
    import json

    from app.memory.models import Memory

    theirs = await ConversationService(db_session, COLLEAGUE_SCOPE).create_conversation()
    fake_provider.extraction_reply = json.dumps({"should_store_memory": True, "memories": [{
        "content": "User prefers tea.", "memory_type": "preference",
        "importance_score": 8, "confidence_score": 0.9}]})
    settings.MEMORY_EXTRACTION_CONTEXT_MESSAGES = 0
    stored = await MemoryService(session=db_session, provider=fake_provider, settings=settings, scope=ADMIN_SCOPE) \
        .extract_and_store(conversation_id=theirs.id, user_message="I prefer tea.",
                           assistant_message="Noted.", scope=ADMIN_SCOPE)
    assert stored == []
    assert fake_provider.extraction_calls == []
    assert (await db_session.execute(select(Memory.id))).first() is None


async def test_telegram_with_an_unattributed_conversation_runs_no_turn(
    client: AsyncClient, session_factory, settings, fake_provider
) -> None:
    """Review F4: configured with a legacy (unattributed) conversation, the
    Admin's Telegram message produces no turn, no message and no model call,
    only the generic reply."""
    from app.api.routes.telegram import get_telegram_sender

    legacy = uuid.uuid4()
    async with session_factory() as session:
        await session.execute(insert(Conversation.__table__).values(id=legacy, title="legacy"))
        await session.commit()
    sent = []

    class Sender:
        async def send_text(self, chat_id, text):
            sent.append((chat_id, text))

    settings.TELEGRAM_BOT_TOKEN = "12345:secret-token-not-for-logs"
    settings.TELEGRAM_WEBHOOK_SECRET = "webhook-secret"
    settings.TELEGRAM_ALLOWED_CHAT_ID = "123"
    settings.TELEGRAM_CONVERSATION_ID = str(legacy)
    app = client._transport.app
    app.dependency_overrides[get_telegram_sender] = lambda: Sender()
    try:
        response = await client.post("/api/telegram/webhook", headers={
            "X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}, json={
            "update_id": 1, "message": {"chat": {"id": 123, "type": "private"},
                                        "from": {"id": 123, "is_bot": False}, "text": "hello"}})
    finally:
        app.dependency_overrides.pop(get_telegram_sender, None)
    assert response.status_code == 200
    assert fake_provider.calls == []
    assert sent == [(123, "Sorry, I couldn't process that message.")]
    async with session_factory() as session:
        assert (await session.execute(select(Message.id).where(Message.conversation_id == legacy))).first() is None
