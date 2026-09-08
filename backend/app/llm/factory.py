"""Provider factory and lifecycle.

Adding a provider is a two-line change here plus one file under `providers/`.
Nothing else in the application needs to know which backend is active.
"""

from typing import Callable, Dict, Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.gateway import (
    ProviderMode,
    UnknownProviderMode,
    require_available,
    resolve_mode,
)
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.groq import GroqProvider

logger = get_logger(__name__)

# mode -> builder. Every entry is a provider Mai can actually construct.
#
# `claude_subscription` is deliberately absent. It is a *known* mode -- the
# gateway names it and reports why it cannot be used -- but there is no
# builder, so there is nothing to accidentally call. A mode with a builder
# that raised would be one refactor away from a mode that works.
_REGISTRY: Dict[ProviderMode, Callable[[Settings], LLMProvider]] = {
    ProviderMode.GROQ: GroqProvider.from_settings,
    ProviderMode.ANTHROPIC_API: AnthropicProvider.from_settings,
}

_provider: Optional[LLMProvider] = None


#: One class, two names. `UnknownProviderMode` is raised by the gateway,
#: which cannot import this module without a cycle; `UnknownProviderError` is
#: the name callers and tests have used since Stage 1. Aliasing rather than
#: subclassing keeps a single class, so `except` on either name catches the
#: same thing and there is no hierarchy to get the wrong way round.
UnknownProviderError = UnknownProviderMode


def build_provider(settings: Optional[Settings] = None) -> LLMProvider:
    """Construct the configured provider. One provider, no fallback.

    Three refusals, in order, and each says something different:

    - an unrecognised name is a typo (`UnknownProviderMode`);
    - a recognised but unusable provider is a policy decision, reported with
      its reason (`ProviderUnavailable`);
    - a usable provider with no builder is a bug here, not a configuration
      problem, and says so.

    Nothing in this function falls back to another provider. A provider that
    quietly failed over would send the conversation to a company the operator
    did not choose and bill an account they did not mean to use.
    """
    settings = settings or get_settings()

    mode = resolve_mode(settings.LLM_PROVIDER)
    require_available(mode)

    builder = _REGISTRY.get(mode)
    if builder is None:
        raise UnknownProviderError(
            f"The {mode.value} provider is available but has no builder."
        )
    return builder(settings)


def init_provider(settings: Optional[Settings] = None) -> LLMProvider:
    """Build and cache the process-wide provider."""
    global _provider
    if _provider is None:
        _provider = build_provider(settings)
        logger.info(
            "LLM provider initialised",
            extra={"provider": _provider.name, "model": _provider.model},
        )
    return _provider


def get_llm_provider() -> LLMProvider:
    """FastAPI dependency. Override this in tests to inject a fake provider."""
    return init_provider()


async def dispose_provider() -> None:
    """Release provider resources on shutdown."""
    global _provider
    if _provider is not None:
        await _provider.close()
        logger.info("LLM provider disposed")
    _provider = None
