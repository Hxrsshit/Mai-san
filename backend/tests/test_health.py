"""Health endpoint."""

from httpx import AsyncClient


async def test_health_reports_ok_when_dependencies_are_configured(
    client: AsyncClient,
) -> None:
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
