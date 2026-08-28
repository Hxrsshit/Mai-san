"""Chat orchestration.

Implements the Stage 1 flow:

    store user message
      -> load this conversation's history
      -> build model context
      -> call the LLM provider
      -> store assistant reply
      -> return both

Context is drawn from the current conversation only. Cross-conversation
memory is explicitly out of scope for Stage 1.
"""

import uuid
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.database.models import Conversation, Message, MessageRole
from app.llm.base import LLMMessage, LLMProvider
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        settings: Optional[Settings] = None,
        conversation_service: Optional[ConversationService] = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._settings = settings or get_settings()
        self._conversations = conversation_service or ConversationService(session)

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
        context = self._build_context(history)

        logger.info(
            "Chat turn started",
            extra={
                "conversation_id": str(conversation_id),
                "context_messages": len(context),
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

    def _build_context(self, history: List[Message]) -> List[LLMMessage]:
        """Turn stored rows into the message list sent to the model.

        A system prompt is always prepended; any system messages that happen to
        be stored in the conversation are preserved in place after it.
        """
        context: List[LLMMessage] = []

        system_prompt = self._settings.MAI_SYSTEM_PROMPT.strip()
        if system_prompt:
            context.append(LLMMessage(role="system", content=system_prompt))

        for message in history:
            context.append(
                LLMMessage(role=message.role.value, content=message.content)
            )
        return context
