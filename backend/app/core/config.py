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
    # Stage 4D added the capability statement. Mai can now *recognise* an
    # action and have it authorized, and has no way to perform one -- so
    # without this the model would cheerfully report having sent the email.
    #
    # It belongs here rather than in the per-turn prompt: it is a standing
    # fact about the application, not state about this turn. Orchestration
    # results stay out of the prompt entirely, exactly as intent and plans do.
    #: Deliberately says nothing about which tools exist.
    #:
    #: It used to. Through Stage 4D it read "You have no tools: you cannot
    #: search the web, send email, read or write files, run code" -- accurate
    #: then, and **false from Stage 4E onwards**, where Mai can read and write
    #: files once execution is switched on. A static string cannot track a
    #: registry, so it was guaranteed to drift into a lie the moment the
    #: registry changed; it had already done so.
    #:
    #: Capability claims now come from one place: the runtime capability
    #: section, generated from the tool registry per request. This prompt
    #: carries behaviour and tone, and names no tool, provider or vendor.
    MAI_SYSTEM_PROMPT: str = (
        "You are Mai, a helpful personal AI assistant. "
        "Answer clearly and concisely.\n\n"
        "Be precise about what you can and cannot do. Your available "
        "capabilities are listed authoritatively in the runtime capability "
        "section below; treat that list as complete. Never say or imply that "
        "you have performed an action unless the application reports that it "
        "was performed. If the user asks for something you cannot do, say so "
        "plainly and offer to help think it through instead."
    )
    # Upper bound on how many stored messages are replayed to the model.
    MAX_CONTEXT_MESSAGES: int = 40

    # --- Memory (Stage 2A) ---
    # Master switch for the memory subsystem (storage + inspection API).
    MEMORY_ENABLED: bool = True
    # Automatic extraction after each completed turn. Can be disabled
    # independently to stop writing new memories while keeping existing ones
    # readable.
    MEMORY_EXTRACTION_ENABLED: bool = True
    # Candidates scoring below either threshold are discarded.
    MEMORY_MIN_IMPORTANCE: int = 5
    MEMORY_MIN_CONFIDENCE: float = 0.7
    # Lexical similarity at or above this counts as a duplicate. Raising it
    # stores more near-duplicates; lowering it risks merging distinct facts.
    MEMORY_DEDUP_THRESHOLD: float = 0.82
    # How many recent same-type memories a candidate is compared against.
    MEMORY_DEDUP_CANDIDATES: int = 50
    # Extraction is a classification task, so temperature stays low.
    MEMORY_EXTRACTION_TEMPERATURE: float = 0.1
    MEMORY_EXTRACTION_MAX_TOKENS: int = 1024

    # --- Entities (Stage 2B) ---
    # Entity extraction runs after a memory is stored. Disabling it leaves
    # conversations and memories untouched.
    ENTITY_EXTRACTION_ENABLED: bool = True
    # Candidates the model is unsure about are discarded.
    ENTITY_MIN_CONFIDENCE: float = 0.7
    # Upper bound per memory; a longer list means over-extraction.
    ENTITY_EXTRACTION_MAX_PER_MEMORY: int = 10
    ENTITY_EXTRACTION_TEMPERATURE: float = 0.1
    ENTITY_EXTRACTION_MAX_TOKENS: int = 1024

    # --- Relationships (Stage 2C) ---
    # Relationship extraction runs after entity extraction, and only when a
    # memory has at least two entities to relate.
    RELATIONSHIP_EXTRACTION_ENABLED: bool = True
    RELATIONSHIP_MIN_CONFIDENCE: float = 0.7
    RELATIONSHIP_EXTRACTION_MAX_PER_MEMORY: int = 10
    RELATIONSHIP_EXTRACTION_TEMPERATURE: float = 0.1
    RELATIONSHIP_EXTRACTION_MAX_TOKENS: int = 1024

    # --- Intent understanding (Stage 4A) ---
    # Classification runs on the request path and costs ONE bounded
    # structured model call per user turn, in addition to the single
    # response-generation call. Disabling it restores the exact pre-4A call
    # profile; nothing else changes, because intent never reaches the prompt.
    INTENT_CLASSIFICATION_ENABLED: bool = True
    # Zero temperature: the same message must classify the same way every
    # time, or downstream stages cannot rely on the label.
    INTENT_CLASSIFICATION_TEMPERATURE: float = 0.0
    INTENT_CLASSIFICATION_MAX_TOKENS: int = 512
    # How many recent turns are shown to the classifier so a short follow-up
    # ("do that one") can be understood. Kept small on purpose: Stage 2D
    # retrieval is NOT reused here, and this must not become a second context
    # system.
    INTENT_CONTEXT_MESSAGES: int = 4

    # --- Planning (Stage 4B) ---
    # Planning runs only for messages Stage 4A classified as planning, task,
    # research or action -- so ordinary conversation costs nothing. An
    # eligible message costs ONE additional structured model call. Disabling
    # this restores the exact pre-4B call profile.
    PLANNING_ENABLED: bool = True
    # Low but not zero. Decomposing work benefits from a little variation
    # where classification does not, but a plan should still be broadly
    # reproducible for the same goal.
    PLANNING_TEMPERATURE: float = 0.2
    # A bounded plan is a few thousand tokens of JSON; the schema limits cap
    # what can survive validation regardless.
    PLANNING_MAX_TOKENS: int = 2048

    # --- Action orchestration (Stage 4D) ---
    # Orchestration examines a turn for a proposed action, resolves it against
    # the Stage 4C registry and authorizes it. It adds ZERO model calls:
    # identification is a deterministic phrase lookup, and only turns Stage 4A
    # classified as ACTION are examined at all.
    #
    # Nothing it produces can execute. Disabling it removes the orchestration
    # field from chat responses and changes nothing else.
    ORCHESTRATION_ENABLED: bool = True

    # --- Controlled execution (Stage 4E) ---
    # Execution defaults OFF. Turning it on is a deliberate act, and until
    # then no executable action can run whatever else is configured.
    EXECUTION_ENABLED: bool = False
    # Every file operation is confined here. Relative to the process working
    # directory by default; no vendor or machine-specific path is hardcoded.
    MAI_WORKSPACE_ROOT: str = "./mai_workspace"
    # How long an approval remains valid. Fifteen minutes is long enough to
    # read a proposal and decide, short enough that an abandoned tab does not
    # leave a live grant lying around. Never unbounded.
    EXECUTION_APPROVAL_TTL_SECONDS: int = 900
    # Bounds on what the workspace tools may read, write and return.
    MAX_WORKSPACE_FILE_SIZE_BYTES: int = 1_000_000
    MAX_WORKSPACE_LIST_RESULTS: int = 500
    MAX_WORKSPACE_LIST_DEPTH: int = 6

    # --- Knowledge lifecycle (Stage 3C) ---
    # Conflict evaluation runs in the background pipeline after relationship
    # extraction. It adds no model calls anywhere. Disabling it stops new
    # lifecycle decisions; existing statuses and links are untouched.
    CONFLICT_DETECTION_ENABLED: bool = True
    # Whether a query using historical language ("what did I use before?") may
    # retrieve superseded knowledge. Off means history is stored and traceable
    # but never surfaces in chat.
    HISTORICAL_RETRIEVAL_ENABLED: bool = True

    # --- Context retrieval (Stage 2D) ---
    # Retrieval runs on the request path before the chat call. It adds no
    # model calls -- every step is a bounded database query.
    RETRIEVAL_ENABLED: bool = True
    # Budgets. Ranking happens first; these decide how much survives.
    RETRIEVAL_MAX_MEMORIES: int = 10
    RETRIEVAL_MAX_ENTITIES: int = 10
    RETRIEVAL_MAX_RELATIONSHIPS: int = 10
    RETRIEVAL_MAX_CONTEXT_CHARS: int = 8000
    # Upper bound on rows considered before ranking, so cost stays predictable
    # as the knowledge base grows.
    RETRIEVAL_CANDIDATE_POOL_SIZE: int = 50

    # Ranking weights. They sum to 1.0, and relevance (text + entity +
    # relationship = 0.70) deliberately outweighs metadata (importance +
    # confidence + recency = 0.30), so an important but irrelevant memory
    # cannot outrank a directly relevant one.
    RETRIEVAL_WEIGHT_TEXT: float = 0.30
    RETRIEVAL_WEIGHT_ENTITY: float = 0.25
    RETRIEVAL_WEIGHT_RELATIONSHIP: float = 0.15
    RETRIEVAL_WEIGHT_IMPORTANCE: float = 0.15
    RETRIEVAL_WEIGHT_CONFIDENCE: float = 0.10
    RETRIEVAL_WEIGHT_RECENCY: float = 0.05

    # --- Context assembly (Stage 3A) ---
    # Stage 3A combines the current message, recent conversation and Stage 2D's
    # ranked retrieval into one bounded package. It adds no model calls and
    # mutates nothing.
    CONTEXT_RECENT_MESSAGE_LIMIT: int = 12
    CONTEXT_MAX_MEMORY_ITEMS: int = 10
    CONTEXT_MAX_ENTITY_ITEMS: int = 10
    CONTEXT_MAX_RELATIONSHIP_ITEMS: int = 10
    # Final authority over every category limit. Characters for now; the
    # accounting is isolated so token budgeting can replace it.
    CONTEXT_MAX_TOTAL_CHARS: int = 10000

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
