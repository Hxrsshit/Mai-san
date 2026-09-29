"""Stage 4F-F: the provider gateway.

Three modes, exactly one active, chosen by operator configuration and by
nothing else. No fallback in any direction.
"""

import pytest

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.factory import UnknownProviderError, build_provider
from app.llm.gateway import (
    CLAUDE_SUBSCRIPTION_UNAVAILABLE,
    PERMITTED_PROVIDER_HOSTS,
    PROVIDERS,
    AuthMode,
    ProviderMode,
    ProviderUnavailable,
    require_available,
    resolve_mode,
    spec_for,
)
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.groq import GroqProvider


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk-groq-sentinel",
        "ANTHROPIC_API_KEY": "sk-ant-sentinel",
    }
    base.update(overrides)
    return Settings(**base)


# --- The provider table -----------------------------------------------------


def test_exactly_four_modes_exist() -> None:
    """Gemini joined as a fourth mode -- a second provider alongside Groq."""
    assert {mode.value for mode in ProviderMode} == {
        "groq", "gemini", "anthropic_api", "claude_subscription",
    }
    assert set(PROVIDERS) == set(ProviderMode)


@pytest.mark.parametrize(
    ("mode", "prefix", "auth", "host", "available"),
    [
        (ProviderMode.GROQ, "GROQ", AuthMode.API_KEY, "api.groq.com", True),
        (ProviderMode.ANTHROPIC_API, "ANTHROPIC", AuthMode.API_KEY,
         "api.anthropic.com", True),
        (ProviderMode.CLAUDE_SUBSCRIPTION, "CLAUDE_SUBSCRIPTION",
         AuthMode.SUBSCRIPTION, "", False),
    ],
)
def test_each_spec_is_what_it_claims(mode, prefix, auth, host, available) -> None:
    """Pinned. A wrong host or auth mode here would be reported as truth."""
    spec = spec_for(mode)

    assert spec.settings_prefix == prefix
    assert spec.auth_mode is auth
    assert spec.host == host
    assert spec.available is available


def test_an_unavailable_provider_always_states_a_reason() -> None:
    for spec in PROVIDERS.values():
        if not spec.available:
            assert spec.unavailable_reason, spec.mode


def test_only_the_declared_hosts_are_permitted() -> None:
    """The network audit's answer, asserted rather than described."""
    # Gemini adds exactly one host, and nothing broader: no wildcard, no
    # other googleapis.com subdomain.
    assert PERMITTED_PROVIDER_HOSTS == frozenset(
        {"api.groq.com", "generativelanguage.googleapis.com", "api.anthropic.com"}
    )


def test_the_unavailable_provider_has_no_host() -> None:
    """Nothing may connect anywhere on its behalf."""
    assert spec_for(ProviderMode.CLAUDE_SUBSCRIPTION).host == ""


# --- Selection --------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("groq", GroqProvider), ("anthropic_api", AnthropicProvider)],
)
def test_the_configured_provider_is_the_one_built(mode, expected) -> None:
    provider = build_provider(_settings(LLM_PROVIDER=mode))

    assert isinstance(provider, expected)
    assert isinstance(provider, LLMProvider)


@pytest.mark.parametrize(
    "unknown",
    # `"groq "` is absent deliberately: surrounding whitespace is normalised,
    # which cannot change which provider is meant. Case is too. Nothing else is.
    # `"gemini"` left this list when Gemini became a provider. Near-misses of
    # it stay, because they must still be refused rather than guessed at.
    ["", "   ", "anthropic", "claude", "openai", "gpt-4", "google",
     "gemini-pro", "google_gemini", "GROQ_API", "claude-subscription",
     "../groq", "groq,anthropic_api", "groq,gemini"],
)
def test_an_unrecognised_provider_is_refused_and_never_defaulted(unknown) -> None:
    """No fallback to a default. An unknown name is a configuration error."""
    with pytest.raises(UnknownProviderError):
        build_provider(_settings(LLM_PROVIDER=unknown))


def test_provider_names_are_matched_case_insensitively() -> None:
    """Case is normalised; nothing else is."""
    assert resolve_mode("GROQ") is ProviderMode.GROQ
    assert resolve_mode("  Anthropic_API  ") is ProviderMode.ANTHROPIC_API


# --- claude_subscription is reserved, not implemented -----------------------


def test_selecting_the_subscription_provider_is_refused() -> None:
    with pytest.raises(ProviderUnavailable) as caught:
        build_provider(_settings(LLM_PROVIDER="claude_subscription"))

    assert "claude_subscription" in str(caught.value)


def test_the_refusal_states_both_independent_reasons() -> None:
    """Either would be sufficient; both are recorded.

    The permission reason could change if Anthropic approved a partner. The
    network reason would still be waiting, so the text names both.
    """
    reason = CLAUDE_SUBSCRIPTION_UNAVAILABLE.lower()

    assert "claude.ai login" in reason
    assert "network policy" in reason


def test_the_subscription_provider_has_no_builder() -> None:
    """Not a builder that raises: no entry at all.

    A mode whose builder raised would be one refactor away from a mode that
    works. There is nothing to call.
    """
    from app.llm.factory import _REGISTRY

    assert ProviderMode.CLAUDE_SUBSCRIPTION not in _REGISTRY
    assert set(_REGISTRY) == {
        ProviderMode.GROQ, ProviderMode.GEMINI, ProviderMode.ANTHROPIC_API,
    }


def test_require_available_permits_the_two_real_providers() -> None:
    for mode in (ProviderMode.GROQ, ProviderMode.ANTHROPIC_API):
        assert require_available(mode).available is True


def test_no_agent_sdk_is_imported_anywhere() -> None:
    """The SDK is not a dependency, and nothing reaches for it.

    Stage 4F-F determined the subscription path is not permitted for a
    third-party product. That determination is worth nothing if a later edit
    quietly adds the import.
    """
    import ast
    import pathlib

    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in app.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                root = module.split(".")[0]
                assert root not in {
                    "claude_agent_sdk", "claude_code_sdk", "anthropic",
                }, f"{path.name} imports {module}"


def test_no_subscription_credential_is_read_from_the_environment() -> None:
    """`CLAUDE_CODE_OAUTH_TOKEN` and friends are never consulted.

    Forwarding a subscription token as though it were an API key is
    explicitly out of bounds, so nothing reads one.
    """
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[1]
    for path in (backend / "app").rglob("*.py"):
        source = path.read_text()
        for forbidden in (
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "sessionKey", "claude.ai/api",
        ):
            assert forbidden not in source, f"{path.name} mentions {forbidden}"


def test_settings_declare_no_subscription_credential() -> None:
    """There is no field for a token, so there is nowhere to put one."""
    fields = set(Settings.model_fields)

    for forbidden in (
        "CLAUDE_SUBSCRIPTION_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_SESSION_KEY", "CLAUDE_SUBSCRIPTION_TOKEN",
    ):
        assert forbidden not in fields, forbidden


# --- No fallback ------------------------------------------------------------


async def test_a_failing_provider_does_not_try_another() -> None:
    """The whole point of no-fallback: one configured provider, one attempt.

    A silent failover would send the conversation to a company the operator
    did not choose, and bill an account they did not mean to use.
    """
    import httpx

    from app.core.errors import LLMError
    from app.llm.base import LLMMessage

    dialled = []

    class Failing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            dialled.append(str(request.url))
            return httpx.Response(500, json={"error": "down"})

    provider = AnthropicProvider(
        api_key="sk-ant-sentinel",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-5",
        max_retries=0,
        transport=Failing(),
    )

    with pytest.raises(LLMError):
        await provider.generate_response([LLMMessage(role="user", content="hi")])

    # Only the configured host, and only its own retries.
    assert dialled
    assert all("api.anthropic.com" in url for url in dialled)
    assert not any("groq" in url for url in dialled)


def test_the_factory_builds_exactly_one_provider_per_call() -> None:
    """Structural, by AST rather than by text.

    A substring scan for "fallback" reads the module's own docstring, which
    explains at length that there is none -- the same lesson Stage 4E's
    dispatcher scan and Stage 4E.1's vendor scan both learned.

    What actually matters: `build_provider` contains no `except` handler at
    all, so there is no branch in which a failure leads to a second attempt,
    and it calls exactly one builder.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "llm" / "factory.py"
    ).read_text()

    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "build_provider"
    )

    handlers = [n for n in ast.walk(function) if isinstance(n, ast.ExceptHandler)]
    assert handlers == [], "build_provider must not recover from a failure"

    # One `builder(settings)` call site, and no second one behind a branch.
    builder_calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "builder"
    ]
    assert len(builder_calls) == 1


# --- Billing separation -----------------------------------------------------


def test_selecting_the_subscription_never_uses_the_anthropic_key() -> None:
    """Both credentials present; explicit configuration still decides.

    An implicit precedence here would move a user's usage onto API billing
    without them asking.
    """
    settings = _settings(LLM_PROVIDER="claude_subscription")

    assert settings.ANTHROPIC_API_KEY == "sk-ant-sentinel"
    with pytest.raises(ProviderUnavailable):
        build_provider(settings)


def test_selecting_anthropic_uses_the_anthropic_key_not_groqs() -> None:
    settings = _settings(LLM_PROVIDER="anthropic_api")

    assert settings.active_api_key == "sk-ant-sentinel"
    assert settings.active_model == settings.ANTHROPIC_MODEL


def test_selecting_groq_uses_the_groq_key_not_anthropics() -> None:
    settings = _settings(LLM_PROVIDER="groq")

    assert settings.active_api_key == "gsk-groq-sentinel"
    assert settings.active_model == settings.GROQ_MODEL
