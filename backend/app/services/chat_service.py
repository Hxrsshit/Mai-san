"""Chat orchestration.

Implements the Stage 1 flow:

    store user message
      -> load this conversation's history
      -> build model context
      -> call the LLM provider
      -> store assistant reply
      -> return both

Recent conversation is drawn from the current conversation. Stage 2D adds
retrieved long-term knowledge alongside it, assembled on the request path with
no additional model call.
"""

import uuid
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.database.models import Conversation, Message, MessageRole
from app.llm.base import LLMMessage, LLMProvider
from app.retrieval.schemas import RetrievalResult
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        settings: Optional[Settings] = None,
        conversation_service: Optional[ConversationService] = None,
        retrieval_service: Optional[RetrievalService] = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._settings = settings or get_settings()
        self._conversations = conversation_service or ConversationService(session)
        self._retrieval = retrieval_service or RetrievalService(
            session, self._settings
        )

    async def send_message(
        self, conversation_id: uuid.UUID, content: str
    ) -> Tuple[Message, Message]:
        """Handle one user turn. Returns (user_message, assistant_message).

        Raises ConversationNotFoundError if the conversation does not exist,
        or an LLMError subclass if the model call fails.
        """
        # 404 before writing anything.
        conversation = await self._conversations.get_conversation(conversation_id)

        user_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
        )
        await self._conversations.maybe_autotitle(conversation, content)

        history = await self._conversations.get_messages(
            conversation_id, limit=self._settings.MAX_CONTEXT_MESSAGES
        )

        # Retrieval runs here, on the request path, and adds no model call.
        # It never raises: a failure yields an empty package and the turn
        # proceeds on recent conversation alone.
        knowledge = await self._retrieve(content)
        context = self._build_context(history, knowledge)

        logger.info(
            "Chat turn started",
            extra={
                "conversation_id": str(conversation_id),
                "context_messages": len(context),
                "retrieved_memories": knowledge.metadata.selected_memories,
                "retrieved_relationships": knowledge.metadata.selected_relationships,
            },
        )

        try:
            llm_response = await self._provider.generate_response(context)
        except LLMError:
            # The user message is already persisted. The session is rolled back
            # by the request dependency, so the failed turn leaves no partial
            # state behind and the client can safely retry.
            logger.error(
                "Chat turn failed at the model call",
                extra={"conversation_id": str(conversation_id)},
            )
            raise

        assistant_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=llm_response.content,
        )
        await self._conversations.touch_conversation(conversation)

        logger.info(
            "Chat turn completed",
            extra={
                "conversation_id": str(conversation_id),
                "assistant_message_id": str(assistant_message.id),
                "total_tokens": llm_response.usage.get("total_tokens"),
            },
        )
        return user_message, assistant_message

    async def start_conversation_with_message(
        self, content: str, title: Optional[str] = None
    ) -> Tuple[Conversation, Message, Message]:
        """Convenience path: create a conversation and send its first message."""
        conversation = await self._conversations.create_conversation(title=title)
        user_message, assistant_message = await self.send_message(
            conversation.id, content
        )
        return conversation, user_message, assistant_message

    # --- Internals ----------------------------------------------------------

    async def _retrieve(self, content: str) -> RetrievalResult:
        """Assemble relevant long-term knowledge. Never raises."""
        try:
            return await self._retrieval.retrieve(content)
        except Exception as exc:  # noqa: BLE001 - retrieval must never break chat
            logger.error(
                "Context retrieval failed; continuing without it",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return RetrievalResult(query=content)

    def _build_context(
        self, history: List[Message], knowledge: Optional[RetrievalResult] = None
    ) -> List[LLMMessage]:
        """Turn stored rows into the message list sent to the model.

        Order matters. Retrieved knowledge sits between the system prompt and
        the conversation, so the recent turns -- and the user's current message
        -- come last and stay closest to the model's attention. The knowledge
        block states in its own text that the current message wins if the two
        disagree.
        """
        context: List[LLMMessage] = []

        system_prompt = self._settings.MAI_SYSTEM_PROMPT.strip()
        if system_prompt:
            context.append(LLMMessage(role="system", content=system_prompt))

        if knowledge is not None and not knowledge.is_empty:
            rendered = self._retrieval.render(knowledge)
            if rendered:
                context.append(LLMMessage(role="system", content=rendered))

        for message in history:
            context.append(
                LLMMessage(role=message.role.value, content=message.content)
            )
        return context
