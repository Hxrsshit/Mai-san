"""Executable tools: a separate, deliberately narrow contract.

Stage 4C's `Tool` defines no way to be run, and a test walks every one of its
subclasses to prove it. That guarantee is **not** weakened here. `ExecutableTool`
is a different base class in a different package; it does not inherit from
`Tool` and never will, so the Stage 4C registry remains exactly what it was --
a registry of declarations, with no method anyone could dispatch against.

Executability is therefore a *second* registry, and a tool has to appear in
both to run:

    registered   the Stage 4C catalogue knows the name
    enabled      the operator switch is on
    authorized   Stage 4C policy permits it for this turn
    executable    THIS registry has an implementation

Four different questions. A tool can be the first three and not the fourth.

Every implementation here is a concrete class, written by hand and registered
by name at import. There is no dynamic import, no lookup of a callable by
string, no plugin loading and no way for a model, a plan, a memory or a
request to add one.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.logging import get_logger
from app.execution import workspace
from app.execution.errors import ToolFailure, WorkspaceViolation
from app.execution.schemas import ExecutionOutcome
from app.tools.base import ToolArguments
from app.tools.catalog import (
    CreateTextFileArguments,
    ListWorkspaceFilesArguments,
    ReadTextFileArguments,
)

logger = get_logger(__name__)


#: An executable tool's argument schema is a Stage 4C `ToolArguments`.
#:
#: Not a parallel base class: the *same* class. Authorization validates a
#: proposal against the declared schema and the dispatcher validates the same
#: arguments again before running, and if those were two types they could
#: drift apart -- leaving a payload that authorizes as one action and runs as
#: another. An alias makes that divergence unrepresentable rather than merely
#: unlikely. `extra="forbid"` and `frozen=True` come with it.
ExecutableArguments = ToolArguments


class ExecutionContext(BaseModel):
    """What a tool is allowed to know. Deliberately almost nothing.

    No session, no settings object, no provider, no request, no context
    package, no conversation and no memory. A tool receives the workspace
    root, its own bounds, and -- if it declared one -- a single integration
    adapter. It can reach nothing else.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    workspace_root: Path
    max_file_bytes: int = 1_000_000
    max_list_results: int = 500
    max_list_depth: int = 6

    #: The one integration this tool declared, resolved by the dispatcher.
    #:
    #: Singular, and not the registry. A tool that names `example` receives
    #: the `example` adapter and nothing else, so it cannot reach an
    #: integration it did not declare -- least privilege as a field type
    #: rather than as a convention. `None` for every filesystem tool, which
    #: is why they cannot reach the network at all.
    integration: Optional[Any] = None


class ExecutableTool(ABC):
    """A capability with an implementation behind it.

    Not a `Tool`. The two are related only by name: the dispatcher looks a
    Stage 4C definition up for authorization metadata, and looks an
    `ExecutableTool` up for the implementation, and both must be present.
    """

    #: Must match a name registered in the Stage 4C catalogue.
    name: str = ""

    #: The integration this tool uses, or "" for none.
    #:
    #: Declared on the base rather than only on `IntegrationTool` so the
    #: dispatcher can read it as an ordinary attribute. The alternative was
    #: `getattr(tool, "integration_name", "")`, and `getattr` is exactly the
    #: string-to-code primitive the dispatcher is tested for not having --
    #: benign here, but a test that has to permit it stops being able to
    #: refuse the dangerous uses.
    integration_name: str = ""

    @property
    @abstractmethod
    def arguments_model(self) -> Type[ExecutableArguments]:
        """The schema arguments are validated against before anything runs."""

    @abstractmethod
    def run(self, arguments: ExecutableArguments, context: ExecutionContext) -> ExecutionOutcome:
        """Perform the action.

        Called by the dispatcher only, and only from `APPROVED`, only after
        approval integrity, authorization, idempotency and state have all been
        checked. An implementation may assume its arguments are validated; it
        may assume nothing else.

        Raises `ToolFailure` or `WorkspaceViolation` on failure. Returning
        normally means the side effect happened.
        """

    def validate_arguments(self, raw: Dict[str, Any]) -> ExecutableArguments:
        """Validate, or raise `ToolFailure` naming the fields that failed."""
        try:
            return self.arguments_model.model_validate(raw)
        except ValidationError as exc:
            fields = sorted(
                {
                    ".".join(str(part) for part in error["loc"])
                    for error in exc.errors()
                    if error.get("loc")
                }
            )
            # Field names only. Values are user input and may be anything.
            raise ToolFailure(
                reason="invalid_arguments", detail=",".join(fields)
            ) from exc


# --- Tool 1: create a text file ---------------------------------------------



class CreateTextFileTool(ExecutableTool):
    """Write a text file inside the workspace."""

    name = "create_text_file"

    @property
    def arguments_model(self) -> Type[ExecutableArguments]:
        return CreateTextFileArguments

    def run(self, arguments, context: ExecutionContext) -> ExecutionOutcome:
        target = workspace.resolve_in(context.workspace_root, arguments.path)

        encoded = arguments.content.encode("utf-8")
        if len(encoded) > context.max_file_bytes:
            raise ToolFailure(reason="content_too_large")

        parent = target.parent
        if not workspace._is_inside(parent.resolve(), context.workspace_root):
            raise WorkspaceViolation(detail="parent outside workspace")
        parent.mkdir(parents=True, exist_ok=True)

        # O_EXCL is the guard, not a prior existence check: it makes creation
        # atomic, so two concurrent attempts cannot both believe they created
        # the file, and a symlink planted between resolution and write cannot
        # be followed.
        flags = os.O_WRONLY | os.O_CREAT | (
            os.O_TRUNC if arguments.overwrite else os.O_EXCL
        )
        if not arguments.overwrite:
            flags |= os.O_NOFOLLOW

        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError as exc:
            raise ToolFailure(reason="file_already_exists") from exc
        except OSError as exc:
            raise ToolFailure(reason="could_not_create_file") from exc

        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
        except OSError as exc:
            raise ToolFailure(reason="could_not_write_file") from exc

        relative = workspace.relative_to_root(target, context.workspace_root)
        return ExecutionOutcome(
            summary=f"Created {relative} ({len(encoded)} bytes).",
            data={"path": relative, "bytes_written": len(encoded)},
        )


# --- Tool 2: read a text file -----------------------------------------------



class ReadTextFileTool(ExecutableTool):
    """Read a text file from the workspace, bounded in size."""

    name = "read_text_file"

    @property
    def arguments_model(self) -> Type[ExecutableArguments]:
        return ReadTextFileArguments

    def run(self, arguments, context: ExecutionContext) -> ExecutionOutcome:
        target = workspace.resolve_in(context.workspace_root, arguments.path)

        if target.is_symlink():
            # Its own resolution was checked, but a symlink is not workspace
            # content; refusing keeps "what the workspace contains" simple.
            raise WorkspaceViolation(detail="symlink")
        if not target.is_file():
            raise ToolFailure(reason="file_not_found")

        size = target.stat().st_size
        if size > context.max_file_bytes:
            raise ToolFailure(reason="file_too_large")

        raw = target.read_bytes()
        if b"\x00" in raw:
            # A null byte is the cheapest reliable binary signal, and this
            # tool's contract is text.
            raise ToolFailure(reason="file_is_not_text")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolFailure(reason="file_is_not_text") from exc

        relative = workspace.relative_to_root(target, context.workspace_root)
        return ExecutionOutcome(
            summary=f"Read {relative} ({size} bytes).",
            data={"path": relative, "bytes": size, "content": text},
        )


# --- Tool 3: list workspace files -------------------------------------------



class ListWorkspaceFilesTool(ExecutableTool):
    """List files in the workspace, bounded in depth and count."""

    name = "list_workspace_files"

    @property
    def arguments_model(self) -> Type[ExecutableArguments]:
        return ListWorkspaceFilesArguments

    def run(self, arguments, context: ExecutionContext) -> ExecutionOutcome:
        files = workspace.list_files(
            root=context.workspace_root,
            max_results=context.max_list_results,
            max_depth=context.max_list_depth,
            subdirectory=arguments.path,
        )
        return ExecutionOutcome(
            summary=f"Found {len(files)} file(s) in the workspace.",
            data={"files": files, "count": len(files)},
        )


# --- The executable registry ------------------------------------------------


class ExecutableRegistry:
    """The closed set of tools that have an implementation."""

    def __init__(self) -> None:
        self._tools: Dict[str, ExecutableTool] = {}

    def register(self, tool: ExecutableTool) -> None:
        name = (tool.name or "").strip().lower()
        if not name:
            raise ValueError("an executable tool must have a name")
        if name in self._tools:
            raise ValueError(f"executable tool {name!r} is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Optional[ExecutableTool]:
        """Exact lookup, or None. No fuzzy matching, as in Stage 4C."""
        return self._tools.get((name or "").strip().lower())

    def contains(self, name: str) -> bool:
        return (name or "").strip().lower() in self._tools

    def names(self) -> tuple:
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)


_registry = ExecutableRegistry()


def build_executable_registry(
    registry: Optional[ExecutableRegistry] = None,
) -> ExecutableRegistry:
    """Register the Stage 4E tools. The only place `register` is called.

    Each is a hand-written class. Adding another means editing this function.

    `WebSearchTool` is imported here rather than at module scope to break a
    cycle: it subclasses `AsyncIntegrationTool`, which lives in a module that
    imports `ExecutableTool` from this one. The import is still a concrete
    class named in code -- there is no dynamic lookup and nothing a string
    could reach.
    """
    from app.execution.calendar_tool import CalendarListEventsTool
    from app.execution.gmail_tools import GmailGetMessageTool, GmailListMessagesTool
    from app.execution.web_search_tool import WebSearchTool

    target = registry if registry is not None else _registry
    target.register(CreateTextFileTool())
    target.register(ReadTextFileTool())
    target.register(ListWorkspaceFilesTool())
    target.register(WebSearchTool())
    target.register(CalendarListEventsTool())
    target.register(GmailListMessagesTool())
    target.register(GmailGetMessageTool())
    return target


def get_executable_registry() -> ExecutableRegistry:
    return _registry


build_executable_registry()


__all__ = [
    "CreateTextFileTool",
    "ExecutableArguments",
    "ExecutableRegistry",
    "ExecutableTool",
    "ExecutionContext",
    "ListWorkspaceFilesTool",
    "ReadTextFileTool",
    "build_executable_registry",
    "get_executable_registry",
]
