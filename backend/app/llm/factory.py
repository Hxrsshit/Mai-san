"""Provider factory and lifecycle.

Adding a provider is a two-line change here plus one file under `providers/`.
Nothing else in the application needs to know which backend is active.
"""

from typing import Callable, Dict, Optional

from app.core.config import Settings, get_settings
from app.core.errors import MaiError
from app.core.logging import get_logger
from app.llm.base import LLMProvider
from app.llm.providers.groq import GroqProvider

logger = get_logger(__name__)

# name -> builder. Adding a provider is one line here plus one file
# under `providers/`; nothing above the abstraction changes.
_REGISTRY: Dict[str, Callable[[Settings], LLMProvider]] = {
    "groq": GroqProvider.from_settings,
}

_provider: Optional[LLMProvider] = None


class UnknownProviderError(MaiError):
    status_code = 500
    code = "unknown_llm_provider"


def build_provider(settings: Optional[Settings] = None) -> LLMProvider:
    """Construct a provider from settings without caching it."""
    settings = settings or get_settings()
    key = settings.LLM_PROVIDER.strip().lower()

    builder = _REGISTRY.get(key)
    if builder is None:
        raise UnknownProviderError(
            f"Unknown LLM_PROVIDER {settings.LLM_PROVIDER!r}. "
            f"Available: {', '.join(sorted(_REGISTRY))}."
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
