"""Bounded Telegram Bot API sender used by the webhook adapter."""

from __future__ import annotations

import json
from typing import Optional, Protocol
from urllib.parse import quote

from app.core.config import Settings
from app.integrations.http_client import SecureHttpClient
from app.integrations.policy import NetworkPolicy

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramSendError(Exception):
    """A safe category for an unsuccessful Telegram delivery."""


class TelegramSender(Protocol):
    async def send_text(self, chat_id: int, text: str) -> None: ...


def chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> tuple[str, ...]:
    """Split only for Telegram presentation; never rewrite Mai's answer."""
    if not text:
        return ("",)
    return tuple(text[index : index + limit] for index in range(0, len(text), limit))


class BotApiSender:
    """POSTs to Telegram through Mai's single, policy-enforced HTTP client."""

    def __init__(
        self, settings: Settings, client: Optional[SecureHttpClient] = None
    ) -> None:
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._client = client or SecureHttpClient(
            NetworkPolicy(
                allowed_hosts=frozenset({TELEGRAM_API_HOST}),
                allowed_methods=frozenset({"POST"}),
                follow_redirects=False,
                max_redirects=0,
            )
        )

    def _send_url(self) -> str:
        # The token is configuration, never user input. Quote it to keep the
        # path structure fixed even if an invalid value was configured.
        return "https://%s/bot%s/sendMessage" % (
            TELEGRAM_API_HOST,
            quote(self._token, safe=""),
        )

    async def send_text(self, chat_id: int, text: str) -> None:
        for chunk in chunk_text(text):
            response = await self._client.post_json(
                self._send_url(), {"chat_id": chat_id, "text": chunk}
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise TelegramSendError("telegram_response")
            try:
                payload = json.loads(response.content)
            except (TypeError, ValueError) as exc:
                raise TelegramSendError("telegram_response") from exc
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise TelegramSendError("telegram_response")
