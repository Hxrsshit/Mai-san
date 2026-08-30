"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.service import ContextService
from app.core.config import Settings, get_settings
from app.database.session import get_db_session, get_session_factory
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.entities.service import EntityService
from app.memory.service import MemoryService
from app.relationships.service import RelationshipService
from app.retrieval.service import RetrievalService
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Provider = Annotated[LLMProvider, Depends(get_llm_provider)]
# Background work needs a *new* session -- the request-scoped one is closed by
# the time it runs. Exposed as a dependency so tests can override it.
SessionFactory = Annotated[object, Depends(get_session_factory)]


def get_conversation_service(session: DbSession) -> ConversationService:
    return ConversationService(session)


def get_context_service(
    session: DbSession, settings: AppSettings
) -> ContextService:
    return ContextService(session=session, settings=settings)


def get_retrieval_service(
    session: DbSession, settings: AppSettings
) -> RetrievalService:
    return RetrievalService(session=session, settings=settings)


def get_chat_service(
    session: DbSession, provider: Provider, settings: AppSettings
) -> ChatService:
    return ChatService(
        session=session,
        provider=provider,
        settings=settings,
        retrieval_service=RetrievalService(session=session, settings=settings),
    )


def get_memory_service(
    session: DbSession, provider: Provider, settings: AppSettings
) -> MemoryService:
    return MemoryService(session=session, provider=provider, settings=settings)


def get_entity_service(
    session: DbSession, provider: Provider, settings: AppSettings
) -> EntityService:
    return EntityService(session=session, provider=provider, settings=settings)


def get_relationship_service(
    session: DbSession, provider: Provider, settings: AppSettings
) -> RelationshipService:
    return RelationshipService(session=session, provider=provider, settings=settings)


Conversations = Annotated[ConversationService, Depends(get_conversation_service)]
Chat = Annotated[ChatService, Depends(get_chat_service)]
Memories = Annotated[MemoryService, Depends(get_memory_service)]
Entities = Annotated[EntityService, Depends(get_entity_service)]
Relationships = Annotated[RelationshipService, Depends(get_relationship_service)]
Retrieval = Annotated[RetrievalService, Depends(get_retrieval_service)]
Context = Annotated[ContextService, Depends(get_context_service)]
