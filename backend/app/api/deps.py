"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.database.session import get_db_session
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Provider = Annotated[LLMProvider, Depends(get_llm_provider)]


def get_conversation_service(session: DbSession) -> ConversationService:
    return ConversationService(session)


def get_chat_service(
    session: DbSession, provider: Provider, settings: AppSettings
) -> ChatService:
    return ChatService(session=session, provider=provider, settings=settings)


Conversations = Annotated[ConversationService, Depends(get_conversation_service)]
Chat = Annotated[ChatService, Depends(get_chat_service)]
