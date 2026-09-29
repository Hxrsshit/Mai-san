"""Gemini as a second LLM provider, alongside Groq.

Gemini is a subclass of `OpenAICompatibleProvider`, like Groq, so every test
here injects an `httpx.MockTransport` *under* `SecureHttpClient` -- the same
point the Groq tests use. `NetworkPolicy` therefore runs exactly as it does
in production: the host allow-list, refused redirects, the size cap and the
timeouts are all live in these tests, not bypassed.

The key used throughout is a placeholder. No real key appears in this file.
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
    LLMTimeoutError,
)
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.factory import build_provider
from app.llm.providers.gemini import DEFAULT_BASE_URL, DEFAULT_MODEL, GeminiProvider
from app.llm.providers.groq import GroqProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider

pytestmark = pytest.mark.anyio

#: Synthetic, clearly not a real key. Long enough that a substring match on
#: it in any output is meaningful.
FAKE_KEY = "AIzaSyFAKE-gemini-placeholder-0123456789ab"


def completion_body(content: str = "Hello from Gemini.") -> dict:
    return {
        "id": "chatcmpl-gemini-test",
        "object": "chat.completion",
        "model": "gemini-3.8-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
    }


def make_provider(handler, **overrides) -> GeminiProvider:
    kwargs = dict(
        api_key=FAKE_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    kwargs.update(overrides)
    return GeminiProvider(**kwargs)


def settings(**values) -> Settings:
    return Settings(**{"LLM_PROVIDER": "groq", **values})


# ============================================================================
# A. Configuration
# ============================================================================


def test_gemini_implements_the_existing_abstraction() -> None:
    """A subclass of the same base Groq uses -- not a second abstraction."""
    assert issubclass(GeminiProvider, OpenAICompatibleProvider)
    assert issubclass(GeminiProvider, LLMProvider)
    assert GeminiProvider.name == "gemini"
    # It adds nothing but a name: every behaviour is the shared base's.
    # `_abc_impl` is Python's ABC bookkeeping, not something the class defines.
    own = {k for k in vars(GeminiProvider) if not k.startswith("_")}
    assert own == {"name"}, own


def test_the_defaults_are_googles_documented_endpoint() -> None:
    """Literal-pinned, so a change of endpoint has to be deliberate."""
    assert DEFAULT_BASE_URL == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert DEFAULT_MODEL == "gemini-3.8-flash"
    config = Settings()
    assert config.GEMINI_BASE_URL == DEFAULT_BASE_URL
    assert config.GEMINI_MODEL == DEFAULT_MODEL
    assert config.GEMINI_API_KEY == ""


def test_gemini_settings_resolve_through_the_active_properties() -> None:
    config = settings(LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY)
    assert config.active_api_key == FAKE_KEY
    assert config.active_base_url == DEFAULT_BASE_URL
    assert config.active_model == DEFAULT_MODEL


# ============================================================================
# B. Provider selection
# ============================================================================


def test_groq_remains_the_default_provider() -> None:
    config = Settings()
    assert config.LLM_PROVIDER == "groq"
    assert isinstance(build_provider(settings(GROQ_API_KEY="gsk-placeholder")), GroqProvider)


def test_gemini_is_selected_only_by_configuration() -> None:
    provider = build_provider(settings(LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY))
    assert isinstance(provider, GeminiProvider)
    assert provider.name == "gemini"
    assert provider.model == DEFAULT_MODEL


@pytest.mark.parametrize("spelling", ["gemini", "GEMINI", "  Gemini  "])
def test_the_name_is_matched_case_and_space_insensitively(spelling) -> None:
    provider = build_provider(settings(LLM_PROVIDER=spelling, GEMINI_API_KEY=FAKE_KEY))
    assert isinstance(provider, GeminiProvider)


def test_selecting_gemini_does_not_read_the_groq_key() -> None:
    """Each provider reads only its own credential."""
    config = settings(
        LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY, GROQ_API_KEY="gsk-other",
    )
    assert config.active_api_key == FAKE_KEY


def test_there_is_no_automatic_routing_to_gemini() -> None:
    """Groq with no key does not fall over to Gemini, even when Gemini has one."""
    provider = build_provider(settings(GROQ_API_KEY="", GEMINI_API_KEY=FAKE_KEY))
    assert isinstance(provider, GroqProvider)


# ============================================================================
# C. Successful generation
# ============================================================================


async def test_the_request_matches_geminis_openai_compatible_format() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler)
    await provider.generate_response([LLMMessage(role="user", content="Hi")])

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gemini-3.8-flash"
    assert body["messages"] == [{"role": "user", "content": "Hi"}]


async def test_a_successful_response_is_normalised() -> None:
    provider = make_provider(lambda r: httpx.Response(200, json=completion_body("Namaste.")))
    response = await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert isinstance(response, LLMResponse)
    assert response.content == "Namaste."
    assert response.finish_reason == "stop"
    assert response.usage["total_tokens"] == 13


# ============================================================================
# D. Authentication and other failures
# ============================================================================


@pytest.mark.parametrize(("status", "expected"), [
    (401, LLMAuthError), (403, LLMAuthError), (429, LLMRateLimitError),
    (400, LLMError), (500, LLMError), (503, LLMError),
])
async def test_http_errors_map_to_provider_agnostic_errors(status, expected) -> None:
    provider = make_provider(
        lambda r: httpx.Response(status, json={"error": {"message": "API key not valid"}})
    )
    with pytest.raises(expected):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_a_missing_key_is_reported_before_any_request() -> None:
    called = []

    def handler(request):
        called.append(request)
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler, api_key="")
    with pytest.raises(LLMNotConfiguredError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert called == [], "a request left without a key"


async def test_a_timeout_maps_to_llm_timeout_error() -> None:
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    provider = make_provider(handler, timeout_seconds=1.0)
    with pytest.raises(LLMTimeoutError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_a_network_failure_is_an_llm_error_not_a_crash() -> None:
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    provider = make_provider(handler)
    with pytest.raises(LLMError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])


async def test_the_health_check_is_unhealthy_without_a_key() -> None:
    provider = make_provider(lambda r: httpx.Response(200, json=completion_body()), api_key="")
    health = await provider.health_check()
    assert health.healthy is False


# ============================================================================
# E. The key never escapes
# ============================================================================


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_an_echoed_key_is_scrubbed_from_error_text(status) -> None:
    """A provider that reflects the key back in an error must not leak it."""
    provider = make_provider(lambda r: httpx.Response(
        status, json={"error": {"message": f"bad key {FAKE_KEY} / Bearer {FAKE_KEY}"}}
    ))
    with pytest.raises(LLMError) as caught:
        await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in repr(caught.value)
    assert FAKE_KEY not in str(getattr(caught.value, "detail", "") or "")


async def test_the_key_is_not_logged(caplog) -> None:
    caplog.set_level("DEBUG")
    provider = make_provider(lambda r: httpx.Response(
        401, json={"error": {"message": f"invalid {FAKE_KEY}"}}
    ))
    with pytest.raises(LLMAuthError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    await provider.close()

    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()
        assert FAKE_KEY not in str(record.__dict__)


async def test_the_key_is_not_stored_on_the_client() -> None:
    """Inserted per request, never baked into the long-lived client."""
    provider = make_provider(lambda r: httpx.Response(200, json=completion_body()))
    await provider.generate_response([LLMMessage(role="user", content="Hi")])
    client = provider._get_client()
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in str(getattr(client, "__dict__", {}))
    await provider.close()


def test_runtime_facts_name_gemini_but_never_its_key() -> None:
    from app.prompt.formatter import render_runtime_facts
    from app.runtime.facts import build

    config = settings(LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY)
    facts = build(settings=config)
    assert facts.llm_provider == "gemini"
    assert facts.llm_model == DEFAULT_MODEL
    assert FAKE_KEY not in facts.model_dump_json()
    assert FAKE_KEY not in render_runtime_facts(facts)


async def test_the_health_endpoint_never_returns_the_key(client, settings) -> None:
    """The one API surface that describes the provider.

    The first version requested `/api/health`, which does not exist, so it
    asserted the key was absent from a 404 page and passed trivially. The
    endpoint is `/health`; asserting the status proves the test reached it.
    """
    settings.LLM_PROVIDER = "gemini"
    settings.GEMINI_API_KEY = FAKE_KEY

    response = await client.get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["status"] in {"ok", "degraded"}
    assert FAKE_KEY not in response.text
