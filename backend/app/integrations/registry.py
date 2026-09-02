"""The integration registry. Explicit, immutable after startup, small.

Deliberately a near-copy of the Stage 4C `ToolRegistry` in shape: same
canonicalisation rule, same refusal to fuzzy-match, same "registration happens
in one file at import time". Two registries answering different questions,
behaving the same way, is easier to reason about than one clever registry
answering both.

**Stage 4F-A registers nothing.** The catalogue below is empty, and that is
the deliverable: the foundation exists and no external service is reachable
through it. Adding an integration is a code change to `build_integrations`,
reviewed like any other.
"""

from typing import Dict, Optional, Tuple

from app.core.logging import get_logger
from app.integrations.base import Integration
from app.integrations.errors import UnknownIntegration

logger = get_logger(__name__)


class IntegrationRegistry:
    """Name to adapter. No dynamic loading, no lookup by anything but name."""

    def __init__(self) -> None:
        self._integrations: Dict[str, Integration] = {}
        self._sealed = False

    @staticmethod
    def canonical(name: str) -> str:
        """Strip and lowercase. The only normalisation, as in Stage 4C.

        Nothing further -- no stemming, no edit distance, no prefix matching.
        Any of those could map a name onto a different integration, and a
        near-miss must fail rather than resolve to something plausible.
        """
        return (name or "").strip().lower()

    def register(self, integration: Integration) -> None:
        """Add one adapter. Refuses a duplicate and refuses after sealing."""
        if self._sealed:
            # Registration is a startup activity. A registry that could grow
            # at runtime is a registry a request could grow.
            raise RuntimeError("the integration registry is sealed")

        name = self.canonical(integration.name)
        if not name:
            raise ValueError("an integration must have a name")
        if name in self._integrations:
            # Silently replacing would let a second registration take over a
            # name that tools already reference.
            raise ValueError(f"integration {name!r} is already registered")

        self._integrations[name] = integration
        logger.info("Integration registered", extra={"integration": name})

    def seal(self) -> None:
        """Close the registry. Called once, after the catalogue is built."""
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    def get(self, name: str) -> Optional[Integration]:
        """Exact lookup, or None. Fails closed: no fallback, no guessing."""
        return self._integrations.get(self.canonical(name))

    def require(self, name: str) -> Integration:
        """The adapter, or raise. For callers that cannot proceed without."""
        integration = self.get(name)
        if integration is None:
            raise UnknownIntegration(detail=self.canonical(name))
        return integration

    def contains(self, name: str) -> bool:
        return self.canonical(name) in self._integrations

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._integrations))

    def __len__(self) -> int:
        return len(self._integrations)


_registry = IntegrationRegistry()


def build_integrations(
    registry: Optional[IntegrationRegistry] = None,
) -> IntegrationRegistry:
    """Register every integration. The only place `register` is called.

    Empty in Stage 4F-A, on purpose. The stage builds the road and connects
    nothing to it: no web search, no email, no calendar, no arbitrary HTTP.
    Adding one means editing this function, importing a concrete class at
    module scope, and having that reviewed.
    """
    target = registry if registry is not None else _registry
    # Intentionally no integrations. See the module docstring.
    return target


def get_integration_registry() -> IntegrationRegistry:
    return _registry


build_integrations()
_registry.seal()


__all__ = [
    "IntegrationRegistry",
    "build_integrations",
    "get_integration_registry",
]
