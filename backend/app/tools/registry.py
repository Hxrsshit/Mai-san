"""The application-controlled tool registry.

What exists is decided in code, at import time, from an explicit list. Nothing
else can add to it:

- **The model cannot register a tool.** `register` is called only from
  `catalog.py`, which imports concrete classes by name at module scope. There
  is no dynamic import, no `getattr` on a module, no class lookup by string.
- **Request data cannot register a tool.** No route, service or request-path
  module calls `register`. A structural test asserts this.
- **Unknown means unavailable.** Lookup is an exact dictionary hit on a
  canonical name. There is no fuzzy fallback, no nearest-match, no retry with
  a normalised variant beyond the strip-and-lowercase that cannot change which
  tool is meant.

Lookups are dictionary access: constant time, no database, no network, no
model call.
"""

from typing import Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.tools.base import Tool
from app.tools.schemas import ToolDefinition

logger = get_logger(__name__)


class DuplicateToolError(Exception):
    """A tool name was registered twice.

    Refused rather than overwritten: silently replacing a registration is how
    a low-risk declaration quietly takes over a high-risk name.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"tool {name!r} is already registered")
        self.name = name


class ToolRegistry:
    """A closed set of declared capabilities."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    # --- Registration (application code only) -------------------------------

    def register(self, tool: Tool) -> None:
        """Add a tool. Called from `catalog.py` at import time and nowhere else."""
        name = tool.definition.name
        if name in self._tools:
            raise DuplicateToolError(name)
        self._tools[name] = tool
        logger.info(
            "Tool registered",
            extra={
                "tool": name,
                "category": tool.definition.category.value,
                "risk_level": tool.definition.risk_level.value,
                "requires_approval": tool.definition.requires_approval,
                "execution_mode": tool.definition.execution_mode.value,
            },
        )

    # --- Lookup -------------------------------------------------------------

    @staticmethod
    def canonical(name: str) -> str:
        """The only normalisation performed: strip and lowercase.

        Both are safe because neither can change which tool is meant.
        Anything further -- stemming, edit distance, prefix matching -- could
        map `delete_all_files` onto `delete_file`, so none is done.
        """
        return (name or "").strip().lower()

    def contains(self, name: str) -> bool:
        return self.canonical(name) in self._tools

    def get(self, name: str) -> Optional[Tool]:
        """Exact lookup, or None. Fails closed: no fallback, no guessing."""
        return self._tools.get(self.canonical(name))

    def definition(self, name: str) -> Optional[ToolDefinition]:
        tool = self.get(name)
        return tool.definition if tool is not None else None

    def list_registered(self) -> Tuple[ToolDefinition, ...]:
        """Every declaration, name-sorted.

        Returns a tuple of frozen definitions, so a caller can neither mutate
        the registry's collection nor the metadata inside it.
        """
        return tuple(
            self._tools[name].definition for name in sorted(self._tools)
        )

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)


#: The process-wide registry. Populated by `catalog.py` at import time.
_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """The application registry.

    Exposed as a function so tests can build an isolated `ToolRegistry()`
    without reaching into module state, and so nothing imports a mutable
    module-level name it might rebind.
    """
    return _registry


__all__ = ["DuplicateToolError", "ToolRegistry", "get_registry"]
