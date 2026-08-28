"""Application configuration, loaded from environment variables."""

from functools import lru_cache
from typing import List

from typing_extensions import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Mai backend.

    Every value is sourced from the environment (or a local .env file).
    Secrets are never given a usable default.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    APP_NAME: str = "Mai"
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"  # "json" or "console"

    # --- Database ---
    # Must be an async driver URL, e.g. postgresql+asyncpg://user:pass@host:5432/mai
    DATABASE_URL: str = "postgresql+asyncpg://mai:mai@localhost:5432/mai"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # --- LLM ---
    # Selects which provider the factory builds. Must match a key in
    # app.llm.factory._REGISTRY.
    LLM_PROVIDER: str = "groq"

    # Shared across every provider.
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = 2
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 4096

    # --- Groq ---
    # Per-provider settings follow the convention <PROVIDER>_API_KEY /
    # _BASE_URL / _MODEL, which is what `active_*` below resolves. Adding a
    # provider means adding its three settings and one factory registry line.
    GROQ_API_KEY: str = ""
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_MODEL: str = "openai/gpt-oss-120b"

    # --- Active-provider resolution -----------------------------------------
    # Everything downstream reads these rather than a provider-specific field,
    # so switching LLM_PROVIDER is the only change required.

    @property
    def _provider(self) -> str:
        return self.LLM_PROVIDER.strip().lower()

    def _provider_setting(self, suffix: str) -> str:
        """Read <PROVIDER>_<SUFFIX>, e.g. GROQ_API_KEY."""
        # Hyphens are legal in a provider name but not in an env var.
        name = f"{self._provider.upper().replace('-', '_')}_{suffix}"
        try:
            return getattr(self, name)
        except AttributeError:
            raise ValueError(
                f"LLM_PROVIDER={self.LLM_PROVIDER!r} has no {name} setting. "
                f"Add it to Settings, or set a provider that exists."
            ) from None

    @property
    def active_api_key(self) -> str:
        return self._provider_setting("API_KEY")

    @property
    def active_base_url(self) -> str:
        return self._provider_setting("BASE_URL")

    @property
    def active_model(self) -> str:
        return self._provider_setting("MODEL")

    # --- Chat behaviour ---
    # System prompt prepended to every request. Stage 1 keeps this deliberately plain;
    # personality lives in a later stage.
    MAI_SYSTEM_PROMPT: str = (
        "You are Mai, a helpful personal AI assistant. "
        "Answer clearly and concisely."
    )
    # Upper bound on how many stored messages are replayed to the model.
    MAX_CONTEXT_MESSAGES: int = 40

    # --- CORS ---
    # NoDecode is required: without it pydantic-settings tries to JSON-decode
    # any complex-typed value coming from the environment, so a plain
    # `CORS_ORIGINS=http://localhost:3000` would raise before the validator
    # below ever runs.
    CORS_ORIGINS: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value):
        """Accept a comma-separated string or a JSON array."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @property
    def is_llm_configured(self) -> bool:
        """True when the selected provider has the credentials it needs."""
        return bool(self.active_api_key)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()
