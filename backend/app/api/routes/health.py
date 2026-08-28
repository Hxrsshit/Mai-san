"""Health endpoint."""

from fastapi import APIRouter

from app.api.deps import AppSettings, DbSession, Provider
from app.database.session import check_session_connection
from app.schemas.common import ComponentHealth, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
async def health(
    settings: AppSettings, provider: Provider, session: DbSession
) -> HealthResponse:
    """Report database and LLM reachability.

    Always returns 200 so the endpoint stays usable as a liveness probe; read
    `status` to distinguish "ok" from "degraded".
    """
    database_ok = await check_session_connection(session)

    # The LLM check is configuration-only: probing the model on every health
    # call would spend tokens and add latency to a hot endpoint.
    llm_configured = settings.is_llm_configured
    llm_health = ComponentHealth(
        healthy=llm_configured,
        detail=None
        if llm_configured
        else f"No API key configured for the {provider.name} provider.",
    )

    return HealthResponse(
        status="ok" if (database_ok and llm_configured) else "degraded",
        app=settings.APP_NAME,
        environment=settings.APP_ENV,
        database=ComponentHealth(
            healthy=database_ok,
            detail=None if database_ok else "Database is unreachable.",
        ),
        llm=llm_health,
    )
