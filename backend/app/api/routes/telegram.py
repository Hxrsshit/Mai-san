"""Telegram webhook adapter; it contains no Mai intelligence or state."""

from __future__ import annotations

import asyncio
import hmac
import uuid
from collections import OrderedDict
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from app.api.deps import AppSettings, Chat, DbSession, Provider, SessionFactory
from app.api.routes.conversations import send_conversation_turn
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.message import MessageCreate
from app.telegram.client import BotApiSender, TelegramSendError, TelegramSender

router = APIRouter(prefix="/api/telegram", tags=["telegram"])
logger = get_logger(__name__)
_SEEN_LIMIT = 4096


class TelegramChat(BaseModel):
    id: int
    model_config = ConfigDict(extra="ignore")


class TelegramMessage(BaseModel):
    chat: TelegramChat
    text: Optional[str] = None
    model_config = ConfigDict(extra="ignore")


class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TelegramMessage] = None
    model_config = ConfigDict(extra="ignore")


class UpdateDeduplicator:
    """Small, process-local retry guard; deliberately not a second datastore."""

    def __init__(self, limit: int = _SEEN_LIMIT) -> None:
        self._limit = limit
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._lock = asyncio.Lock()

    async def add_if_new(self, update_id: int) -> bool:
        async with self._lock:
            if update_id in self._seen:
                return False
            self._seen[update_id] = None
            if len(self._seen) > self._limit:
                self._seen.popitem(last=False)
            return True


def get_telegram_sender(settings: AppSettings) -> TelegramSender:
    return BotApiSender(settings)


def _deduplicator(request: Request) -> UpdateDeduplicator:
    existing = getattr(request.app.state, "telegram_update_deduplicator", None)
    if existing is None:
        existing = UpdateDeduplicator()
        request.app.state.telegram_update_deduplicator = existing
    return existing


def _configured(settings: Settings) -> bool:
    return bool(
        settings.TELEGRAM_BOT_TOKEN
        and settings.TELEGRAM_WEBHOOK_SECRET
        and _allowed_chat_id(settings) is not None
        and settings.TELEGRAM_CONVERSATION_ID
    )


def _conversation_id(settings: Settings) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(settings.TELEGRAM_CONVERSATION_ID)
    except (AttributeError, ValueError, TypeError):
        return None


def _allowed_chat_id(settings: Settings) -> Optional[int]:
    try:
        return int(settings.TELEGRAM_ALLOWED_CHAT_ID)
    except (TypeError, ValueError):
        return None


@router.post("/webhook", status_code=status.HTTP_200_OK, summary="Receive an approved Telegram update")
async def telegram_webhook(
    request: Request,
    settings: AppSettings,
    chat: Chat,
    session: DbSession,
    provider: Provider,
    session_factory: SessionFactory,
    sender: TelegramSender = Depends(get_telegram_sender),
    webhook_secret: Optional[str] = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
) -> JSONResponse:
    """Translate one Telegram text update into the normal Mai turn path."""
    if not _configured(settings) or _conversation_id(settings) is None:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not found"})
    if webhook_secret is None or not hmac.compare_digest(
        webhook_secret, settings.TELEGRAM_WEBHOOK_SECRET
    ):
        logger.warning("Rejected Telegram webhook", extra={"category": "authentication"})
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Unauthorized"})

    try:
        payload: Any = await request.json()
        update = TelegramUpdate.model_validate(payload)
    except (ValueError, ValidationError):
        logger.warning("Rejected Telegram webhook", extra={"category": "malformed"})
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "Invalid update"})

    # Telegram also sends non-message updates. They are valid but do not
    # represent conversational input, so acknowledging them prevents retries.
    if update.message is None or update.message.text is None:
        return JSONResponse(content={"ok": True, "ignored": True})
    try:
        content = MessageCreate(content=update.message.text).content
    except ValidationError:
        logger.warning("Rejected Telegram webhook", extra={"category": "invalid_text"})
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "Invalid message"})
    if update.message.chat.id != _allowed_chat_id(settings):
        logger.warning("Rejected Telegram webhook", extra={"category": "unauthorized_chat"})
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": "Forbidden"})
    if not await _deduplicator(request).add_if_new(update.update_id):
        return JSONResponse(content={"ok": True, "duplicate": True})

    tasks = BackgroundTasks()
    try:
        result = await send_conversation_turn(
            conversation_id=_conversation_id(settings),  # checked above
            content=content,
            chat=chat,
            background_tasks=tasks,
            settings=settings,
            provider=provider,
            session_factory=session_factory,
            session=session,
        )
    except Exception:  # Existing API handlers classify the underlying failure.
        # The normal HTTP route lets its dependency roll this transaction
        # back. This adapter handles the failure locally so Telegram gets a
        # safe reply, therefore it must preserve that same rollback rule.
        await session.rollback()
        logger.exception("Telegram conversation turn failed", extra={"update_id": update.update_id})
        try:
            await sender.send_text(update.message.chat.id, "Sorry, I couldn't process that message.")
        except Exception:
            logger.warning("Telegram delivery failed", extra={"category": "safe_error"})
        return JSONResponse(content={"ok": True})

    try:
        await sender.send_text(update.message.chat.id, result.assistant_message.content)
    except TelegramSendError:
        logger.warning("Telegram delivery failed", extra={"category": "telegram_api"})
    except Exception:
        logger.warning("Telegram delivery failed", extra={"category": "unexpected"})
    finally:
        # This runs the exact post-turn work queued by the normal route only
        # after the response has been sent to Telegram.
        await tasks()
    return JSONResponse(content={"ok": True})
