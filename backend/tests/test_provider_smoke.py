"""`python -m app.llm.smoke`: one harmless request, sanitised output.

A configured key is not proof a provider works; this command is the check
that is (docs/provider_integration_checklist.md). These tests pin that it
uses the configured provider, reports an error *code* rather than text,
never prints the prompt, the reply or a key, and fails closed.
"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import LLMTimeoutError
from app.llm import smoke
from app.llm.providers.gemini import GeminiProvider

pytestmark = pytest.mark.anyio

FAKE_KEY = "AIzaSyFAKE-smoke-placeholder-0123456789"


def gemini(handler) -> GeminiProvider:
    return GeminiProvider(
        api_key=FAKE_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash", max_retries=0,
        transport=httpx.MockTransport(handler),
    )


def settings() -> Settings:
    return Settings(_env_file=None, LLM_PROVIDER="gemini", GEMINI_API_KEY=FAKE_KEY)


async def test_a_working_provider_reports_ok_without_its_reply() -> None:
    def handler(request):
        assert smoke.SMOKE_PROMPT in request.content.decode()
        return httpx.Response(200, json={
            "model": "gemini-3.8-flash",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    result = await smoke.run(settings(), provider=gemini(handler))
    assert result["ok"] is True
    assert result["provider"] == "gemini" and result["model"] == "gemini-3.8-flash"
    assert result["content_chars"] == 2
    assert result["error_code"] is None
    # No field carries the reply's text, the prompt or the key.
    assert "ok" not in [v for v in result.values() if isinstance(v, str)]
    text = json.dumps(result)
    assert FAKE_KEY not in text and smoke.SMOKE_PROMPT not in text


async def test_a_failing_provider_reports_its_error_code() -> None:
    def handler(request):
        raise httpx.ReadTimeout("slow")

    result = await smoke.run(settings(), provider=gemini(handler))
    assert result["ok"] is False
    assert result["error_code"] == LLMTimeoutError.code
    assert FAKE_KEY not in json.dumps(result)


async def test_an_unknown_provider_fails_closed_without_a_request() -> None:
    result = await smoke.run(Settings(_env_file=None, LLM_PROVIDER="openai"))
    assert result["ok"] is False
    assert result["error_code"]


def test_the_smoke_module_touches_no_database_or_tools() -> None:
    import pathlib
    import re

    source = pathlib.Path("app/llm/smoke.py").read_text()
    assert not re.search(
        r"^\s*(from|import)\s+app\.(database|tools|execution|tasks|services)\b",
        source, re.M,
    )
