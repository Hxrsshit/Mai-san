"""LLM abstraction and the Groq provider.

The provider is exercised against a mock httpx transport, so these tests
verify the real wire format without any network access or API key.
"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import (
    LLMAuthError,
    LLMError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.factory import UnknownProviderError, build_provider
from app.llm.providers.groq import GroqProvider


def completion_body(content: str = "Hello.", finish_reason: str = "stop") -> dict:
    """A response shaped like a real Groq chat-completion payload."""
    return {
        "id": "chatcmpl-1",
        "request_id": "req-1",
        "created": 1756400000,
        "model": "openai/gpt-oss-120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def make_provider(handler, **overrides) -> GroqProvider:
    """A GroqProvider wired to a mock transport.

    Stage 4F-C changed the injection point from a whole `httpx.AsyncClient`
    to a bare transport. That is not merely a signature change: the provider
    now builds a `SecureHttpClient` around whatever transport it is given, so
    `NetworkPolicy` runs in these tests exactly as it runs in production.
    Injecting a finished client would have bypassed the boundary and made
    every test here prove less than it appears to.
    """
    kwargs = dict(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        model="openai/gpt-oss-120b",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    kwargs.update(overrides)
    return GroqProvider(**kwargs)


# --- Abstraction ------------------------------------------------------------


def test_groq_provider_satisfies_the_abstraction() -> None:
    provider = make_provider(lambda request: httpx.Response(200, json=completion_body()))
    assert isinstance(provider, LLMProvider)
    assert provider.name == "groq"
    assert provider.model == "openai/gpt-oss-120b"


def test_the_abstraction_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_factory_builds_the_configured_provider() -> None:
    provider = build_provider(
        Settings(_env_file=None, LLM_PROVIDER="groq", GROQ_API_KEY="k")
    )
    assert isinstance(provider, GroqProvider)
    assert provider.model == "openai/gpt-oss-120b"


def test_factory_rejects_an_unknown_provider() -> None:
    with pytest.raises(UnknownProviderError):
        build_provider(Settings(_env_file=None, LLM_PROVIDER="does-not-exist"))


# --- Request shape ----------------------------------------------------------


async def test_request_matches_the_documented_groq_format() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler)
    await provider.generate_response(
        [
            LLMMessage(role="system", content="You are Mai."),
            LLMMessage(role="user", content="Hi"),
        ]
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"

    body = captured["body"]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "You are Mai."},
        {"role": "user", "content": "Hi"},
    ]
    assert "temperature" in body and "max_tokens" in body


async def test_per_call_overrides_take_precedence() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler, temperature=0.7, max_tokens=4096)
    await provider.generate_response(
        [LLMMessage(role="user", content="Hi")], temperature=0.1, max_tokens=16
    )

    assert captured["temperature"] == 0.1
    assert captured["max_tokens"] == 16


# --- Response parsing -------------------------------------------------------


async def test_successful_response_is_normalised() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, json=completion_body("Hello there."))
    )

    response = await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert isinstance(response, LLMResponse)
    assert response.content == "Hello there."
    assert response.model == "openai/gpt-oss-120b"
    assert response.finish_reason == "stop"
    assert response.usage["total_tokens"] == 15


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {}}]},
        {"no_choices_key": True},
    ],
)
async def test_unusable_responses_raise_llm_response_error(body) -> None:
    provider = make_provider(lambda request: httpx.Response(200, json=body))

    with pytest.raises(LLMResponseError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_content_filtered_response_explains_itself() -> None:
    provider = make_provider(
        lambda request: httpx.Response(
            200, json=completion_body(content="", finish_reason="sensitive")
        )
    )

    with pytest.raises(LLMResponseError, match="content filter"):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_non_json_response_raises_llm_response_error() -> None:
    provider = make_provider(lambda request: httpx.Response(200, text="<html>502</html>"))

    with pytest.raises(LLMResponseError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


# --- Error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthError),
        (403, LLMAuthError),
        (429, LLMRateLimitError),
        (400, LLMError),
        (500, LLMError),
        (503, LLMError),
    ],
)
async def test_http_errors_map_to_provider_agnostic_errors(status, expected) -> None:
    provider = make_provider(
        lambda request: httpx.Response(status, json={"error": {"message": "nope"}})
    )

    with pytest.raises(expected):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_timeout_maps_to_llm_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider = make_provider(handler, timeout_seconds=1.0)

    with pytest.raises(LLMTimeoutError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_missing_api_key_is_reported_before_any_request() -> None:
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler, api_key="")

    with pytest.raises(LLMNotConfiguredError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert called == []


# --- Retries ----------------------------------------------------------------


async def test_transient_failures_are_retried_then_succeed(monkeypatch) -> None:
    monkeypatch.setattr(GroqProvider, "_backoff_seconds", staticmethod(lambda a: 0.0))
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json=completion_body("Recovered."))

    provider = make_provider(handler, max_retries=2)
    response = await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert len(attempts) == 3
    assert response.content == "Recovered."


async def test_retries_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(GroqProvider, "_backoff_seconds", staticmethod(lambda a: 0.0))
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, json={"message": "unavailable"})

    provider = make_provider(handler, max_retries=2)

    with pytest.raises(LLMError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert len(attempts) == 3  # 1 initial + 2 retries


async def test_client_errors_are_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(GroqProvider, "_backoff_seconds", staticmethod(lambda a: 0.0))
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = make_provider(handler, max_retries=3)

    with pytest.raises(LLMAuthError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert len(attempts) == 1  # retrying a bad key cannot help


# --- Health -----------------------------------------------------------------


async def test_health_check_is_unhealthy_without_a_key() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, json=completion_body()), api_key=""
    )

    health = await provider.health_check()

    assert health.healthy is False
    assert "No API key configured" in health.detail


async def test_health_check_is_healthy_when_the_api_answers() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, json=completion_body())
    )

    health = await provider.health_check()

    assert health.healthy is True
    assert health.provider == "groq"


async def test_health_check_reports_failure_without_raising() -> None:
    provider = make_provider(lambda request: httpx.Response(401, json={}))

    health = await provider.health_check()

    assert health.healthy is False


# --- Secret hygiene ---------------------------------------------------------


async def test_the_api_key_is_never_logged(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    provider = make_provider(handler, api_key="super-secret-key")

    with pytest.raises(LLMAuthError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert "super-secret-key" not in caplog.text


# --- Retry-After ------------------------------------------------------------
# Shared free tiers answer 429 with a Retry-After that is longer than our own
# backoff, so the header has to win.


async def test_retry_after_header_overrides_backoff(monkeypatch) -> None:
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.llm.providers.openai_compatible.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(GroqProvider, "_backoff_seconds", staticmethod(lambda a: 0.5))

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "5"}, json={"error": {"message": "busy"}}
            )
        return httpx.Response(200, json=completion_body("Recovered."))

    provider = make_provider(handler, max_retries=2)
    response = await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert response.content == "Recovered."
    assert slept == [5.0]  # the header, not the 0.5s backoff


async def test_backoff_is_used_when_no_retry_after_is_sent(monkeypatch) -> None:
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.llm.providers.openai_compatible.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(GroqProvider, "_backoff_seconds", staticmethod(lambda a: 0.5))

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler, max_retries=2)
    await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert slept == [0.5]


async def test_retry_after_is_capped(monkeypatch) -> None:
    """A hostile or broken Retry-After must not stall the request forever."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.llm.providers.openai_compatible.asyncio.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "99999"}, json={})

    provider = make_provider(handler, max_retries=1)

    with pytest.raises(LLMRateLimitError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert slept == [30.0]  # _MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize("value", ["not-a-number", "-5", "", "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_unparsable_retry_after_falls_back_to_backoff(monkeypatch, value) -> None:
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.llm.providers.openai_compatible.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(GroqProvider, "_backoff_seconds", staticmethod(lambda a: 0.25))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": value}, json={})

    provider = make_provider(handler, max_retries=1)

    with pytest.raises(LLMRateLimitError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert slept == [0.25]
