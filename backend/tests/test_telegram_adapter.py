"""Telegram proves itself as an adapter around the established chat path."""

import json
from typing import Optional

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.api.routes.telegram import get_telegram_sender
from app.integrations.http_client import SecureHttpClient
from app.integrations.policy import NetworkPolicy
from app.telegram.client import BotApiSender, TELEGRAM_API_HOST, TelegramSendError, chunk_text


class RecordingSender:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.messages: list[tuple[int, str]] = []
        self.error = error

    async def send_text(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))
        if self.error:
            raise self.error


@pytest_asyncio.fixture
async def telegram(client: AsyncClient, conversation_id, settings):
    settings.TELEGRAM_BOT_TOKEN = "12345:secret-token-not-for-logs"
    settings.TELEGRAM_WEBHOOK_SECRET = "webhook-secret"
    settings.TELEGRAM_ALLOWED_CHAT_ID = "123"
    settings.TELEGRAM_CONVERSATION_ID = str(conversation_id)
    sender = RecordingSender()
    app = client._transport.app  # in-process test transport
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    yield sender
    app.dependency_overrides.pop(get_telegram_sender, None)


def update(*, update_id=1, chat_id=123, text="Hello Mai"):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


async def test_text_update_reaches_existing_conversation_path(
    client: AsyncClient, telegram, fake_provider
) -> None:
    response = await client.post(
        "/api/telegram/webhook",
        json=update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert fake_provider.last_call[-1].content == "Hello Mai"
    assert telegram.messages == [(123, "Hello from Mai.")]


@pytest.mark.parametrize(
    "payload,status_code",
    [
        ({}, 400),  # malformed update
        ({"update_id": 1}, 200),  # no message update
        ({"update_id": 1, "message": {"text": "x"}}, 400),  # no chat
        ({"update_id": 1, "message": {"chat": {"id": 123}}}, 200),  # no text
        ({"update_id": 1, "message": {"chat": {"id": 123}, "text": "   "}}, 400),
    ],
)
async def test_non_text_or_malformed_updates_are_not_conversation_turns(
    client: AsyncClient, telegram, fake_provider, payload, status_code
) -> None:
    response = await client.post(
        "/api/telegram/webhook", json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )
    assert response.status_code == status_code
    assert fake_provider.calls == []


async def test_authorization_and_deduplication_are_adapter_boundaries(
    client: AsyncClient, telegram, fake_provider
) -> None:
    headers = {"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}
    denied = await client.post("/api/telegram/webhook", json=update(chat_id=999), headers=headers)
    assert denied.status_code == 403
    first = await client.post("/api/telegram/webhook", json=update(update_id=8), headers=headers)
    duplicate = await client.post("/api/telegram/webhook", json=update(update_id=8), headers=headers)
    assert first.status_code == duplicate.status_code == 200
    assert len(fake_provider.calls) == 1


async def test_telegram_delivery_failure_does_not_fail_or_repeat_turn(
    client: AsyncClient, telegram, fake_provider
) -> None:
    telegram.error = TelegramSendError("timeout")
    response = await client.post(
        "/api/telegram/webhook", json=update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )
    assert response.status_code == 200
    assert len(fake_provider.calls) == 1


async def test_mai_failure_returns_only_a_safe_telegram_message(
    client: AsyncClient, telegram, fake_provider
) -> None:
    fake_provider.raise_error = RuntimeError("provider details must not escape")
    response = await client.post(
        "/api/telegram/webhook", json=update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )
    assert response.status_code == 200
    assert telegram.messages == [(123, "Sorry, I couldn't process that message.")]


def test_chunking_preserves_the_complete_mai_response() -> None:
    message = "x" * 8_193
    chunks = chunk_text(message)
    assert [len(chunk) for chunk in chunks] == [4096, 4096, 1]
    assert "".join(chunks) == message


async def test_bot_sender_is_fixed_to_telegram_host_and_uses_bounded_client(settings) -> None:
    settings.TELEGRAM_BOT_TOKEN = "123:token"
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    sender = BotApiSender(
        settings,
        SecureHttpClient(
            NetworkPolicy(allowed_hosts=frozenset({TELEGRAM_API_HOST}), allowed_methods=frozenset({"POST"})),
            transport=httpx.MockTransport(handler),
            resolve=lambda *_: [(None, None, None, None, ("149.154.167.220", 443))],
        ),
    )
    await sender.send_text(123, "hello")
    assert requests[0].url.host == TELEGRAM_API_HOST
    assert json.loads(requests[0].content) == {"chat_id": 123, "text": "hello"}


def test_telegram_source_does_not_reach_prohibited_core_components() -> None:
    source = open("app/api/routes/telegram.py", encoding="utf-8").read()
    for prohibited in (
        "TaskRunner", "ExecutionService", "Dispatcher", "GrantService",
        "BackgroundRuntime", "ReminderScheduler", "subprocess", "eval(", "exec(",
    ):
        assert prohibited not in source
