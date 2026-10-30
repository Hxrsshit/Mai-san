"""Conversation and message persistence.

All database access for conversations lives here so routes stay thin and the
chat service has one place to read/write history.

Stage 7B unit 4: every conversation belongs to a knowledge scope, and the
service is bound to one at construction. Every read, update and delete is
filtered by `scope.clause(Conversation)`, and every new conversation is
written with `scope.write_columns()`. A conversation outside the scope -- another
member's, another organisation's, or an unattributed legacy one -- is "not
found", exactly like a missing id, so its existence is never revealed.
Messages have no scope of their own: they are read and written only through a
conversation of this scope (`docs/stage7b_ownership_design.md`, section 3).
"""

import uuid
from typing import List, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.core.errors import ConversationNotFoundError, DatabaseError
from app.core.logging import get_logger
from app.database.models.base import utcnow
from app.database.models import (
    DEFAULT_CONVERSATION_TITLE,
    Conversation,
    Message,
    MessageRole,
)
from app.identity.scope import InvalidScope, KnowledgeScope

logger = get_logger(__name__)

# Driver connection failures surface as bare OSErrors (asyncpg raises
# ConnectionRefusedError before SQLAlchemy can wrap them), so both must be
# treated as database errors -- otherwise a down database returns an opaque
# 500 instead of a 503.
_DB_ERRORS = (SQLAlchemyError, OSError)

# Conversations are auto-titled from the first user message.
_TITLE_MAX_LENGTH = 60


class ConversationService:
    def __init__(self, session: AsyncSession, scope: KnowledgeScope) -> None:
        if not isinstance(scope, KnowledgeScope):
            raise InvalidScope("ConversationService needs a KnowledgeScope")
        self._session = session
        self._scope = scope

    @property
    def scope(self) -> KnowledgeScope:
        return self._scope

    def _in_scope(self):
        return self._scope.clause(Conversation)

    # --- Conversations ------------------------------------------------------

    async def create_conversation(
        self, title: Optional[str] = None
    ) -> Conversation:
        conversation = Conversation(
            title=title or DEFAULT_CONVERSATION_TITLE, **self._scope.write_columns()
        )
        self._session.add(conversation)
        try:
            await self._session.flush()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("create conversation", exc)

        logger.info(
            "Conversation created",
            extra={"conversation_id": str(conversation.id)},
        )
        return conversation

    async def list_conversations(
        self, limit: int = 50, offset: int = 0
    ) -> Sequence[Conversation]:
        """Most recently active conversations first."""
        statement = (
            select(Conversation)
            .where(self._in_scope())
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
            # The list view renders headers only; without noload the selectin
            # relationship would pull every message of every conversation.
            .options(noload(Conversation.messages))
        )
        try:
            result = await self._session.execute(statement)
            return result.scalars().unique().all()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("list conversations", exc)

    async def count_conversations(self) -> int:
        try:
            result = await self._session.execute(
                select(func.count()).select_from(Conversation).where(self._in_scope())
            )
            return int(result.scalar_one())
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("count conversations", exc)

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        """Fetch a conversation of this scope with its messages, or raise 404.

        A filtered SELECT, never `session.get`: the identity map would hand
        back an already-loaded row without applying the scope."""
        try:
            conversation = (
                await self._session.execute(
                    select(Conversation).where(
                        Conversation.id == conversation_id, self._in_scope()
                    )
                )
            ).scalars().first()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("get conversation", exc)

        if conversation is None:
            raise ConversationNotFoundError(
                f"Conversation {conversation_id} does not exist."
            )
        return conversation

    async def delete_conversation(self, conversation_id: uuid.UUID) -> None:
        """Delete a conversation; its messages go with it via ON DELETE CASCADE."""
        try:
            result = await self._session.execute(
                delete(Conversation).where(
                    Conversation.id == conversation_id, self._in_scope()
                )
            )
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("delete conversation", exc)

        if result.rowcount == 0:
            raise ConversationNotFoundError(
                f"Conversation {conversation_id} does not exist."
            )
        logger.info(
            "Conversation deleted", extra={"conversation_id": str(conversation_id)}
        )

    async def rename_conversation(
        self, conversation_id: uuid.UUID, title: str
    ) -> Conversation:
        conversation = await self.get_conversation(conversation_id)
        conversation.title = title
        await self._session.flush()
        return conversation

    # --- Messages -----------------------------------------------------------

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
    ) -> Message:
        await self._require_conversation(conversation_id)
        message = Message(
            conversation_id=conversation_id, role=role, content=content
        )
        self._session.add(message)
        try:
            await self._session.flush()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("add message", exc)

        logger.info(
            "Message stored",
            extra={
                "conversation_id": str(conversation_id),
                "message_id": str(message.id),
                "role": role.value,
                "content_length": len(content),
            },
        )
        return message

    async def get_messages(
        self, conversation_id: uuid.UUID, limit: Optional[int] = None
    ) -> List[Message]:
        """Messages in chronological order.

        When `limit` is set, the *most recent* `limit` messages are returned,
        still oldest-first -- that is the window handed to the model.
        """
        await self._require_conversation(conversation_id)
        statement = select(Message).where(Message.conversation_id == conversation_id)

        try:
            if limit is None:
                result = await self._session.execute(
                    statement.order_by(Message.created_at.asc(), Message.id.asc())
                )
                return list(result.scalars().all())

            result = await self._session.execute(
                statement.order_by(
                    Message.created_at.desc(), Message.id.desc()
                ).limit(limit)
            )
            return list(reversed(result.scalars().all()))
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("get messages", exc)

    async def touch_conversation(self, conversation: Conversation) -> None:
        """Bump updated_at so the conversation sorts to the top of the list.

        Uses a Python datetime rather than func.now(): with expire_on_commit
        disabled, a SQL expression would still be sitting on the instance when
        it is serialised.
        """
        await self._require_conversation(conversation.id)
        conversation.updated_at = utcnow()
        await self._session.flush()

    async def maybe_autotitle(
        self, conversation: Conversation, first_user_message: str
    ) -> None:
        """Give an untitled conversation a name from its first user message."""
        if conversation.title != DEFAULT_CONVERSATION_TITLE:
            return
        await self._require_conversation(conversation.id)

        title = " ".join(first_user_message.split())
        if len(title) > _TITLE_MAX_LENGTH:
            title = title[: _TITLE_MAX_LENGTH - 1].rstrip() + "…"

        if title:
            conversation.title = title
            await self._session.flush()

    # --- Helpers ------------------------------------------------------------

    async def _require_conversation(self, conversation_id: uuid.UUID) -> None:
        """Raise 404 unless the conversation exists in this scope. Messages are
        owned through their conversation, so this guards every message read
        and write."""
        try:
            found = (
                await self._session.execute(
                    select(Conversation.id).where(
                        Conversation.id == conversation_id, self._in_scope()
                    )
                )
            ).first()
        except _DB_ERRORS as exc:
            raise self._wrap_db_error("check conversation", exc)
        if found is None:
            raise ConversationNotFoundError(
                f"Conversation {conversation_id} does not exist."
            )

    @staticmethod
    def _wrap_db_error(action: str, exc: Exception) -> DatabaseError:
        logger.error(
            "Database operation failed",
            extra={"action": action, "error": str(exc)},
            exc_info=exc,
        )
        return DatabaseError(f"Could not {action}.")
