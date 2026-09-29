"""Gemini cannot leave Mai's network boundary.

Every provider request goes through `SecureHttpClient` with a single-host
`NetworkPolicy`. These tests prove Gemini does too: structurally, by what the
module can and cannot import, and behaviourally, by driving real requests at
hosts and redirects the policy must refuse.
"""

import ast
from pathlib import Path

import httpx
import pytest

from app.core.errors import LLMError
from app.llm.base import LLMMessage
from app.llm.providers.gemini import GeminiProvider
from app.llm.transport import provider_policy

pytestmark = pytest.mark.anyio

BACKEND = Path(__file__).resolve().parents[2]
GEMINI = BACKEND / "app" / "llm" / "providers" / "gemini.py"
FAKE_KEY = "AIzaSyFAKE-gemini-security-0123456789ab"


def parsed():
    return ast.parse(GEMINI.read_text(encoding="utf-8"))


# ============================================================================
# A. Structural: nothing in the module can open a connection of its own
# ============================================================================


def test_the_gemini_module_imports_no_http_library_or_sdk() -> None:
    forbidden = {
        "httpx", "requests", "urllib", "urllib3", "aiohttp", "socket", "http",
        "google", "googleapiclient", "vertexai", "grpc", "subprocess", "os",
    }
    for node in ast.walk(parsed()):
        roots = set()
        if isinstance(node, ast.Import):
            roots = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots = {node.module.split(".")[0]}
        assert not (roots & forbidden), roots & forbidden


def test_the_gemini_module_imports_only_the_shared_base() -> None:
    imports = set()
    for node in ast.walk(parsed()):
        if isinstance(node, ast.ImportFrom):
            imports.add((node.module, tuple(a.name for a in node.names)))
        elif isinstance(node, ast.Import):
            imports.add(tuple(a.name for a in node.names))
    assert imports == {
        ("app.llm.providers.openai_compatible", ("OpenAICompatibleProvider",)),
    }, imports


def test_gemini_overrides_no_network_method() -> None:
    """Every request path is the base class's, which uses the policed client."""
    classes = [n for n in ast.walk(parsed()) if isinstance(n, ast.ClassDef)]
    assert [c.name for c in classes] == ["GeminiProvider"]
    methods = [
        n.name for n in ast.walk(classes[0])
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert methods == [], methods


def test_no_google_sdk_is_a_dependency() -> None:
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        path = BACKEND / name
        if path.exists():
            text = path.read_text(encoding="utf-8").lower()
            for sdk in ("google-genai", "google-generativeai", "google-cloud-aiplatform",
                        "vertexai"):
                assert sdk not in text, (name, sdk)


def test_the_provider_policy_admits_exactly_the_gemini_host() -> None:
    policy = provider_policy(
        "https://generativelanguage.googleapis.com/v1beta/openai", 30.0
    )
    assert policy.allowed_hosts == frozenset({"generativelanguage.googleapis.com"})
    assert policy.allowed_methods == frozenset({'POST'})
    assert policy.follow_redirects is False


def test_the_gateway_declares_exactly_one_gemini_host() -> None:
    from app.llm.gateway import PERMITTED_PROVIDER_HOSTS, PROVIDERS, ProviderMode

    assert PROVIDERS[ProviderMode.GEMINI].host == "generativelanguage.googleapis.com"
    google_hosts = {h for h in PERMITTED_PROVIDER_HOSTS if "google" in h}
    assert google_hosts == {"generativelanguage.googleapis.com"}


def test_compose_does_not_let_the_environment_redirect_gemini() -> None:
    """The base URL is not passed into the container, so it stays the
    settings default and an environment variable cannot repoint it."""
    compose = (BACKEND.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY: ${GEMINI_API_KEY:-}" in compose
    assert "GEMINI_BASE_URL" not in compose


def test_the_example_env_file_carries_no_key() -> None:
    example = (BACKEND.parent / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if line.startswith("GEMINI_API_KEY"):
            assert line.strip() == "GEMINI_API_KEY=", "a value was committed"


# ============================================================================
# B. Behavioural: the policy refuses what it must, through the real client
# ============================================================================


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "x"},
                     "finish_reason": "stop"}],
        "model": "gemini-3.8-flash",
    })


@pytest.mark.parametrize("base_url", [
    "http://generativelanguage.googleapis.com/v1beta/openai",   # not HTTPS
    "https://evil.example.com/v1beta/openai",                   # another host
    "https://generativelanguage.googleapis.com.evil.com/v1",    # suffix trick
    "https://127.0.0.1/v1beta/openai",                          # loopback
    "https://169.254.169.254/latest",                           # metadata service
    "https://10.0.0.5/v1",                                      # private range
])
async def test_a_hostile_base_url_is_refused_before_any_request(base_url) -> None:
    sent = []

    def handler(request):
        sent.append(request)
        return ok(request)

    provider = GeminiProvider(
        api_key=FAKE_KEY, base_url=base_url, model="gemini-3.8-flash",
        max_retries=0, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert sent == [], f"a request reached {base_url}"


async def test_a_redirect_is_not_followed() -> None:
    """A completions endpoint that redirects would carry the payload, and the
    key, somewhere the operator did not choose."""
    hops = []

    def handler(request):
        hops.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.example.com/steal"})

    provider = GeminiProvider(
        api_key=FAKE_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert all("evil.example.com" not in hop for hop in hops), hops


async def test_the_key_goes_only_to_the_declared_host() -> None:
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization", "")))
        return ok(request)

    provider = GeminiProvider(
        api_key=FAKE_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    await provider.generate_response([LLMMessage(role="user", content="Hi")])
    assert seen == [("generativelanguage.googleapis.com", f"Bearer {FAKE_KEY}")]


def test_no_test_file_carries_a_real_gemini_key() -> None:
    """Google keys start `AIza` and are 39 characters. The placeholders here
    carry FAKE, so a real one would stand out."""
    import re

    pattern = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
    for path in (BACKEND / "tests").rglob("*.py"):
        for match in pattern.findall(path.read_text(encoding="utf-8")):
            assert "FAKE" in match, (path.name, match[:8] + "...")
