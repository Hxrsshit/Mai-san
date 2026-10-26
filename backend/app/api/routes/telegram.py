"""Telegram webhook adapter: a thin translation layer with no Mai intelligence.

What it writes and keeps. Redeeming a `/link` code writes the identity row,
consumes the link code and activates an invited member, all through
`MembershipService`, and the webhook commits that only when the redemption
succeeded; every other outcome is rolled back. It also keeps two pieces of
process-local, in-memory state on `app.state`: the `update_id` deduplicator
and the failure-reply rate limit. It keeps nothing else, and it writes chat
rows only through the existing conversation path.

Stage 7A. After the secret and a minimal shape check, every update is
deduplicated on `update_id` and then classified by the pure
`app.telegram.updates.classify`. There are exactly three outcomes:

- LINK_COMMAND: a private message whose first token starts with `/link`
  redeems a link code through `MembershipService` and gets an
  application-written reply. It never reaches the chat path, so a code never
  reaches the model, `messages` or memory.
- PRIVATE_TEXT: only the Admin's operator-configured private chat, with no
  member identity attached to it and an active Admin, gets a chat turn,
  through the existing conversation path, unchanged. There are no employee
  turns in this stage.
- IGNORED: everything else, including groups, supergroups and channels (the
  Admin's too), forwards, bots and edits, is acknowledged and ignored.

Logs carry reason codes and `update_id` only: never text, a code, a Telegram
id or a chat id. A database error during linking or the Admin check never
becomes a turn and is logged without exception text, because the driver's
message can quote the bound parameters. For the same reason the rollback on
those paths is quiet (a failing rollback must not escape to the global
handler, which logs the exception chain) and runs after the `try` statement,
outside any `except`, so nothing logged during it can chain to the original
error.
"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Callable, Deque, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import AppSettings, Chat, DbSession, Provider, SessionFactory
from app.api.routes.conversations import send_conversation_turn
from app.core.config import Settings
from app.core.logging import get_logger
from app.identity.constants import LEGACY_ADMIN_MEMBER_ID, TELEGRAM_PROVIDER
from app.identity.schemas import RedeemResult
from app.identity.service import MembershipService
from app.schemas.message import MessageCreate
from app.telegram.client import BotApiSender, TelegramSendError, TelegramSender
from app.telegram.updates import ClassifiedUpdate, UpdateKind, classify

router = APIRouter(prefix="/api/telegram", tags=["telegram"])
logger = get_logger(__name__)
_SEEN_LIMIT = 4096

#: Replies to `/link`. Every refusal and every error is the same text, so a
#: sender cannot tell an unknown code from a used, expired or refused one.
LINKED_REPLY = "Linked. Your admin can now see this account as linked."
INVALID_LINK_REPLY = "That code isn't valid. Ask your admin for a new one."

#: At most this many failure replies per window, per process. Past it the
#: update is still acknowledged, without a reply.
FAILURE_REPLY_LIMIT = 20
FAILURE_REPLY_WINDOW_SECONDS = 60.0


class TelegramChat(BaseModel):
    id: int
    model_config = ConfigDict(extra="ignore")


class TelegramMessage(BaseModel):
    # `chat.type`, `from` and the forward fields are deliberately not part of
    # this model: their absence must be IGNORED by `classify`, never a 400.
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


class FailureReplyLimiter:
    """A process-local sliding window over failure replies to `/link`.

    It bounds how much a sender guessing codes can make the bot say; it is
    not a lockout and stores nothing about who asked. `allow` never awaits,
    so it is atomic on the event loop.
    """

    def __init__(
        self,
        limit: int = FAILURE_REPLY_LIMIT,
        window_seconds: float = FAILURE_REPLY_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._sent: Deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._sent and now - self._sent[0] >= self._window:
            self._sent.popleft()
        if len(self._sent) >= self._limit:
            return False
        self._sent.append(now)
        return True


def get_telegram_sender(settings: AppSettings) -> TelegramSender:
    return BotApiSender(settings)


def _deduplicator(request: Request) -> UpdateDeduplicator:
    existing = getattr(request.app.state, "telegram_update_deduplicator", None)
    if existing is None:
        existing = UpdateDeduplicator()
        request.app.state.telegram_update_deduplicator = existing
    return existing


def _failure_limiter(request: Request) -> FailureReplyLimiter:
    existing = getattr(request.app.state, "telegram_link_failure_limiter", None)
    if existing is None:
        existing = FailureReplyLimiter()
        request.app.state.telegram_link_failure_limiter = existing
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


#: What a database failure looks like here: SQLAlchemy's errors and the
#: driver's socket errors (the same pair `app.api.deps` uses).
_DATABASE_FAILURES = (SQLAlchemyError, OSError)


async def _rollback_quietly(session: DbSession) -> None:
    """End the transaction on a path that is already failing, without letting
    a second database error escape. An escaping error would reach the global
    handler, which logs the exception chain, and the original error's text
    carries the bound parameters (a Telegram id).

    If the rollback itself fails while a transaction is still open, the
    session is closed, which discards that transaction, so the request's
    session dependency can never commit it afterwards.
    """
    try:
        await session.rollback()
        return
    except _DATABASE_FAILURES:
        pass
    if session.in_transaction():
        try:
            await session.close()
        except _DATABASE_FAILURES:
            pass


def _ignored(update_id: int, reason: str) -> JSONResponse:
    # A reason code and the update id only.
    logger.info("Ignored Telegram update", extra={"category": reason, "update_id": update_id})
    return JSONResponse(content={"ok": True, "ignored": True})


async def _reply(sender: TelegramSender, chat_id: int, text: str) -> None:
    """Send an application-written reply. A failure is logged by category only."""
    try:
        await sender.send_text(chat_id, text)
    except TelegramSendError:
        logger.warning("Telegram delivery failed", extra={"category": "telegram_api"})
    except Exception:
        logger.warning("Telegram delivery failed", extra={"category": "unexpected"})


async def _link(
    request: Request,
    classified: ClassifiedUpdate,
    update_id: int,
    settings: Settings,
    session: DbSession,
    sender: TelegramSender,
) -> JSONResponse:
    """Redeem a link code. Never a chat turn; always 200.

    The only commit is after LINKED. INVALID, a database error and anything
    unexpected all roll back and get the same generic reply.
    """
    linked = False
    failure: Optional[str] = None
    error_class: Optional[str] = None
    try:
        # Redemption starts from a clean transaction, so the commit below can
        # only ever make this link durable, nothing else.
        if session.in_transaction():
            await session.rollback()
        result = await MembershipService(session, settings).redeem_link_code(
            classified.code_text or "", TELEGRAM_PROVIDER, str(classified.sender_id)
        )
        if result is RedeemResult.LINKED:
            await session.commit()
            linked = True
        else:
            await session.rollback()
    except _DATABASE_FAILURES:
        # Only a flag here. The driver's message quotes the bound parameters,
        # which include the subject, so the error is never logged and is no
        # longer being handled when the rollback below runs.
        failure = "database"
    except Exception as exc:
        # For example a code consumed concurrently. The class name only,
        # never the text, for the same reason as above.
        failure = "unexpected"
        error_class = type(exc).__name__

    if failure is not None:
        # Outside any `except`: no exception is being handled, so nothing the
        # rollback logs can chain (`__context__`) to the original error.
        # Rolled back, never committed; the same generic reply follows.
        await _rollback_quietly(session)
        extra = {"category": failure}
        if error_class is not None:
            extra["error_class"] = error_class
        logger.warning("Telegram link failed", extra=extra)

    if linked:
        logger.info("Telegram link handled", extra={"category": "linked", "update_id": update_id})
        await _reply(sender, classified.chat_id, LINKED_REPLY)
    elif _failure_limiter(request).allow():
        logger.info("Telegram link handled", extra={"category": "invalid", "update_id": update_id})
        await _reply(sender, classified.chat_id, INVALID_LINK_REPLY)
    else:
        logger.info(
            "Telegram link handled", extra={"category": "reply_rate_limited", "update_id": update_id}
        )
    return JSONResponse(content={"ok": True})


async def _admin_turn(
    classified: ClassifiedUpdate,
    update_id: int,
    settings: Settings,
    chat: Chat,
    session: DbSession,
    provider: Provider,
    session_factory: SessionFactory,
    sender: TelegramSender,
) -> JSONResponse:
    """ADMIN_PRIVATE: the one Telegram path to a chat turn.

    Only the operator-configured private chat, only while no member identity
    is attached to that account, and only while the Admin is active.
    """
    allowed_chat_id = _allowed_chat_id(settings)
    if allowed_chat_id is None or allowed_chat_id <= 0 or classified.chat_id != allowed_chat_id:
        return _ignored(update_id, "not_admin_chat")

    service = MembershipService(session, settings)
    refusal: Optional[str] = None
    check_failed = False
    try:
        if await service.has_attached_identity(TELEGRAM_PROVIDER, str(classified.sender_id)):
            refusal = "member_identity"
        elif await service.active_principal(LEGACY_ADMIN_MEMBER_ID) is None:
            refusal = "admin_unavailable"
    except _DATABASE_FAILURES:
        # Only a flag here; the rollback and the log run below, outside any
        # `except`, for the same reason as in `_link`.
        check_failed = True
    if check_failed:
        # A database error never becomes a turn. No exception text.
        await _rollback_quietly(session)
        logger.warning("Telegram admin check failed", extra={"category": "database"})
        return _ignored(update_id, "database")
    if refusal is not None:
        await _rollback_quietly(session)
        return _ignored(update_id, refusal)

    try:
        content = MessageCreate(content=classified.text).content
    except ValidationError:
        logger.warning("Rejected Telegram webhook", extra={"category": "invalid_text"})
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": "Invalid message"})

    tasks = BackgroundTasks()
    try:
        result = await send_conversation_turn(
            conversation_id=_conversation_id(settings),  # checked by the caller
            content=content,
            chat=chat,
            background_tasks=tasks,
            settings=settings,
            provider=provider,
            session_factory=session_factory,
            session=session,
            # Stage 6N. No owner: Telegram is authenticated by chat id, not
            # by the owner seam, so a monitor is never set up from here --
            # the request is explained and nothing is persisted.
            owner_id=None,
        )
    except Exception:  # Existing API handlers classify the underlying failure.
        # The normal HTTP route lets its dependency roll this transaction
        # back. This adapter handles the failure locally so Telegram gets a
        # safe reply, therefore it must preserve that same rollback rule.
        await session.rollback()
        logger.exception("Telegram conversation turn failed", extra={"update_id": update_id})
        try:
            await sender.send_text(classified.chat_id, "Sorry, I couldn't process that message.")
        except Exception:
            logger.warning("Telegram delivery failed", extra={"category": "safe_error"})
        return JSONResponse(content={"ok": True})

    try:
        await sender.send_text(classified.chat_id, result.assistant_message.content)
    except TelegramSendError:
        logger.warning("Telegram delivery failed", extra={"category": "telegram_api"})
    except Exception:
        logger.warning("Telegram delivery failed", extra={"category": "unexpected"})
    finally:
        # This runs the exact post-turn work queued by the normal route only
        # after the response has been sent to Telegram.
        await tasks()
    return JSONResponse(content={"ok": True})


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
    """Classify one Telegram update: link, the Admin's turn, or ignored."""
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

    # Before any branch: a retried update is never redeemed or answered twice.
    if not await _deduplicator(request).add_if_new(update.update_id):
        return JSONResponse(content={"ok": True, "duplicate": True})

    classified = classify(payload)
    if classified.kind is UpdateKind.LINK_COMMAND:
        return await _link(request, classified, update.update_id, settings, session, sender)
    if classified.kind is UpdateKind.PRIVATE_TEXT:
        return await _admin_turn(
            classified, update.update_id, settings, chat, session, provider, session_factory, sender
        )
    return _ignored(update.update_id, classified.reason)
