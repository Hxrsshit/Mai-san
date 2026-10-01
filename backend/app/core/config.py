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

    # --- Gemini ---
    # Google's OpenAI-compatible endpoint, reached through the same
    # `SecureHttpClient` and single-host policy as Groq. Selected only by
    # `LLM_PROVIDER=gemini`; Groq remains the default.
    GEMINI_API_KEY: str = ""
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    GEMINI_MODEL: str = "gemini-3.8-flash"

    # --- Anthropic API (Stage 4F-F) ---
    # The Messages API, with an API key and API billing. Distinct from a
    # Claude Pro/Max subscription, which Mai does not and may not use --
    # see `app.llm.gateway.CLAUDE_SUBSCRIPTION_UNAVAILABLE`.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com"
    ANTHROPIC_MODEL: str = "claude-sonnet-5"

    # --- Active-provider resolution -----------------------------------------
    # Everything downstream reads these rather than a provider-specific field,
    # so switching LLM_PROVIDER is the only change required.

    @property
    def _provider(self) -> str:
        return self.LLM_PROVIDER.strip().lower()

    def _provider_setting(self, suffix: str) -> str:
        """Read <PREFIX>_<SUFFIX>, e.g. GROQ_API_KEY.

        The prefix comes from the provider table rather than from the mode
        name. Deriving it produced `ANTHROPIC_API_API_KEY` for the
        `anthropic_api` mode -- not a name anyone would write in a `.env`
        file, and a convention that surprises is a convention that gets
        worked around.
        """
        from app.llm.gateway import PROVIDERS, resolve_mode

        prefix = PROVIDERS[resolve_mode(self._provider)].settings_prefix
        name = f"{prefix}_{suffix}"
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

    #: How long a plan's authorization stays valid, in seconds.
    #:
    #: Stage 6C recorded `tasks.authorized_at` and nothing expired it. A
    #: permission with no expiry is a standing grant by omission, which is
    #: the thing 6D was told not to build -- so the window is explicit and
    #: matches the execution approval TTL, for the same reason: a decision a
    #: person made an hour ago is not a decision about now.
    TASK_AUTHORIZATION_TTL_SECONDS: int = 900
    # Bounds on what the workspace tools may read, write and return.
    MAX_WORKSPACE_FILE_SIZE_BYTES: int = 1_000_000
    MAX_WORKSPACE_LIST_RESULTS: int = 500
    MAX_WORKSPACE_LIST_DEPTH: int = 6

    # --- Web research (Stage 4F-B) ---
    # No key ships and none is invented. Absent, the integration reports
    # NOT_CONFIGURED and the capability reports itself unavailable -- which
    # is the honest state of a deployment with no search provider.
    #: Which search API `WebSearchIntegration` talks to.
    #:
    #: The providers differ in host, HTTP verb, auth header and response
    #: shape, so this is not cosmetic -- it selects a descriptor in
    #: `app.integrations.web_search.PROVIDERS`. An unrecognised value is
    #: refused rather than defaulted, because silently falling back would
    #: send one provider's key to another provider.
    SEARCH_PROVIDER: str = "tavily"
    SEARCH_API_KEY: str = ""

    # --- Telegram adapter --------------------------------------------------
    # All four values are required before the webhook is reachable. The chat
    # id and existing conversation UUID bind Telegram to Mai's one local user;
    # they are adapter configuration, not a new identity system.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    # A string so Docker's deliberately empty optional value does not make
    # settings construction fail; the adapter parses and validates it before
    # accepting any webhook.
    TELEGRAM_ALLOWED_CHAT_ID: str = ""
    TELEGRAM_CONVERSATION_ID: str = ""

    # --- Google Calendar OAuth (Stage 4F-G) ---
    # A "Desktop app" OAuth client from the Google Cloud console. The secret
    # is not really secret for an installed app -- Google says so -- which is
    # why PKCE carries the security rather than the secret.
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    #: Where the consent screen sends the authorization code back. Loopback
    #: only, validated on every use: the code is delivered to a listener on
    #: the user's own machine and never crosses a network.
    GOOGLE_OAUTH_REDIRECT_URI: str = (
        "http://127.0.0.1:8000/api/integrations/google/callback"
    )
    #: Where the Gmail consent screen sends its code back.
    #:
    #: A *separate* path from the Calendar callback, and separate on purpose.
    #: Each callback validates exactly one required scope set, so a Gmail
    #: grant arriving at the Calendar callback is refused and vice versa --
    #: sharing one route would mean dispatching on the pending authorization's
    #: scopes, and a mis-dispatch there would store a grant under the wrong
    #: provider key.
    #:
    #: This must also be registered in the Google Cloud console alongside the
    #: Calendar one; Google requires an exact match.
    GOOGLE_GMAIL_REDIRECT_URI: str = (
        "http://127.0.0.1:8000/api/integrations/gmail/callback"
    )
    #: Where OAuth tokens live. Outside the source tree, outside the image,
    #: mode 0700, files mode 0600. See app/integrations/token_store.py for
    #: why this is not encrypted.
    MAI_CREDENTIAL_DIR: str = "~/.mai/credentials"

    #: The timezone "tomorrow afternoon" is resolved in. An IANA name.
    #:
    #: Stage 4F-G computed calendar windows in UTC, which is wrong everywhere
    #: except UTC: asked at 09:00 in Asia/Kolkata, "tomorrow" resolved to a
    #: window running from 05:30 tomorrow to 05:30 the day after -- missing
    #: the user's morning and including someone else's. A calendar day is a
    #: local idea, so the window has to be built in a local zone.
    #:
    #: UTC remains the default because a wrong-but-declared zone is worse than
    #: an obviously neutral one, and because nothing may guess: a guessed
    #: timezone silently reads the wrong part of someone's calendar.
    MAI_TIMEZONE: str = "UTC"

    # --- Knowledge lifecycle (Stage 3C) ---
    # Conflict evaluation runs in the background pipeline after relationship
    # extraction. It adds no model calls anywhere. Disabling it stops new
    # lifecycle decisions; existing statuses and links are untouched.
    CONFLICT_DETECTION_ENABLED: bool = True
    # Whether a query using historical language ("what did I use before?") may
    # retrieve superseded knowledge. Off means history is stored and traceable
    # but never surfaces in chat.
    HISTORICAL_RETRIEVAL_ENABLED: bool = True

    # --- History import (Stage 5C) ---
    #
    # Imports read from a directory, not an HTTP upload. That is a security
    # decision, not an ergonomic one: an upload endpoint means multipart form
    # parsing, which means adding `python-multipart` and putting Starlette's
    # form parser on a reachable path. This application has no form parsing
    # today, which is exactly why PYSEC-2026-249 is unreachable here, and an
    # import feature is a poor reason to give that up. A bind-mounted
    # directory also keeps a multi-hundred-megabyte export out of the ASGI
    # request path entirely.
    HISTORY_IMPORT_ENABLED: bool = True
    #: Where export files are looked for. Read-only to the application.
    MAI_IMPORT_DIR: str = "~/.mai/imports"

    # Bounds. Every one of these is a hard stop, not a hint: an export is
    # attacker-influenced input in the sense that matters -- it is a large
    # document from outside the system, and a parser without limits is a
    # denial-of-service waiting for a big file.
    IMPORT_MAX_FILE_BYTES: int = 500_000_000
    #: Guards a zip bomb: the *uncompressed* size of any single member, and
    #: the total, are both checked before extraction.
    IMPORT_MAX_UNCOMPRESSED_BYTES: int = 2_000_000_000
    IMPORT_MAX_ZIP_MEMBERS: int = 10_000
    IMPORT_MAX_CONVERSATIONS: int = 10_000
    IMPORT_MAX_MESSAGES_PER_CONVERSATION: int = 2_000
    IMPORT_MAX_TOTAL_MESSAGES: int = 200_000
    IMPORT_MAX_MESSAGE_CHARS: int = 20_000
    IMPORT_MAX_PARTS_PER_MESSAGE: int = 50
    #: Depth cap when walking the export's parent/child node graph. A cyclic
    #: or absurdly deep graph stops here instead of exhausting the stack.
    IMPORT_MAX_THREAD_DEPTH: int = 10_000

    # Derived-memory extraction. Bounded separately, because this is the part
    # that costs model calls: a 5,000-conversation archive must not turn into
    # 5,000 requests the moment someone clicks import.
    IMPORT_MEMORY_EXTRACTION_ENABLED: bool = True
    #: Extraction calls per import run. The run reports what it did not reach,
    #: and re-running continues from there.
    IMPORT_MAX_EXTRACTION_CALLS: int = 200
    #: A conversation needs at least this many user characters to be worth a
    #: model call. Short exchanges carry little durable context.
    IMPORT_MIN_CONVERSATION_CHARS: int = 200
    #: Characters of user text handed to the extractor per conversation.
    IMPORT_EXTRACTION_WINDOW_CHARS: int = 6_000

    # --- Reminders (Stage 5F.1) ---
    #
    # A reminder is local-only: it makes no external call, reads no
    # integration and sends nothing anywhere. What it produces is a row the
    # user reads back through the API.
    REMINDERS_ENABLED: bool = True
    #: How often the scheduler looks for due reminders.
    #:
    #: Thirty seconds is the granularity a person notices for "remind me at
    #: 10am" and cheap enough to run forever: the query is one indexed range
    #: scan over a table holding a handful of rows.
    REMINDER_POLL_SECONDS: int = 30
    #: Whether the poller runs in this process.
    #:
    #: Separate from `REMINDERS_ENABLED` so reminders can be created and
    #: listed with the loop off -- which is what the test suite does, and what
    #: a second process would want if one ever existed.
    REMINDER_SCHEDULER_ENABLED: bool = True

    #: Stage 6F. Whether the one background runtime advances scheduled tasks.
    #:
    #: On by default and inert by default: it only ever touches a task a
    #: person explicitly scheduled with `TaskService.schedule_background`,
    #: which requires an authorised plan, and nothing it advances can execute
    #: unless `EXECUTION_ENABLED` is also on -- which it is not by default.
    #:
    #: The poll interval is `REMINDER_POLL_SECONDS`. There is one loop, so
    #: there is one interval; the name predates the loop serving tasks too.
    BACKGROUND_TASKS_ENABLED: bool = True

    #: Most tasks the runtime advances in one tick. Capped again in code by
    #: `app.background.runtime.HARD_MAX_TASKS_PER_TICK`, so a configuration
    #: value cannot raise the ceiling.
    BACKGROUND_MAX_TASKS_PER_TICK: int = 5

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

    @field_validator("MAI_TIMEZONE")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Refuse an unknown zone at startup rather than falling back.

        A silent fallback to UTC is the failure that hurts: the deployment
        looks configured, every calendar window is quietly built in the wrong
        zone, and the answers are plausible enough that nobody checks. A typo
        should stop the process.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        name = (value or "").strip()
        if not name:
            raise ValueError("MAI_TIMEZONE must not be empty")
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"MAI_TIMEZONE is not a known IANA zone: {name!r}") from exc
        return name

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
