"""The tool catalogue: every capability the application declares.

This module is the *only* place `register` is called. Tool classes are
imported by name at module scope -- there is no dynamic import, no lookup of a
class by string, and no plugin loading. Adding a tool means editing this file.

Nothing here implements a capability. Stage 4C registers **declarations**: a
name, a category, a risk level and an approval requirement. The `future_*`
entries exist so the risk model can be exercised end to end against realistic
metadata, and every one of them is refused or gated by policy today.
"""

from typing import Optional, Type

from pydantic import Field

from app.core.logging import get_logger
from app.tools.base import Tool, ToolArguments
from app.tools.registry import ToolRegistry, get_registry
from app.tools.schemas import RiskLevel, ToolCategory, ToolDefinition

logger = get_logger(__name__)


# --- The one inert tool -----------------------------------------------------


class EchoArguments(ToolArguments):
    """Arguments for `echo`. One bounded string, nothing else."""

    text: str = Field(..., min_length=1, max_length=500)


class EchoTool(Tool):
    """A deliberately inert tool, present only to exercise the framework.

    It has no side effects because it has no behaviour at all: like every
    Stage 4C tool it declares metadata and an argument schema and stops. There
    is no file access, no network call, no database write and no code
    execution, because there is no method through which any of those could
    happen.

    It exists so the framework's happy path is testable: a registered,
    enabled, low-risk tool that policy does not forbid, which is what makes
    the `ALLOWED` state reachable and therefore meaningful.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="Returns its input unchanged. Framework test tool.",
            category=ToolCategory.DIAGNOSTIC,
            risk_level=RiskLevel.LOW,
            # The only registered tool that does not require approval, so the
            # ALLOWED branch of policy has something to demonstrate.
            requires_approval=False,
        )

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        return EchoArguments


# --- Declared future capabilities -------------------------------------------
# Metadata only. None of these has an implementation, and each is refused or
# gated by policy today. They are registered so the risk ladder is exercised
# against realistic names rather than against invented test fixtures.


class _DeclaredTool(Tool):
    """A capability that is described but does not exist.

    Subclasses supply metadata. None supplies behaviour -- there is no method
    on `Tool` through which behaviour could be supplied.
    """

    _definition: ToolDefinition

    @property
    def definition(self) -> ToolDefinition:
        return self._definition


def _declare(
    name: str,
    description: str,
    category: ToolCategory,
    risk_level: RiskLevel,
    requires_approval: bool = True,
) -> Tool:
    tool = _DeclaredTool()
    tool._definition = ToolDefinition(
        name=name,
        description=description,
        category=category,
        risk_level=risk_level,
        requires_approval=requires_approval,
        # Declared but not permitted: a capability can sit in the catalogue
        # while the architecture that would make it safe is still being built.
        enabled=False,
    )
    return tool


def build_catalog(registry: Optional[ToolRegistry] = None) -> ToolRegistry:
    """Register every declared tool. Idempotent per registry instance.

    Called once at import time for the process registry, and callable with a
    fresh `ToolRegistry()` from tests so they never mutate module state.
    """
    target = registry if registry is not None else get_registry()

    target.register(EchoTool())

    for tool in (
        _declare(
            "future_web_search",
            "Search the public web. Not implemented.",
            ToolCategory.INFORMATION,
            RiskLevel.LOW,
        ),
        _declare(
            "future_generate_document",
            "Produce a document from structured input. Not implemented.",
            ToolCategory.CREATIVE,
            RiskLevel.MEDIUM,
        ),
        _declare(
            "future_send_email",
            "Send an email on the user's behalf. Not implemented.",
            ToolCategory.COMMUNICATION,
            RiskLevel.HIGH,
        ),
        _declare(
            "future_delete_file",
            "Delete a file from local storage. Not implemented.",
            ToolCategory.FILE_OPERATION,
            RiskLevel.CRITICAL,
        ),
    ):
        target.register(tool)

    return target


#: Build the process registry at import time.
build_catalog()


__all__ = ["EchoArguments", "EchoTool", "build_catalog"]
