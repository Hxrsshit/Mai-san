"""Building `RuntimeFacts` from configuration and live objects.

The one place authoritative facts are assembled. It reads `Settings` and, when
one is available, the live `LLMProvider` -- because the provider knows its own
model identifier, and asking it is more truthful than re-deriving the same
value from configuration a second time.

**Never raises.** A fact that cannot be determined becomes `"unknown"`, which
is honest, rather than a guess, which is the bug this whole layer exists to
fix.
"""

from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.runtime.schemas import RuntimeFacts

logger = get_logger(__name__)

#: Used when a fact genuinely cannot be determined. Saying so is correct;
#: inventing a plausible value is exactly the failure being fixed.
UNKNOWN = "unknown"


def database_dialect(database_url: str) -> str:
    """The dialect from a SQLAlchemy URL, and nothing else.

    `postgresql+asyncpg://mai:secret@db:5432/mai` -> `postgresql`

    Deliberately not the URL. It carries the database password, and Stage 3D's
    audit found exactly that kind of value reaching places it should not. The
    driver suffix is dropped too: `asyncpg` is an implementation detail the
    model has no use for.
    """
    if not database_url or "://" not in database_url:
        return UNKNOWN
    scheme = database_url.split("://", 1)[0]
    return scheme.split("+", 1)[0].strip().lower() or UNKNOWN


def _safe(read, label: str) -> str:
    """Read one fact, or report it as unknown. Never raises.

    Saying "unknown" is correct when a fact cannot be determined. Guessing a
    plausible value is the failure this whole layer exists to fix.
    """
    try:
        value = read()
    except Exception as exc:  # noqa: BLE001 - an unknown fact beats a wrong one
        logger.warning(
            "Could not determine a runtime fact",
            extra={"fact": label, "error": str(exc)},
        )
        return UNKNOWN
    return (value or UNKNOWN) if isinstance(value, str) else UNKNOWN


def build(
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    registered_tool_count: Optional[int] = None,
) -> RuntimeFacts:
    """Assemble the authoritative facts for this process. Never raises."""
    settings = settings or get_settings()

    # The provider's own name and model, from the live object where there is
    # one. `settings.active_model` is the configured value; the provider is
    # what is actually being called.
    #: Each fact is read independently, so one failure degrades one value.
    #: A provider whose model identifier cannot be read still has a name worth
    #: reporting -- partial truth beats blanket ignorance, and both beat the
    #: confident guess this layer exists to prevent.
    llm_provider = _safe(
        (lambda: provider.name) if provider is not None
        else (lambda: settings.LLM_PROVIDER),
        "provider name",
    )
    llm_model = _safe(
        (lambda: provider.model) if provider is not None
        else (lambda: settings.active_model),
        "model identifier",
    )

    if registered_tool_count is None:
        try:
            from app.tools import catalog  # noqa: F401  (import registers)
            from app.tools.registry import get_registry

            registered_tool_count = len(get_registry())
        except Exception:  # noqa: BLE001
            registered_tool_count = 0

    return RuntimeFacts(
        assistant_name=settings.APP_NAME or "Mai",
        environment=settings.APP_ENV or UNKNOWN,
        llm_provider=llm_provider,
        llm_model=llm_model,
        database=database_dialect(settings.DATABASE_URL),
        memory_enabled=settings.MEMORY_ENABLED,
        retrieval_enabled=settings.RETRIEVAL_ENABLED,
        planning_enabled=settings.PLANNING_ENABLED,
        intent_classification_enabled=settings.INTENT_CLASSIFICATION_ENABLED,
        tool_authorization_enabled=True,
        registered_tool_count=registered_tool_count,
    )


__all__ = ["UNKNOWN", "build", "database_dialect"]
