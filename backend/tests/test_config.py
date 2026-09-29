"""Settings loaded from the environment.

These tests deliberately go through real environment variables rather than
constructing Settings() with Python values: pydantic-settings applies its own
decoding to env input, and a config that works in Python can still fail at
startup. That gap previously broke `docker compose up`.
"""

import pytest

from app.core.config import Settings


def env_settings(monkeypatch, **values) -> Settings:
    """Build Settings from environment variables only, ignoring any .env file."""
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


# --- CORS_ORIGINS -----------------------------------------------------------


def test_cors_origins_accepts_a_single_bare_url(monkeypatch) -> None:
    """The form docker-compose uses."""
    settings = env_settings(monkeypatch, CORS_ORIGINS="http://localhost:3000")
    assert settings.CORS_ORIGINS == ["http://localhost:3000"]


def test_cors_origins_accepts_a_comma_separated_list(monkeypatch) -> None:
    settings = env_settings(monkeypatch, CORS_ORIGINS="http://a.com, http://b.com")
    assert settings.CORS_ORIGINS == ["http://a.com", "http://b.com"]


def test_cors_origins_accepts_a_json_array(monkeypatch) -> None:
    settings = env_settings(monkeypatch, CORS_ORIGINS='["http://x.com"]')
    assert settings.CORS_ORIGINS == ["http://x.com"]


def test_cors_origins_has_a_working_default() -> None:
    assert Settings(_env_file=None).CORS_ORIGINS == ["http://localhost:3000"]


# --- Provider selection -----------------------------------------------------


def test_groq_is_the_default_provider() -> None:
    settings = Settings(_env_file=None)
    assert settings.LLM_PROVIDER == "groq"
    assert settings.active_base_url == "https://api.groq.com/openai/v1"
    assert settings.active_model == "openai/gpt-oss-120b"


def test_active_settings_resolve_from_the_provider_prefix(monkeypatch) -> None:
    settings = env_settings(
        monkeypatch,
        GROQ_API_KEY="groq-key",
        GROQ_MODEL="openai/gpt-oss-20b",
        GROQ_BASE_URL="https://example.test/v1",
    )
    assert settings.active_api_key == "groq-key"
    assert settings.active_model == "openai/gpt-oss-20b"
    assert settings.active_base_url == "https://example.test/v1"
    assert settings.is_llm_configured is True


def test_provider_name_is_normalised(monkeypatch) -> None:
    settings = env_settings(monkeypatch, LLM_PROVIDER="  GROQ  ", GROQ_API_KEY="k")
    assert settings.active_api_key == "k"


def test_missing_provider_settings_raise_an_actionable_error() -> None:
    """An unlisted provider is refused by name, with the valid options."""
    from app.llm.factory import UnknownProviderError

    # "gemini" was this test's example until Gemini became a provider.
    # "mistral" is still unlisted, which is what the test is about.
    settings = Settings(_env_file=None, LLM_PROVIDER="mistral")

    with pytest.raises(UnknownProviderError) as caught:
        _ = settings.active_api_key

    assert "mistral" in str(caught.value)
    assert "groq" in str(caught.value)
    assert "gemini" in str(caught.value), "the valid options omit Gemini"


# --- Secrets ----------------------------------------------------------------


def test_api_key_is_empty_by_default() -> None:
    """No key may ever be baked into the source."""
    settings = Settings(_env_file=None)
    assert settings.GROQ_API_KEY == ""
    assert settings.is_llm_configured is False


def test_api_key_is_read_from_the_environment(monkeypatch) -> None:
    settings = env_settings(monkeypatch, GROQ_API_KEY="gsk_from_env")
    assert settings.active_api_key == "gsk_from_env"


# --- No OpenRouter / GLM residue -------------------------------------------


@pytest.mark.parametrize(
    "removed",
    [
        "GLM_API_KEY",
        "GLM_BASE_URL",
        "GLM_MODEL",
        "OPENROUTER_SITE_URL",
        "OPENROUTER_APP_NAME",
        "is_paid_model",
        "provider_headers",
    ],
)
def test_openrouter_and_glm_settings_are_gone(removed) -> None:
    """Stage 1 uses Groq; the previous gateway's config must not linger."""
    assert not hasattr(Settings(_env_file=None), removed)


# --- Shared LLM tuning ------------------------------------------------------


def test_shared_llm_settings_have_sane_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.LLM_TIMEOUT_SECONDS == 60.0
    assert settings.LLM_MAX_RETRIES == 2
    assert settings.LLM_MAX_TOKENS == 4096


def test_shared_llm_settings_are_overridable(monkeypatch) -> None:
    settings = env_settings(
        monkeypatch, LLM_TIMEOUT_SECONDS="5", LLM_MAX_RETRIES="0", LLM_MAX_TOKENS="256"
    )
    assert settings.LLM_TIMEOUT_SECONDS == 5.0
    assert settings.LLM_MAX_RETRIES == 0
    assert settings.LLM_MAX_TOKENS == 256
