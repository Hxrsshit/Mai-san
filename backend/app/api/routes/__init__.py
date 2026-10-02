"""API routers."""

from app.api.routes.context import router as context_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.entities import router as entities_router
from app.api.routes.execution import router as execution_router
from app.api.routes.health import router as health_router
from app.api.routes.integrations import router as integrations_router
from app.api.routes.intent import router as intent_router
from app.api.routes.history import router as history_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.notification_delivery import router as notification_delivery_router
from app.api.routes.reminders import router as reminders_router
from app.api.routes.orchestration import router as orchestration_router
from app.api.routes.planning import router as planning_router
from app.api.routes.prompt import router as prompt_router
from app.api.routes.tools import router as tools_router
from app.api.routes.relationships import entity_router as entity_relationships_router
from app.api.routes.relationships import router as relationships_router
from app.api.routes.retrieval import conversation_router as context_preview_router
from app.api.routes.retrieval import router as retrieval_router
from app.api.routes.memories import router as memories_router
from app.api.routes.tasks import router as tasks_router

__all__ = [
    "conversations_router",
    "entities_router",
    "health_router",
    "memories_router",
    "relationships_router",
    "entity_relationships_router",
    "retrieval_router",
    "context_preview_router",
    "context_router",
    "prompt_router",
    "history_router",
    "knowledge_router",
    "reminders_router",
    "tasks_router",
    "notification_delivery_router",
    "intent_router",
    "planning_router",
    "tools_router",
    "orchestration_router",
    "execution_router",
    "integrations_router",
]
