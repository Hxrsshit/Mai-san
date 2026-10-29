"""Shared FastAPI dependencies."""

import uuid
from typing import Annotated, Optional

from fastapi import Depends, Header
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.composition.notification_delivery import notification_delivery_service
from app.context.service import ContextService
from app.core import auth
from app.core.errors import (
    AdminRequired,
    AuthenticationNotConfigured,
    AuthenticationRequired,
    InvalidCredentials,
    MembershipInactive,
    MembershipUnavailable,
    SeamInvariantError,
)
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.session import get_db_session, get_session_factory
from app.delivery.service import NotificationDeliveryService
from app.delivery.status import DeliveryStatusReader
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.entities.service import EntityService
from app.execution.service import ExecutionService
from app.history.service import HistoryImportService
from app.identity.constants import Role
from app.identity.principal import Principal, load_active_principal
from app.identity.scope import KnowledgeScope
from app.identity.service import MembershipService
from app.intent.service import IntentService
from app.knowledge.service import KnowledgeService
from app.memory.service import MemoryService
from app.orchestration.service import OrchestrationService
# RevocationIncomplete is re-exported for the organisation routes: only this
# module imports `app.organisation` (Stage 7A dependency direction).
from app.organisation.revocation import MemberRevocation, RevocationIncomplete
from app.planning.service import PlanningService
from app.tasks.notifications import NotificationService
from app.tasks.requests import MonitorRequestService
from app.tasks.service import TaskService
from app.runtime.facts import build as build_runtime_facts
from app.tools.authorization import AuthorizationService
from app.tools.registry import ToolRegistry, get_registry
from app.prompt.formatter import PromptFormatter
from app.reminders.service import ReminderService
from app.relationships.service import RelationshipService
from app.retrieval.service import RetrievalService
from app.services.chat_service import ChatService
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Provider = Annotated[LLMProvider, Depends(get_llm_provider)]
# Background work needs a *new* session -- the request-scoped one is closed by
# the time it runs. Exposed as a dependency so tests can override it.
SessionFactory = Annotated[object, Depends(get_session_factory)]


def get_conversation_service(session: DbSession, scope: "Scope") -> ConversationService:
    """Stage 7B unit 4: bound to the authenticated member's own scope."""
    return ConversationService(session, scope)


def get_context_service(
    session: DbSession, settings: AppSettings, scope: "Scope"
) -> ContextService:
    return ContextService(session=session, settings=settings, scope=scope)


def get_retrieval_service(
    session: DbSession, settings: AppSettings, scope: "Scope"
) -> RetrievalService:
    return RetrievalService(session=session, settings=settings, scope=scope)


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
    session: DbSession, provider: Provider, settings: AppSettings, scope: "Scope"
) -> IntentService:
    """Stage 4A understanding. Read-only, and holds no executor."""
    return IntentService(session=session, provider=provider, settings=settings, scope=scope)


def get_execution_service(
    session: DbSession, settings: AppSettings, scope: "Scope"
) -> ExecutionService:
    """Built per request, and gated inside rather than here.

    A dependency that raised when `EXECUTION_ENABLED` is false would make the
    endpoints 500 rather than say why, and would leave revocation -- which must
    work with the switch off -- unreachable. The service itself decides which
    operations the switch covers.
    """
    return ExecutionService(session, settings=settings, scope=scope)


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
    scope: "Scope",
) -> ChatService:
    return ChatService(
        session=session,
        provider=provider,
        settings=settings,
        scope=scope,
        context_service=context_service,
        prompt_formatter=formatter,
        intent_service=intent_service,
        planning_service=planning_service,
        orchestration_service=orchestration_service,
    )


def build_chat_service(
    session: AsyncSession, provider: LLMProvider, settings: Settings, scope: KnowledgeScope
) -> ChatService:
    """The chat service for an adapter that authenticates outside the bearer
    seam (Telegram's Admin chat, Stage 7A). Composed exactly as the
    `get_chat_service` dependency chain composes it, so the adapter cannot run
    a different turn. `scope` must come from a principal the adapter has just
    loaded from the database (Stage 7B unit 4)."""
    return get_chat_service(
        session=session,
        provider=provider,
        settings=settings,
        context_service=get_context_service(session, settings, scope),
        formatter=get_prompt_formatter(settings, provider),
        intent_service=get_intent_service(session, provider, settings, scope),
        planning_service=get_planning_service(provider, settings),
        orchestration_service=get_orchestration_service(settings),
        scope=scope,
    )


def get_knowledge_service(
    session: DbSession, settings: AppSettings, scope: "Scope"
) -> KnowledgeService:
    """Stage 3C lifecycle reads. Takes no provider: conflict handling makes
    no model call anywhere."""
    return KnowledgeService(session=session, settings=settings, scope=scope)


def get_memory_service(
    session: DbSession, provider: Provider, settings: AppSettings, scope: "Scope"
) -> MemoryService:
    return MemoryService(session=session, provider=provider, settings=settings, scope=scope)


def get_history_import_service(
    session: DbSession, provider: Provider, settings: AppSettings, scope: "Scope"
) -> HistoryImportService:
    """Stage 5C. Takes the memory service so derived knowledge goes through
    the one pipeline that deduplicates and conflict-resolves it."""
    return HistoryImportService(
        session=session,
        memory_service=MemoryService(
            session=session, provider=provider, settings=settings, scope=scope
        ),
        settings=settings,
        scope=scope,
    )


def get_reminder_service(
    session: DbSession, settings: AppSettings, scope: "Scope"
) -> ReminderService:
    """Stage 5F.1. Takes no provider: reminder scheduling makes no model call
    anywhere -- the schedule is parsed deterministically."""
    return ReminderService(session=session, settings=settings, scope=scope)


def get_task_service(session: DbSession, settings: AppSettings, owner_id: "OwnerId") -> TaskService:
    """Stage 6A. Takes no provider: nothing in the task layer calls a model.

    The owner is the authenticated owner (`get_owner_id`, Stage 6M.4), never a
    request parameter -- an endpoint that could name an owner would be an
    endpoint that could read another owner's tasks the moment Mai has two.
    """
    return TaskService(session=session, settings=settings, owner_id=owner_id)


def get_monitor_request_service(
    session: DbSession, settings: AppSettings, owner_id: "OwnerId"
) -> MonitorRequestService:
    """Stage 6N. The monitor service for the cancel route, bound to the
    authenticated owner. Takes no provider: nothing in it calls a model."""
    return MonitorRequestService(session, settings=settings, owner_id=owner_id)


def get_notification_delivery_service(session: DbSession, owner_id: "OwnerId") -> NotificationDeliveryService:
    """Stage 6L. The 6I delivery service, from the 6K composition root.

    Takes no adapter: the adapters are whatever the one process registry
    holds. The owner is the authenticated owner (`get_owner_id`, Stage 6M.4),
    passed explicitly, never a request parameter. Built per request because it holds this
    request's session; the registry and adapters behind it are shared.
    """
    return notification_delivery_service(session, owner_id)


#: What a database read can raise. Drivers can let connection errors escape
#: unwrapped (`get_db_session` catches the same pair on commit).
_DATABASE_FAILURES = (SQLAlchemyError, OSError)


async def _active_principal(session: AsyncSession, member_id: uuid.UUID) -> Optional[Principal]:
    """Stage 7A. The member's active membership, read once and not cached.

    Runs before any route code, so nothing may be pending on the request
    session; if something is, that is a programming error and nothing is
    rolled back over it. The read's transaction is ended at once, so the seam
    holds no connection (PostgreSQL) and no write lock (SQLite's BEGIN
    IMMEDIATE) for the rest of the request. A database error fails closed
    as 503 -- never as "inactive" and never as access.
    """
    # Anything already begun on this session -- flushed or not -- would be
    # rolled back below. Nothing may run before authentication.
    if session.new or session.dirty or session.deleted or session.in_transaction():
        raise SeamInvariantError()
    try:
        principal = await load_active_principal(session, member_id)
    except _DATABASE_FAILURES as exc:
        # The class name only: SQLAlchemy's text carries bound parameters.
        logger.warning("Membership check failed", extra={"category": type(exc).__name__})
        await _end_quietly(session)
        raise MembershipUnavailable()
    except Exception:
        # Not a database failure: end the read, keep the original error.
        await _end_quietly(session)
        raise
    try:
        await session.rollback()
    except _DATABASE_FAILURES as exc:
        logger.warning("Membership check failed", extra={"category": type(exc).__name__})
        raise MembershipUnavailable()
    return principal


async def _end_quietly(session: AsyncSession) -> None:
    """End the seam's read on a path that is already failing, without
    replacing the error that is on its way out."""
    try:
        await session.rollback()
    except _DATABASE_FAILURES:
        pass


async def get_owner_id(
    settings: AppSettings,
    session: DbSession,
    authorization: Optional[str] = Header(default=None),
) -> uuid.UUID:
    """The one place a request's owner is resolved (6M.2), and since 6M.4 the
    one place a request is authenticated.

    `Authorization: Bearer <token>` is checked by `app.core.auth`; the owner
    is whoever the token authenticates -- the configured owner -- and never a
    request value. Fails closed: no configured digest is 503, no or malformed
    header is 401 `authentication_required`, a wrong token is 401
    `invalid_credentials`. Since Stage 7A the owner is a member, and the
    member must be active in an active organisation, checked in the database
    on every request: otherwise 403 `membership_inactive`, or 503
    `membership_unavailable` if the check itself fails. Every protected
    router depends on this (see `app.main`), and the owner-bound services
    below receive its answer.
    """
    if not auth.is_configured(settings):
        raise AuthenticationNotConfigured()
    scheme, _, token = (authorization or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        raise AuthenticationRequired()
    owner_id = auth.authenticate(token, settings)
    if owner_id is None:
        raise InvalidCredentials()
    if await _active_principal(session, owner_id) is None:
        raise MembershipInactive()
    return owner_id


OwnerId = Annotated[uuid.UUID, Depends(get_owner_id)]


async def get_principal(owner_id: OwnerId, session: DbSession) -> Principal:
    """Stage 7A. The authenticated member, as a plain value.

    Depends on the seam, so it can never be reached unauthenticated, and
    reads the membership again itself rather than trusting a value handed
    over -- a replaced seam (a test override) still cannot produce a
    principal that is not active in the database.
    """
    principal = await _active_principal(session, owner_id)
    if principal is None:
        raise MembershipInactive()
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def get_knowledge_scope(principal: CurrentPrincipal) -> KnowledgeScope:
    """Stage 7B unit 4. The authenticated member's personal knowledge scope.

    Derived only from the principal, which `get_principal` re-reads from the
    database on every request -- never from a request value
    (`docs/stage7b_ownership_design.md`, section 1)."""
    return KnowledgeScope.personal(principal)


Scope = Annotated[KnowledgeScope, Depends(get_knowledge_scope)]


def require_admin(principal: CurrentPrincipal) -> Principal:
    """Stage 7A. The authenticated member, who must be an Admin: 403 otherwise."""
    if principal.role is not Role.ADMIN:
        raise AdminRequired()
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


def get_membership_service(session: DbSession, settings: AppSettings) -> MembershipService:
    """Stage 7A. The identity service for the organisation routes. It never
    commits: the request session does, when the route returns normally."""
    return MembershipService(session, settings=settings)


def get_member_revocation(session: DbSession, settings: AppSettings) -> MemberRevocation:
    """Stage 7A. The one construction of member revocation.

    Its cancel service is the second construction of `MonitorRequestService`
    here (the first is the cancel route's, bound to the seam's owner). This
    one is bound to the member being revoked, and `MemberRevocation` calls it
    only with the member `mark_revoked` has just accepted in the Admin's own
    organisation -- never with a request value.
    """

    def cancel_service_for(member_id: uuid.UUID) -> MonitorRequestService:
        return MonitorRequestService(session, settings=settings, owner_id=member_id)

    return MemberRevocation(session, cancel_service_for=cancel_service_for)


MembershipServiceDep = Annotated[MembershipService, Depends(get_membership_service)]
MemberRevocationDep = Annotated[MemberRevocation, Depends(get_member_revocation)]


def get_notification_service(session: DbSession, owner_id: OwnerId) -> NotificationService:
    """Stage 6M.2. The 6H read service, bound to the request's owner."""
    return NotificationService(session, owner_id)


def get_delivery_status_reader(session: DbSession, owner_id: OwnerId) -> DeliveryStatusReader:
    """Stage 6M.2. Read-only delivery state, bound to the request's owner."""
    return DeliveryStatusReader(session, owner_id)


def get_entity_service(
    session: DbSession, provider: Provider, settings: AppSettings, scope: Scope
) -> EntityService:
    return EntityService(session=session, provider=provider, settings=settings, scope=scope)


def get_relationship_service(
    session: DbSession, provider: Provider, settings: AppSettings, scope: Scope
) -> RelationshipService:
    return RelationshipService(
        session=session, provider=provider, settings=settings, scope=scope
    )


Conversations = Annotated[ConversationService, Depends(get_conversation_service)]
Chat = Annotated[ChatService, Depends(get_chat_service)]
Memories = Annotated[MemoryService, Depends(get_memory_service)]
Entities = Annotated[EntityService, Depends(get_entity_service)]
Relationships = Annotated[RelationshipService, Depends(get_relationship_service)]
Retrieval = Annotated[RetrievalService, Depends(get_retrieval_service)]
Reminders = Annotated[ReminderService, Depends(get_reminder_service)]
HistoryImport = Annotated[
    HistoryImportService, Depends(get_history_import_service)
]
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
Executions = Annotated[ExecutionService, Depends(get_execution_service)]

Tasks = Annotated[TaskService, Depends(get_task_service)]
MonitorRequests = Annotated[MonitorRequestService, Depends(get_monitor_request_service)]
NotificationDeliveries = Annotated[
    NotificationDeliveryService, Depends(get_notification_delivery_service)
]
TaskNotifications = Annotated[NotificationService, Depends(get_notification_service)]
DeliveryStatuses = Annotated[DeliveryStatusReader, Depends(get_delivery_status_reader)]
