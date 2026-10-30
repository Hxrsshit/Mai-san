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
    `status` to distinguish "ok" from "degraded". `llm.healthy` is true only
    after the provider has been observed to succeed; `llm.reachability` says
    which of "unverified", "reachable" or "failing" applies.
    """
    database_ok = await check_session_connection(session)

    # Never a model call: probing the provider on every health request would
    # spend tokens and add latency to a hot endpoint. Instead, configuration
    # is checked here and reachability is *observed* -- the outcome of the
    # provider's most recent real request -- so a provider that is timing out
    # is no longer reported healthy just because its key is present.
    llm_health = _llm_health(settings, provider)
    llm_ok = llm_health.healthy

    return HealthResponse(
        status="ok" if (database_ok and llm_ok) else "degraded",
        app=settings.APP_NAME,
        environment=settings.APP_ENV,
        database=ComponentHealth(
            healthy=database_ok,
            detail=None if database_ok else "Database is unreachable.",
        ),
        llm=llm_health,
    )


def _llm_health(settings, provider) -> ComponentHealth:
    """Configuration, then the last observed outcome. Sanitised: an error
    code and a time, never a message, a prompt or a credential."""
    if not settings.active_api_key:
        return ComponentHealth(
            healthy=False,
            detail=f"No API key configured for the {provider.name} provider.",
            reachability="unverified",
        )
    if not settings.is_llm_configured:
        return ComponentHealth(
            healthy=False,
            detail=f"No model configured for the {provider.name} provider.",
            reachability="unverified",
        )
    outcome = getattr(provider, "last_outcome", None)
    if outcome is None:
        # Configured is not working: healthy only after an observed success.
        return ComponentHealth(
            healthy=False,
            detail=(
                f"{provider.name} is configured; no request since startup, "
                "so reachability is not yet verified."
            ),
            reachability="unverified",
        )
    at = outcome.at.strftime("%Y-%m-%d %H:%M:%S UTC")
    if outcome.ok:
        return ComponentHealth(
            healthy=True,
            detail=f"The last {provider.name} request succeeded at {at}.",
            reachability="reachable",
        )
    return ComponentHealth(
        healthy=False,
        detail=(
            f"The last {provider.name} request failed ({outcome.error_code}) "
            f"at {at}."
        ),
        reachability="failing",
    )
