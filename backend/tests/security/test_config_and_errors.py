"""Stage 3D: configuration safety and information leakage through errors.

Two questions: does a broken configuration fail safely, and does a failure ever
tell the client something it should not know?
"""

import json

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.core.errors import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)

#: Strings that must never appear in a response body.
LEAK_MARKERS = (
    "traceback",
    "sqlalchemy",
    "asyncpg",
    "sqlite3",
    "aiosqlite",
    "select ",
    "insert into",
    "gsk_",
    "api_key",
    "bearer ",
    "/users/",
    "site-packages",
    "app/services/",
    "__init__",
)


def assert_no_leak(response) -> None:
    body = response.text.lower()
    for marker in LEAK_MARKERS:
        assert marker not in body, f"response leaked {marker!r}: {response.text[:300]}"


# --- Configuration ----------------------------------------------------------


def test_a_missing_api_key_is_reported_as_unconfigured_not_crashed() -> None:
    """Absence must be detectable, not fatal at import time."""
    settings = Settings(_env_file=None, GROQ_API_KEY="")

    assert settings.is_llm_configured is False
    assert settings.active_api_key == ""


def test_an_unknown_provider_fails_with_a_useful_message_and_no_secret() -> None:
    """The security half is unchanged: the message carries no credential."""
    from app.llm.factory import UnknownProviderError

    settings = Settings(
        _env_file=None, LLM_PROVIDER="nonexistent", GROQ_API_KEY="SENTINEL-KEY-VALUE"
    )

    with pytest.raises(UnknownProviderError) as caught:
        _ = settings.active_api_key

    message = str(caught.value)
    assert "nonexistent" in message
    # Stage 4F-F: the closed provider set is named, rather than a setting for
    # a provider that does not exist.
    assert "groq" in message and "anthropic_api" in message
    # And no configured credential travels in it.
    assert "gsk_" not in message
    assert settings.GROQ_API_KEY not in message


def test_an_unavailable_provider_says_why_and_carries_no_secret() -> None:
    """`claude_subscription` is refused with its reason, not a typo message."""
    from app.llm.factory import build_provider
    from app.llm.gateway import ProviderUnavailable

    settings = Settings(
        _env_file=None,
        LLM_PROVIDER="claude_subscription",
        GROQ_API_KEY="SENTINEL-KEY-VALUE",
        ANTHROPIC_API_KEY="SENTINEL-ANTHROPIC-VALUE",
    )

    with pytest.raises(ProviderUnavailable) as caught:
        build_provider(settings)

    message = str(caught.value)
    assert "claude_subscription" in message
    assert "claude.ai login" in message
    assert "SENTINEL" not in message


def test_no_setting_default_carries_a_credential() -> None:
    """A default value is committed code; it must never be a working secret."""
    settings = Settings(_env_file=None, GROQ_API_KEY="x")

    for name, value in settings.model_dump().items():
        if not isinstance(value, str) or not value:
            continue
        if name.upper().endswith(("API_KEY", "SECRET", "PASSWORD", "TOKEN")):
            # Only the one value supplied above may be non-empty.
            assert value == "x", f"{name} ships a non-empty credential default"


def test_debug_flags_default_to_off() -> None:
    settings = Settings(_env_file=None, GROQ_API_KEY="x")

    assert settings.DB_ECHO is False
    assert settings.LOG_LEVEL.upper() in {"INFO", "WARNING", "ERROR"}


async def test_no_endpoint_returns_configuration(client: AsyncClient, settings) -> None:
    """A sweep of every read endpoint for configuration leakage."""
    paths = [
        "/health",
        "/api/conversations",
        "/api/memories",
        "/api/entities",
        "/api/relationships",
    ]
    for path in paths:
        response = await client.get(path)
        body = response.text
        assert settings.GROQ_API_KEY not in body
        assert settings.DATABASE_URL not in body
        assert settings.MAI_SYSTEM_PROMPT not in body
        assert "api_key" not in body.lower()


async def test_health_reports_status_without_configuration(
    client: AsyncClient, settings
) -> None:
    response = await client.get("/health")

    body = response.json()
    serialised = json.dumps(body)
    assert settings.GROQ_API_KEY not in serialised
    assert settings.DATABASE_URL not in serialised
    assert "password" not in serialised.lower()


# --- Provider failures ------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        LLMTimeoutError("timed out after 60s"),
        LLMRateLimitError("rate limit reached"),
        LLMAuthError("invalid api key"),
        LLMResponseError("malformed response"),
        LLMError("generic provider failure"),
    ],
)
async def test_a_provider_failure_never_leaks_internals(
    client: AsyncClient, conversation_id, fake_provider, settings, error
) -> None:
    fake_provider.raise_error = error

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello Mai."},
    )

    assert response.status_code >= 400
    assert_no_leak(response)
    assert settings.GROQ_API_KEY not in response.text
    # A stable error envelope, not an exception dump.
    body = response.json()["error"]
    assert set(body) == {"code", "message", "request_id"}


def test_an_auth_rejection_carries_no_upstream_text() -> None:
    """The primary control: 401/403 discards the provider's message entirely.

    An upstream API that echoes part of a rejected key back ("Incorrect API key
    provided: sk-...XYZ") cannot reach the client, because the auth branch
    never includes `detail`.
    """
    import httpx

    from app.llm.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key="gsk_LIVEKEYVALUE0123456789ABCDEF",
        base_url="https://example.invalid/v1",
        model="test-model",
    )

    for status in (401, 403, 429, 500):
        upstream = httpx.Response(
            status,
            json={"error": {"message": "Incorrect API key provided: gsk_LIVEK...CDEF"}},
            request=httpx.Request("POST", "https://example.invalid/v1/x"),
        )
        error = provider._error_for_status(upstream)
        assert "gsk_" not in error.message, f"HTTP {status} echoed a key fragment"
        assert "Incorrect API key provided" not in error.message


async def test_a_credential_shape_in_an_error_is_redacted_before_the_client(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Defence in depth for the generic 4xx branch, which does pass `detail`.

    A key that looks like a key is masked. A credential that looks like an
    ordinary word cannot be distinguished from prose by any pattern -- that
    limit is documented rather than papered over, and the control above is what
    actually covers the auth path.
    """
    fake_provider.raise_error = LLMError(
        "groq rejected the request (HTTP 400): bad key gsk_ABCDEF0123456789XYZ"
    )

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello."},
    )

    assert "gsk_ABCDEF0123456789XYZ" not in response.text
    assert "gsk_***" in response.text


async def test_a_provider_failure_leaves_no_partial_turn(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """A failed generation must not leave an orphaned user message."""
    fake_provider.raise_error = LLMTimeoutError("timed out")

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "This turn will fail."},
    )
    assert response.status_code >= 400

    messages = (await client.get(f"/api/conversations/{conversation_id}/messages")).json()
    assert messages == [], "a failed turn left a message behind"


# --- Unexpected internal errors ---------------------------------------------


async def test_an_unexpected_exception_returns_a_generic_envelope(
    client: AsyncClient, conversation_id, monkeypatch
) -> None:
    """An unclassified exception must become a bare 500, not a dump.

    `ASGITransport` re-raises application exceptions by default, which would
    bypass the very handler under test, so this uses a transport configured
    the way a real server behaves.
    """
    from httpx import ASGITransport

    async def boom(*args, **kwargs):
        raise ZeroDivisionError("secret internal detail /Users/someone/app/x.py")

    monkeypatch.setattr(
        "app.services.conversation_service.ConversationService.get_messages", boom
    )

    app = client._transport.app  # type: ignore[attr-defined]
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as strict:
        response = await strict.get(
            f"/api/conversations/{conversation_id}/messages"
        )

    assert response.status_code == 500
    assert_no_leak(response)
    assert "secret internal detail" not in response.text
    assert response.json()["error"]["code"] == "internal_error"


async def test_a_database_failure_returns_503_without_details(
    client: AsyncClient, conversation_id, monkeypatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    async def boom(*args, **kwargs):
        raise OSError("connection refused to postgresql://mai:pw@db:5432/mai")

    monkeypatch.setattr(AsyncSession, "flush", boom)

    response = await client.post("/api/conversations", json={})

    assert response.status_code in (500, 503)
    assert "mai:pw" not in response.text
    assert_no_leak(response)


# --- Request id handling ----------------------------------------------------


async def test_a_client_supplied_request_id_is_bounded(client: AsyncClient) -> None:
    """The header is echoed into responses and log lines; it must be bounded.

    An unbounded value would be reflected into every log line for the request,
    amplifying a single request into arbitrary log volume.
    """
    response = await client.get(
        "/health", headers={"X-Request-ID": "A" * 10_000}
    )

    assert response.status_code in (200, 503)
    echoed = response.headers.get("X-Request-ID", "")
    assert len(echoed) <= 200, f"echoed {len(echoed)} characters back"


async def test_a_hostile_request_id_cannot_forge_log_structure(
    client: AsyncClient,
) -> None:
    """CRLF in a reflected value is the classic log- and header-forging vector."""
    response = await client.get(
        "/health",
        headers={"X-Request-ID": "abc"},
    )
    assert response.status_code in (200, 503)

    # Values that cannot be sent as a header at all are rejected by the
    # transport; what matters is that whatever *is* accepted is sanitised.
    from app.api.middleware import _sanitise_request_id

    for hostile in (
        "abc\r\nX-Injected: yes",
        "abc\nlevel=CRITICAL forged=1",
        "a" * 5000,
        "id with spaces and \x00 null",
    ):
        cleaned = _sanitise_request_id(hostile)
        assert "\r" not in cleaned and "\n" not in cleaned
        assert "\x00" not in cleaned
        assert len(cleaned) <= 64


# --- CORS -------------------------------------------------------------------
# Finding C-04: `CORS_ORIGINS=*` with `allow_credentials=True` lets any website
# read every endpoint from a visitor's browser. Starlette resolves the wildcard
# by echoing the caller's origin, so it is not the inert default it appears.


def test_the_default_origin_list_is_explicit() -> None:
    settings_default = Settings(_env_file=None, GROQ_API_KEY="x")
    assert settings_default.CORS_ORIGINS == ["http://localhost:3000"]
    assert "*" not in settings_default.CORS_ORIGINS


def test_a_wildcard_origin_disables_credentials() -> None:
    from fastapi.middleware.cors import CORSMiddleware

    from app.core.config import get_settings
    from app.main import create_app

    wildcard = Settings(_env_file=None, GROQ_API_KEY="x", CORS_ORIGINS="*")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: wildcard

    # create_app reads settings at construction, so build one bound to them.
    import app.main as main_module

    original = main_module.get_settings
    main_module.get_settings = lambda: wildcard
    try:
        wildcard_app = create_app()
    finally:
        main_module.get_settings = original

    cors = [
        middleware
        for middleware in wildcard_app.user_middleware
        if middleware.cls is CORSMiddleware
    ]
    assert cors, "CORS middleware is not installed"
    options = cors[0].kwargs
    assert options["allow_credentials"] is False, (
        "a wildcard origin was combined with credentials"
    )


def test_an_explicit_origin_list_keeps_credentials() -> None:
    from fastapi.middleware.cors import CORSMiddleware

    import app.main as main_module
    from app.main import create_app

    explicit = Settings(
        _env_file=None, GROQ_API_KEY="x", CORS_ORIGINS="http://localhost:3000"
    )
    original = main_module.get_settings
    main_module.get_settings = lambda: explicit
    try:
        app = create_app()
    finally:
        main_module.get_settings = original

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors[0].kwargs["allow_credentials"] is True
