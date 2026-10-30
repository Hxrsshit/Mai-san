"""The provider contract: one matrix every implemented LLM provider must pass.

Every case runs against each provider in `CONTRACT`, through the real class
and the real `SecureHttpClient` -- only the socket is an `httpx.MockTransport`
-- so the host policy, refused redirects, size cap and timeouts are live, and
a provider that drifts from the others fails here rather than in production.
(Stage 6Q found exactly that drift: Anthropic did not retry its documented
529 overload, logged nothing per attempt, and reported a missing key as a
rejected one.)

Adding a provider means adding one `ProviderSpec` below; see
`docs/provider_integration_checklist.md`. A provider not in `CONTRACT` is not
implemented -- `test_unimplemented_providers_fail_closed` pins that OpenAI is
not a selectable mode, so nothing can claim it works.

All keys here are synthetic placeholders.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Dict, List

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
from app.llm.base import LLMMessage, LLMProvider
from app.llm.factory import UnknownProviderError, build_provider
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.groq import GroqProvider

pytestmark = pytest.mark.anyio

FAKE_KEY = "sk-FAKE-contract-placeholder-0123456789abcdef"


def openai_body(content: str = "Hello.") -> dict:
    return {
        "model": "served-model",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def anthropic_body(content: str = "Hello.") -> dict:
    return {
        "model": "served-model",
        "content": [{"type": "text", "text": content}] if content else [],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


@dataclass(frozen=True)
class ProviderSpec:
    mode: str
    cls: type
    settings_prefix: str
    base_url: str
    path: str
    #: (header name, expected value) the credential must travel in.
    auth: Callable[[str], tuple]
    body: Callable[[str], dict]
    #: Statuses the provider documents as transient overload.
    overload_statuses: tuple


CONTRACT: List[ProviderSpec] = [
    ProviderSpec("groq", GroqProvider, "GROQ", "https://api.groq.com/openai/v1",
                 "/openai/v1/chat/completions",
                 lambda key: ("authorization", f"Bearer {key}"), openai_body, (503,)),
    ProviderSpec("gemini", GeminiProvider, "GEMINI",
                 "https://generativelanguage.googleapis.com/v1beta/openai",
                 "/v1beta/openai/chat/completions",
                 lambda key: ("authorization", f"Bearer {key}"), openai_body, (503,)),
    ProviderSpec("anthropic_api", AnthropicProvider, "ANTHROPIC",
                 "https://api.anthropic.com", "/v1/messages",
                 lambda key: ("x-api-key", key), anthropic_body, (503, 529)),
]
IDS = [spec.mode for spec in CONTRACT]
MESSAGES = [LLMMessage(role="system", content="Be brief."),
            LLMMessage(role="user", content="Say hello.")]


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    """Retries keep their count and order; only the waiting is skipped."""
    async def no_wait(_seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_wait)


class Recorder:
    """A mock socket that answers from a script and keeps every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(spec: ProviderSpec, recorder, **overrides) -> LLMProvider:
    kwargs = dict(api_key=FAKE_KEY, base_url=spec.base_url, model="contract-model",
                  max_retries=2, transport=httpx.MockTransport(recorder))
    kwargs.update(overrides)
    return spec.cls(**kwargs)


def ok(spec, content="Hello.") -> httpx.Response:
    return httpx.Response(200, json=spec.body(content))


# --- Configuration and selection ---------------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
def test_each_provider_is_selected_explicitly_by_configuration(spec) -> None:
    settings = Settings(LLM_PROVIDER=spec.mode, **{f"{spec.settings_prefix}_API_KEY": FAKE_KEY})
    provider = build_provider(settings)
    assert type(provider) is spec.cls
    assert provider.name == spec.mode
    assert provider.model == getattr(settings, f"{spec.settings_prefix}_MODEL")


@pytest.mark.parametrize("name", ["openai", "gpt", "", "groq,gemini", "auto"])
def test_unimplemented_providers_fail_closed(name) -> None:
    """Not a fallback, not a guess: an unknown mode is refused."""
    with pytest.raises(UnknownProviderError):
        build_provider(Settings(LLM_PROVIDER=name))


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_a_missing_key_is_not_configured_and_sends_nothing(spec) -> None:
    recorder = Recorder(ok(spec))
    with pytest.raises(LLMNotConfiguredError):
        await make(spec, recorder, api_key="").generate_response(MESSAGES)
    assert recorder.requests == []


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("model", ["", "   "])
async def test_a_missing_model_is_not_configured_and_sends_nothing(spec, model) -> None:
    recorder = Recorder(ok(spec))
    with pytest.raises(LLMNotConfiguredError):
        await make(spec, recorder, model=model).generate_response(MESSAGES)
    assert recorder.requests == []


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
def test_a_missing_model_is_reported_as_unconfigured_by_settings(spec) -> None:
    settings = Settings(LLM_PROVIDER=spec.mode, **{
        f"{spec.settings_prefix}_API_KEY": FAKE_KEY, f"{spec.settings_prefix}_MODEL": " ",
    })
    assert settings.is_llm_configured is False


# --- Request construction ----------------------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_the_request_goes_to_the_endpoint_with_the_key_in_its_header(spec) -> None:
    recorder = Recorder(ok(spec))
    await make(spec, recorder).generate_response(MESSAGES)
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == httpx.URL(spec.base_url).host
    assert request.url.path == spec.path
    name, value = spec.auth(FAKE_KEY)
    assert request.headers[name] == value
    # The key travels in exactly one place.
    assert FAKE_KEY not in str(request.url)
    assert FAKE_KEY not in request.content.decode()
    assert json.loads(request.content)["model"] == "contract-model"


# --- Response normalisation --------------------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_a_valid_reply_is_normalised(spec) -> None:
    provider = make(spec, Recorder(ok(spec, "Hi there.")))
    response = await provider.generate_response(MESSAGES)
    assert response.content == "Hi there."
    assert response.model == "served-model"
    assert response.usage["total_tokens"] == 5


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("reply", [
    "empty_text", "not_json", "json_array", "no_fields",
])
async def test_an_unusable_reply_is_an_invalid_response_not_a_crash(spec, reply) -> None:
    replies = {
        "empty_text": lambda: ok(spec, ""),
        "not_json": lambda: httpx.Response(200, text="<html>oops</html>"),
        "json_array": lambda: httpx.Response(200, json=[1, 2]),
        "no_fields": lambda: httpx.Response(200, json={}),
    }
    recorder = Recorder(replies[reply]())
    with pytest.raises(LLMResponseError):
        await make(spec, recorder).generate_response(MESSAGES)
    assert len(recorder.requests) == 1  # a malformed reply is not retried


# --- HTTP status classification and retries ----------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("status,error", [
    (400, LLMError), (401, LLMAuthError), (403, LLMAuthError), (404, LLMError),
])
async def test_caller_errors_are_classified_and_never_retried(spec, status, error) -> None:
    recorder = Recorder(httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(error) as caught:
        await make(spec, recorder).generate_response(MESSAGES)
    assert len(recorder.requests) == 1
    # Never mislabelled as a timeout; a bad request or model is not "auth".
    assert not isinstance(caught.value, LLMTimeoutError)
    if status in (400, 404):
        assert not isinstance(caught.value, (LLMAuthError, LLMRateLimitError))


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_rate_limiting_is_retried_a_bounded_number_of_times(spec) -> None:
    recorder = Recorder(httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(LLMRateLimitError):
        await make(spec, recorder, max_retries=2).generate_response(MESSAGES)
    assert len(recorder.requests) == 3


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_errors_are_retried_then_reported_unavailable(spec, status) -> None:
    recorder = Recorder(httpx.Response(status, json={"error": {"message": "down"}}))
    with pytest.raises(LLMError) as caught:
        await make(spec, recorder, max_retries=2).generate_response(MESSAGES)
    assert len(recorder.requests) == 3
    assert type(caught.value) is LLMError
    assert f"HTTP {status}" in caught.value.message


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_the_documented_overload_status_is_retried_and_can_recover(spec) -> None:
    for status in spec.overload_statuses:
        recorder = Recorder(httpx.Response(status, json={}), ok(spec, "Recovered."))
        response = await make(spec, recorder).generate_response(MESSAGES)
        assert response.content == "Recovered.", status
        assert len(recorder.requests) == 2, status


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectTimeout])
async def test_timeouts_are_timeouts_and_bounded(spec, failure) -> None:
    recorder = Recorder(failure("slow"))
    with pytest.raises(LLMTimeoutError):
        await make(spec, recorder, max_retries=2).generate_response(MESSAGES)
    assert len(recorder.requests) == 3


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_a_connection_failure_is_not_reported_as_a_timeout(spec) -> None:
    recorder = Recorder(httpx.ConnectError("refused"))
    with pytest.raises(LLMError) as caught:
        await make(spec, recorder, max_retries=1).generate_response(MESSAGES)
    assert type(caught.value) is LLMError
    assert len(recorder.requests) == 2


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_retries_never_exceed_the_configured_bound(spec) -> None:
    for retries in (0, 1, 3):
        recorder = Recorder(httpx.Response(503, json={}))
        with pytest.raises(LLMError):
            await make(spec, recorder, max_retries=retries).generate_response(MESSAGES)
        assert len(recorder.requests) == retries + 1


# --- Network boundary ----------------------------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_a_redirect_is_never_followed(spec) -> None:
    recorder = Recorder(httpx.Response(
        307, headers={"Location": "https://attacker.invalid/collect"}))
    with pytest.raises(LLMError):
        await make(spec, recorder).generate_response(MESSAGES)
    assert all(r.url.host == httpx.URL(spec.base_url).host for r in recorder.requests)


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("base_url", [
    "http://api.example.com/v1",       # not HTTPS
    "https://127.0.0.1/v1",            # loopback
    "https://10.0.0.5/v1",             # private range
])
async def test_a_forbidden_endpoint_is_refused_before_any_request(spec, base_url) -> None:
    recorder = Recorder(ok(spec))
    with pytest.raises(LLMError) as caught:
        await make(spec, recorder, base_url=base_url).generate_response(MESSAGES)
    assert recorder.requests == []
    assert "not permitted" in caught.value.message


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_an_oversized_reply_is_refused_without_retry(spec) -> None:
    recorder = Recorder(httpx.Response(200, content=b"x" * 2_100_000))
    with pytest.raises(LLMError):
        await make(spec, recorder).generate_response(MESSAGES)
    assert len(recorder.requests) == 1


# --- Credentials stay out of errors and logs ------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
async def test_an_echoed_key_never_reaches_an_error_or_a_log(spec, status, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    echo = {"error": {"message": f"bad header Bearer {FAKE_KEY} / {FAKE_KEY}"}}
    with pytest.raises(LLMError) as caught:
        await make(spec, Recorder(httpx.Response(status, json=echo))).generate_response(MESSAGES)
    assert FAKE_KEY not in caught.value.message
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_prompts_and_replies_are_not_logged(spec, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    secret_prompt = "my-private-diary-entry-7731"
    provider = make(spec, Recorder(ok(spec, "private-reply-9946")))
    await provider.generate_response([LLMMessage(role="user", content=secret_prompt)])
    assert secret_prompt not in caplog.text
    assert "private-reply-9946" not in caplog.text


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_every_failed_attempt_is_logged_in_the_shared_shape(spec, caplog) -> None:
    caplog.set_level(logging.WARNING)
    with pytest.raises(LLMError):
        await make(spec, Recorder(httpx.Response(503, json={})), max_retries=1
                   ).generate_response(MESSAGES)
    failures = [r for r in caplog.records if r.getMessage() == "LLM request failed"]
    assert [r.attempt for r in failures] == [1, 2]
    assert [r.will_retry for r in failures] == [True, False]
    assert all(r.provider == spec.mode for r in failures)


# --- Observed outcome (what /health reports) ------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_the_last_outcome_is_recorded_as_a_code_only(spec) -> None:
    provider = make(spec, Recorder(httpx.ReadTimeout("slow")), max_retries=0)
    assert provider.last_outcome is None
    with pytest.raises(LLMTimeoutError):
        await provider.generate_response(MESSAGES)
    assert provider.last_outcome.ok is False
    assert provider.last_outcome.error_code == "llm_timeout"

    provider = make(spec, Recorder(ok(spec)))
    await provider.generate_response(MESSAGES)
    assert provider.last_outcome.ok is True
    assert provider.last_outcome.error_code is None


# --- Concurrency ----------------------------------------------------------------


@pytest.mark.parametrize("spec", CONTRACT, ids=IDS)
async def test_one_request_failing_does_not_poison_a_concurrent_one(spec) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"fail-me" in request.content:
            return httpx.Response(400, json={"error": {"message": "bad"}})
        return ok(spec, "fine")

    provider = make(spec, handler)
    good, bad = await asyncio.gather(
        provider.generate_response([LLMMessage(role="user", content="hello")]),
        provider.generate_response([LLMMessage(role="user", content="fail-me")]),
        return_exceptions=True,
    )
    assert good.content == "fine"
    assert isinstance(bad, LLMError)
    # The provider is still usable, and its configuration did not change.
    assert (await provider.generate_response(MESSAGES)).content == "fine"
    assert provider.name == spec.mode and provider.model == "contract-model"


def test_providers_cannot_authorise_or_execute() -> None:
    """A provider produces text. It has no route to authorization, tools or
    execution: nothing in `app/llm` imports them."""
    import pathlib
    import re

    banned = re.compile(
        r"^\s*(from|import)\s+app\.(tools|execution|tasks|authorization|background)\b",
        re.M,
    )
    offenders = [
        str(path) for path in pathlib.Path("app/llm").rglob("*.py")
        if banned.search(path.read_text())
    ]
    assert offenders == []
