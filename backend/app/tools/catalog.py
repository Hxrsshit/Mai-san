"""The tool catalogue: every capability the application declares.

This module is the *only* place `register` is called. Tool classes are
imported by name at module scope -- there is no dynamic import, no lookup of a
class by string, and no plugin loading. Adding a tool means editing this file.

Most of this file is **declarations**: a name, a category, a risk level and
an approval requirement. The `future_*` entries exist so the risk model can be
exercised end to end against realistic metadata, and every one of them is
refused or gated by policy today.

Three entries are different. `create_text_file`, `read_text_file` and
`list_workspace_files` have implementations, added in Stage 4E, and those
implementations live in `app.execution.tools` -- not here. What this file
declares about them is still only metadata and an argument schema; the
capability to run them exists in one dispatcher, behind an approval bound to
the exact payload, and only when execution is switched on.
"""

from typing import Optional, Type

from pydantic import Field

from app.core.logging import get_logger
from app.tools.base import Tool, ToolArguments
from app.tools.registry import ToolRegistry, get_registry
from app.tools.schemas import (
    ExecutionMode,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
)

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


# --- Executable workspace tools (Stage 4E) ----------------------------------
# The first three tools in Mai with an implementation behind them. Everything
# they can touch lives under the configured workspace root, and every one of
# them requires an explicit human approval bound to the exact payload.
#
# Their argument schemas are defined **here**, in the declaration layer, and
# imported by the executor. That is not tidiness: authorization validates a
# proposal against the declared schema, and the dispatcher validates the same
# arguments again before running. If those were two classes they could drift,
# and the gap between them would be a payload that passes authorization and
# then runs as something else. One class per tool makes the gap impossible.


class CreateTextFileArguments(ToolArguments):
    """Arguments for `create_text_file`."""

    path: str = Field(..., min_length=1, max_length=400)
    content: str = Field(..., max_length=100_000)
    #: Overwriting is a materially different action from creating, so it is a
    #: separate parameter -- and because it is part of the payload, an
    #: approval for `overwrite=False` does not approve `overwrite=True`.
    overwrite: bool = False


class ReadTextFileArguments(ToolArguments):
    """Arguments for `read_text_file`."""

    path: str = Field(..., min_length=1, max_length=400)


class ListWorkspaceFilesArguments(ToolArguments):
    """Arguments for `list_workspace_files`."""

    #: Optional subdirectory, itself resolved inside the workspace.
    path: Optional[str] = Field(default=None, max_length=400)


class WebSearchArguments(ToolArguments):
    """Arguments for `web_search`.

    A query and two bounded knobs. Note what is absent: no `url`, no
    `endpoint`, no `method`, no `headers`, no `api_key`. A user cannot choose
    a destination and cannot supply a credential, because there is no field
    for either -- the same "a model cannot set what it cannot name" reasoning
    the earlier stages used, applied to the network.
    """

    query: str = Field(..., min_length=1, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)
    #: Defaults to on. A research assistant has no reason to default to
    #: fewer filters, and turning it off is part of the approved payload.
    safe_search: bool = True


class _ExecutableDeclaration(Tool):
    """A declaration whose implementation lives in `app.execution.tools`.

    Still no `execute` method -- this class is the *description*, and Stage
    4C's guarantee that a `Tool` cannot be run is untouched. The executor is a
    separate object in a separate package, reached only by the dispatcher, and
    the two are matched by name.
    """

    _definition: ToolDefinition
    _arguments: Type[ToolArguments]

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        return self._arguments


def _declare_executable(
    name: str,
    description: str,
    risk_level: RiskLevel,
    arguments: Type[ToolArguments],
    category: ToolCategory = ToolCategory.FILE_OPERATION,
) -> Tool:
    """Declare a capability that Stage 4E can actually perform.

    `requires_approval=True` on every one, including the read-only tools.
    Reading is lower risk than writing, not zero risk -- it is the step that
    moves file contents into a prompt -- and Stage 4E's rule is that anything
    with a side effect is approved explicitly, per payload. The risk ladder
    still distinguishes them; the approval requirement does not.
    """
    tool = _ExecutableDeclaration()
    tool._definition = ToolDefinition(
        name=name,
        description=description,
        category=category,
        risk_level=risk_level,
        # Approval is retained for search, not waived for convenience. It is
        # read-only, but it is the one tool that sends what the user asked
        # about to someone else -- and a per-query approval is exactly where
        # a person gets to decide whether that is acceptable for this query.
        requires_approval=True,
        execution_mode=ExecutionMode.SYNCHRONOUS,
        enabled=True,
    )
    tool._arguments = arguments
    return tool


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
    enabled: bool = True,
) -> Tool:
    """Declare a capability that does not exist.

    `enabled` and `execution_mode` answer different questions, and Stage 4D
    is where the difference starts to matter:

    - `enabled` is the **operator's** switch: would we permit this capability?
    - `execution_mode` is the **application's** statement of fact: can it run?
      It is `unavailable` for every tool declared *here*, and a definition may
      only say otherwise where an executor genuinely exists -- which for these
      it does not.

    Stage 4C set `enabled=False` on all of these because nothing consumed the
    registry, so the switch had no meaning. Stage 4D introduces the consumer,
    and leaving every switch off would make `APPROVAL_REQUIRED` unreachable --
    collapsing a required outcome into a vestigial one and hiding the
    difference between "we do not permit this" and "a human would have to
    confirm it".

    So the operator switch now reflects a real position, and nothing about
    executability changed: every tool declared through this helper is still
    `unavailable`, still has no implementation, and still cannot run. Stage 4E
    did not change that -- it added three *separate* entries above with real
    executors, and deliberately left `future_send_email` and
    `future_delete_file` exactly as they were.
    """
    tool = _DeclaredTool()
    tool._definition = ToolDefinition(
        name=name,
        description=description,
        category=category,
        risk_level=risk_level,
        requires_approval=requires_approval,
        enabled=enabled,
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
        _declare_executable(
            "create_text_file",
            "Create or overwrite a text file inside the Mai workspace.",
            RiskLevel.MEDIUM,
            CreateTextFileArguments,
        ),
        _declare_executable(
            "read_text_file",
            "Read a text file from inside the Mai workspace.",
            RiskLevel.LOW,
            ReadTextFileArguments,
        ),
        _declare_executable(
            "list_workspace_files",
            "List files inside the Mai workspace.",
            RiskLevel.LOW,
            ListWorkspaceFilesArguments,
        ),
        _declare_executable(
            "web_search",
            "Search the public web for a query and return sources.",
            # Read-only, and still MEDIUM rather than LOW: a search sends the
            # user's question to a third party, which the three filesystem
            # tools do not do. The risk is a privacy one, not a destruction
            # one, and the ladder should say so.
            RiskLevel.MEDIUM,
            WebSearchArguments,
            category=ToolCategory.INFORMATION,
        ),
    ):
        target.register(tool)

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
            # Refused twice over: the operator switch is off *and* the risk
            # level is above the ceiling. Either alone would forbid it; both
            # is deliberate, so removing one does not quietly permit it.
            enabled=False,
        ),
    ):
        target.register(tool)

    return target


#: Build the process registry at import time.
build_catalog()


__all__ = [
    "CreateTextFileArguments",
    "WebSearchArguments",
    "EchoArguments",
    "EchoTool",
    "ListWorkspaceFilesArguments",
    "ReadTextFileArguments",
    "build_catalog",
]
