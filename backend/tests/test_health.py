"""Health endpoint."""

from datetime import datetime, timezone

from httpx import AsyncClient

from app.llm.base import ProviderOutcome


async def test_health_reports_ok_when_dependencies_are_configured(
    client: AsyncClient, fake_provider
) -> None:
    # "ok" needs an *observed* provider success, not just a configured key.
    fake_provider.last_outcome = ProviderOutcome(
        ok=True, error_code=None, at=datetime.now(timezone.utc)
    )
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "Mai"
    assert body["database"]["healthy"] is True
    assert body["llm"]["healthy"] is True


async def test_health_reports_degraded_without_an_api_key(
    client: AsyncClient, settings
) -> None:
    # Clear the key for the *active* provider (groq in the test fixture).
    settings.GROQ_API_KEY = ""

    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["llm"]["healthy"] is False
    assert "No API key configured" in body["llm"]["detail"]


async def test_health_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers.get("X-Request-ID")


# --- Observed provider reachability (Stage 6Q follow-up) ----------------------
#
# `/health` never calls the model. It reports configuration, then what the
# provider's most recent *real* request did -- so a provider that is timing
# out is no longer reported healthy just because its key is present.


async def test_reachability_is_unverified_before_any_request(
    client: AsyncClient, fake_provider
) -> None:
    body = (await client.get("/health")).json()
    # A configured key is not proof of a working provider.
    assert body["status"] == "degraded"
    assert body["database"]["healthy"] is True
    assert body["llm"]["healthy"] is False
    assert body["llm"]["reachability"] == "unverified"
    assert "not yet verified" in body["llm"]["detail"]


async def test_a_failed_last_request_makes_health_degraded(
    client: AsyncClient, fake_provider
) -> None:
    fake_provider.last_outcome = ProviderOutcome(
        ok=False, error_code="llm_timeout",
        at=datetime(2026, 10, 13, 11, 28, 11, tzinfo=timezone.utc),
    )
    body = (await client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["llm"]["healthy"] is False
    assert body["llm"]["reachability"] == "failing"
    assert body["llm"]["detail"] == (
        "The last fake request failed (llm_timeout) at 2026-10-13 11:28:11 UTC."
    )


async def test_a_successful_last_request_reports_reachable(
    client: AsyncClient, fake_provider
) -> None:
    fake_provider.last_outcome = ProviderOutcome(
        ok=True, error_code=None, at=datetime.now(timezone.utc),
    )
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["llm"]["reachability"] == "reachable"


async def test_a_missing_model_is_reported_unconfigured(
    client: AsyncClient, settings
) -> None:
    settings.GROQ_MODEL = ""
    body = (await client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["llm"]["healthy"] is False
    assert "No model configured" in body["llm"]["detail"]


async def test_health_never_calls_the_model(client: AsyncClient, fake_provider) -> None:
    for _ in range(3):
        await client.get("/health")
    for recorded in (fake_provider.calls, fake_provider.extraction_calls,
                     fake_provider.entity_calls, fake_provider.relationship_calls,
                     fake_provider.intent_calls, fake_provider.planning_calls):
        assert recorded == []


async def test_health_recovers_after_a_later_successful_request(
    client: AsyncClient, fake_provider
) -> None:
    """Health follows the provider's latest real request: failing, then
    reachable after a success, then failing again after a later failure."""
    fake_provider.last_outcome = ProviderOutcome(
        ok=False, error_code="llm_error", at=datetime.now(timezone.utc)
    )
    failing = (await client.get("/health")).json()
    assert failing["status"] == "degraded"
    assert failing["llm"]["reachability"] == "failing"

    fake_provider._record_success()  # the real recorder, as a request would
    recovered = (await client.get("/health")).json()
    assert recovered["status"] == "ok"
    assert recovered["llm"]["healthy"] is True
    assert recovered["llm"]["reachability"] == "reachable"

    fake_provider._record_failure(RuntimeError("anything"))
    again = (await client.get("/health")).json()
    assert again["llm"]["reachability"] == "failing"
    assert again["llm"]["detail"].count("llm_error") == 1  # code only


async def test_missing_configuration_is_degraded_even_after_a_success(
    client: AsyncClient, fake_provider, settings
) -> None:
    fake_provider.last_outcome = ProviderOutcome(
        ok=True, error_code=None, at=datetime.now(timezone.utc)
    )
    settings.GROQ_API_KEY = ""
    body = (await client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["llm"]["healthy"] is False
    assert "No API key configured" in body["llm"]["detail"]


async def test_health_exposes_only_codes_and_times_never_secrets(
    client: AsyncClient, fake_provider, settings
) -> None:
    sentinel_key = "sk-sentinel-health-0123456789"
    settings.GROQ_API_KEY = sentinel_key
    for outcome in (
        None,
        ProviderOutcome(ok=True, error_code=None, at=datetime.now(timezone.utc)),
        ProviderOutcome(ok=False, error_code="llm_auth_error",
                        at=datetime.now(timezone.utc)),
    ):
        fake_provider.last_outcome = outcome
        text = (await client.get("/health")).text
        assert sentinel_key not in text
        assert set((await client.get("/health")).json()["llm"]) == {
            "healthy", "detail", "reachability"
        }
