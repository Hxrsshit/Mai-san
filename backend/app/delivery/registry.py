"""The adapter registry. Name to adapter; explicit, small, sealable.

The same shape as `app.integrations.registry.IntegrationRegistry`: one
canonicalisation rule, no fuzzy matching, refusal of duplicates, and a seal
after which nothing can be added. An adapter is registered by code, never by
configuration, content or a request -- so no message can name a channel into
existence.
"""

from typing import Dict, Optional, Tuple

from app.core.logging import get_logger
from app.delivery.contract import ADAPTER_NAME, NotificationAdapter

logger = get_logger(__name__)


class AdapterRegistry:
    """Name to adapter. No dynamic loading, no lookup by anything but name."""

    def __init__(self) -> None:
        self._adapters: Dict[str, NotificationAdapter] = {}
        self._sealed = False

    @staticmethod
    def canonical(name) -> str:
        """Strip and lowercase. The only normalisation."""
        return (name or "").strip().lower() if isinstance(name, str) else ""

    def register(self, adapter: NotificationAdapter) -> None:
        """Add one adapter. Refuses a non-adapter, a bad name, a duplicate,
        and anything after sealing."""
        if self._sealed:
            raise RuntimeError("the adapter registry is sealed")
        if not isinstance(adapter, NotificationAdapter):
            raise TypeError("not a NotificationAdapter")
        name = self.canonical(adapter.name)
        if not ADAPTER_NAME.fullmatch(name):
            raise ValueError("adapter names are lowercase identifiers")
        if name in self._adapters:
            raise ValueError(f"adapter {name!r} is already registered")
        self._adapters[name] = adapter
        logger.info("Notification adapter registered", extra={"adapter": name})

    def seal(self) -> None:
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    def get(self, name) -> Optional[NotificationAdapter]:
        """Exact lookup, or None. Fails closed."""
        return self._adapters.get(self.canonical(name))

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def __len__(self) -> int:
        return len(self._adapters)


__all__ = ["AdapterRegistry"]
