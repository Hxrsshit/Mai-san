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
from app.intent.service import IntentService
from app.knowledge.service import KnowledgeService
from app.memory.service import MemoryService
from app.orchestration.service import OrchestrationService
from app.planning.service import PlanningService
from app.runtime.facts import build as build_runtime_facts
from app.tools.authorization import AuthorizationService
from app.tools.registry import ToolRegistry, get_registry
from app.prompt.formatter import PromptFormatter
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


def get_prompt_formatter(
    settings: AppSettings, provider: Provider
) -> PromptFormatter:
    """The Stage 3B formatter, configured with instructions and runtime facts.

    Shared by the chat request path and the prompt debug endpoint, so what
    debug shows is produced by the same code that talks to the model.

    The facts are built here rather than inside the formatter: assembling them
    needs `Settings` and the live provider, and the formatter is required to
    know neither. It renders values it is handed.
    """
    return PromptFormatter(
        system_prompt=settings.MAI_SYSTEM_PROMPT,
        runtime_facts=build_runtime_facts(settings=settings, provider=provider),
    )


def get_intent_service(
    session: DbSession, provider: Provider, settings: AppSettings
) -> IntentService:
    """Stage 4A understanding. Read-only, and holds no executor."""
    return IntentService(session=session, provider=provider, settings=settings)


def get_tool_registry() -> ToolRegistry:
    """The application registry. Populated in code at import time."""
    from app.tools import catalog  # noqa: F401  (import for registration)

    return get_registry()


def get_authorization_service() -> AuthorizationService:
    """Stage 4C authorization. No session and no provider: it needs neither."""
    return AuthorizationService(registry=get_tool_registry())


def get_orchestration_service(settings: AppSettings) -> OrchestrationService:
    """Stage 4D orchestration. No session and no provider: it needs neither."""
    return OrchestrationService(
        authorization=get_authorization_service(), settings=settings
    )


def get_planning_service(
    provider: Provider, settings: AppSettings
) -> PlanningService:
    """Stage 4B planning. Takes no session: planning writes nothing."""
    return PlanningService(provider=provider, settings=settings)


def get_chat_service(
    session: DbSession,
    provider: Provider,
    settings: AppSettings,
    context_service: "Context",
    formatter: "Formatter",
    intent_service: "Intent",
    planning_service: "Planning",
    orchestration_service: "Orchestration",
) -> ChatService:
    return ChatService(
        session=session,
        provider=provider,
        settings=settings,
        context_service=context_service,
        prompt_formatter=formatter,
        intent_service=intent_service,
        planning_service=planning_service,
        orchestration_service=orchestration_service,
    )


def get_knowledge_service(
    session: DbSession, settings: AppSettings
) -> KnowledgeService:
    """Stage 3C lifecycle reads. Takes no provider: conflict handling makes
    no model call anywhere."""
    return KnowledgeService(session=session, settings=settings)


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
Formatter = Annotated[PromptFormatter, Depends(get_prompt_formatter)]
Knowledge = Annotated[KnowledgeService, Depends(get_knowledge_service)]
Intent = Annotated[IntentService, Depends(get_intent_service)]
Planning = Annotated[PlanningService, Depends(get_planning_service)]
Tools = Annotated[ToolRegistry, Depends(get_tool_registry)]
Authorization = Annotated[AuthorizationService, Depends(get_authorization_service)]
Orchestration = Annotated[
    OrchestrationService, Depends(get_orchestration_service)
]
