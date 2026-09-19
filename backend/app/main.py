"""Mai backend application entrypoint (Stage 1)."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.routes import (
    conversations_router,
    entities_router,
    entity_relationships_router,
    health_router,
    memories_router,
    relationships_router,
    retrieval_router,
)
from app.api.routes import (
    context_preview_router,
    context_router,
    execution_router,
    history_router,
    integrations_router,
    intent_router,
    knowledge_router,
    orchestration_router,
    planning_router,
    prompt_router,
    tools_router,
)
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import (
    check_database_connection,
    dispose_engine,
    init_engine,
)
from app.llm.factory import dispose_provider, init_provider

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start up shared resources, and tear them down on shutdown."""
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)

    logger.info(
        "Starting Mai backend",
        extra={"app": settings.APP_NAME, "environment": settings.APP_ENV},
    )

    init_engine(settings)
    if await check_database_connection():
        logger.info("Database connection established")
    else:
        # Deliberately non-fatal: the container should come up and report
        # "degraded" on /health rather than crash-loop while Postgres boots.
        logger.error("Database is unreachable at startup; /health will report degraded")

    provider = init_provider(settings)
    if not settings.is_llm_configured:
        logger.warning(
            "No LLM API key configured; chat requests will fail until one is set",
            extra={"provider": provider.name},
        )

    logger.info("Mai backend ready")
    yield

    logger.info("Shutting down Mai backend")
    await dispose_provider()
    await dispose_engine()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=f"{settings.APP_NAME} API",
        description="Stage 1 foundation: chat, conversation persistence, LLM integration.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)

    # A wildcard origin combined with credentials lets *any* website call this
    # API from a visitor's browser and read the response -- every conversation,
    # memory and entity, plus the DELETE routes. Starlette resolves "*" by
    # echoing the request's own origin when credentials are allowed, so the
    # wildcard is not the harmless default it looks like. Credentials are
    # dropped rather than the origin list being silently rewritten, so an
    # operator who really wants "*" gets a working read-only-from-anywhere API
    # rather than a surprise.
    origins = list(settings.CORS_ORIGINS)
    allow_credentials = "*" not in origins
    if not allow_credentials:
        logger.warning(
            "CORS_ORIGINS contains '*'; credentialed cross-origin requests are "
            "disabled. Set an explicit origin list to re-enable them.",
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(conversations_router)
    # Prompt inspection is registered unconditionally: it describes the chat
    # request path itself, which exists whether or not the memory subsystem
    # does. With memory off it is how you confirm no knowledge reaches the
    # model.
    app.include_router(prompt_router)
    # Stage 4A understanding. Registered unconditionally alongside the
    # chat path it describes: it reads no memory and needs no knowledge
    # subsystem, and with memory disabled it is how you confirm that.
    app.include_router(intent_router)
    # Stage 4B planning. Like intent, it reads no memory and needs no
    # knowledge subsystem, so it is registered unconditionally.
    app.include_router(planning_router)
    # Stage 4C authorization. Read-only: it lists declarations and answers
    # whether an action would be permitted. Nothing here runs anything.
    app.include_router(tools_router)
    # Stage 4F-G. Connecting an external account is an operator action and is
    # registered unconditionally: a deployment with no Google client
    # configured still needs somewhere to report that, and `/connect` refuses
    # with a specific code rather than a 404.
    app.include_router(integrations_router)
    # Stage 4D orchestration. Propose, authorize, return -- never execute.
    app.include_router(orchestration_router)

    # Stage 4E controlled execution. Registered only when the operator has
    # switched execution on, so a deployment that cannot run anything does not
    # expose endpoints that talk about running things. The service refuses
    # independently -- this is the outer of two doors, not the only one.
    if settings.EXECUTION_ENABLED:
        app.include_router(execution_router)

    # The memory subsystem can be disabled entirely; when it is, the
    # inspection routes are not registered at all.
    if settings.MEMORY_ENABLED:
        app.include_router(memories_router)
        # Entities are derived from memories, so they share the master switch.
        app.include_router(entities_router)
        app.include_router(relationships_router)
        app.include_router(entity_relationships_router)
        # Retrieval reads memories/entities/relationships, so it shares their
        # master switch.
        app.include_router(retrieval_router)
        app.include_router(context_preview_router)
        # Stage 3A assembly reads memories/entities/relationships, so it
        # shares their master switch.
        app.include_router(context_router)
        # Stage 3C lifecycle inspection reads memories and relationships,
        # so it shares their master switch.
        app.include_router(knowledge_router)
        # Stage 5C history import derives memories, so it shares the memory
        # master switch too: a deployment with memory off has nowhere to put
        # what an import would produce, and the raw archive alone would be
        # storage with no purpose. The service refuses independently on
        # HISTORY_IMPORT_ENABLED -- this is the outer of two doors.
        if settings.HISTORY_IMPORT_ENABLED:
            app.include_router(history_router)

    return app


app = create_app()
