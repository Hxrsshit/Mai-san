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
from app.runtime.capabilities import build as capability_facts
from app.runtime.schemas import RuntimeFacts

logger = get_logger(__name__)

#: Used when a fact genuinely cannot be determined. Saying so is correct;
#: inventing a plausible value is exactly the failure being fixed.
UNKNOWN = "unknown"

#: Capability facts, and the setting each is read from.
#:
#: Declared as data rather than written inline so the set can be checked
#: against `Settings` by a test. A capability Mai reports and a capability the
#: application has must not drift apart silently -- that drift is the same
#: class of fault as the bug this whole layer exists to fix, just slower.
CAPABILITY_SETTINGS = {
    "memory_enabled": "MEMORY_ENABLED",
    "retrieval_enabled": "RETRIEVAL_ENABLED",
    "planning_enabled": "PLANNING_ENABLED",
    "intent_classification_enabled": "INTENT_CLASSIFICATION_ENABLED",
    "action_orchestration_enabled": "ORCHESTRATION_ENABLED",
    "execution_enabled": "EXECUTION_ENABLED",
}

#: Settings deliberately *not* surfaced, each with the reason.
#:
#: Every one is a sub-switch of a capability that is already reported. Listing
#: them keeps the omission a decision rather than an oversight: a test asserts
#: every `*_ENABLED` setting appears here or in `CAPABILITY_SETTINGS`, so a new
#: one forces a choice.
SETTINGS_NOT_SURFACED = {
    "MEMORY_EXTRACTION_ENABLED": "sub-switch of memory_enabled",
    "ENTITY_EXTRACTION_ENABLED": "sub-switch of memory_enabled",
    "RELATIONSHIP_EXTRACTION_ENABLED": "sub-switch of memory_enabled",
    "CONFLICT_DETECTION_ENABLED": "sub-switch of memory_enabled",
    "HISTORICAL_RETRIEVAL_ENABLED": "sub-switch of retrieval_enabled",
    # Stage 5C. Neither is a conversational capability, and reporting one
    # would make Mai claim something it cannot do on request: an import is an
    # operator action against a server-side directory, with no chat trigger.
    # What an import *produces* is already visible the honest way -- derived
    # memories reach a turn through retrieval, like every other memory.
    "HISTORY_IMPORT_ENABLED": "operator action, not a conversational capability",
    "IMPORT_MEMORY_EXTRACTION_ENABLED": "sub-switch of history_import_enabled",
}


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
    if not isinstance(value, str):
        return UNKNOWN
    # Stripped before the emptiness check: whitespace is not a provider name,
    # and reporting "   " as the configured provider is the same kind of
    # confidently-wrong answer as reporting the wrong vendor.
    return value.strip() or UNKNOWN


def _auth_mode_reader(settings):
    """A callable `_safe` can run, so a bad provider name degrades one field.

    An unrecognised `LLM_PROVIDER` raises inside `resolve_mode`; reporting
    `unknown` for the auth mode is the honest degradation, and the provider
    itself will refuse to build for the same reason.
    """

    def read() -> str:
        from app.llm.gateway import PROVIDERS, resolve_mode

        return PROVIDERS[resolve_mode(settings.LLM_PROVIDER)].auth_mode.value

    return read


def build(
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    registered_tool_count: Optional[int] = None,
    executable_tool_count: Optional[int] = None,
    capabilities: Optional[tuple] = None,
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
    # From the provider table keyed by configuration, never from the provider
    # object -- an object could be a stub, and this is an authoritative fact.
    llm_auth_mode = _safe(_auth_mode_reader(settings), "authentication mode")

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

    if executable_tool_count is None:
        try:
            from app.execution.tools import get_executable_registry

            executable_tool_count = len(get_executable_registry())
        except Exception:  # noqa: BLE001
            # Counting failed, so the honest count is zero -- and zero makes
            # `can_execute_actions` false. A fact this layer cannot establish
            # must not be reported as a capability.
            executable_tool_count = 0

    if capabilities is None:
        try:
            capabilities = capability_facts(settings=settings)
        except Exception:  # noqa: BLE001
            # An empty tuple renders as "no tools", which is the conservative
            # answer. This layer fails towards claiming less, never more.
            capabilities = ()

    return RuntimeFacts(
        assistant_name=settings.APP_NAME or "Mai",
        environment=settings.APP_ENV or UNKNOWN,
        llm_provider=llm_provider,
        llm_model=llm_model,
        database=database_dialect(settings.DATABASE_URL),
        llm_auth_mode=llm_auth_mode,
        **{
            fact: bool(getattr(settings, setting))
            for fact, setting in CAPABILITY_SETTINGS.items()
        },
        tool_authorization_enabled=True,
        registered_tool_count=registered_tool_count,
        executable_tool_count=executable_tool_count,
        capabilities=capabilities,
    )


__all__ = ["UNKNOWN", "build", "database_dialect"]
