"""Stage 4C secure tool abstraction and authorization framework.

The boundary future capabilities must pass through:

    ActionProposal  ->  registry lookup  ->  policy  ->  AuthorizationDecision
    (untrusted)         (application)        (deterministic)

**No capability is implemented here.** No web access, no filesystem, no shell,
no subprocess, no network client, no code execution. The one registered tool
with an argument schema, `echo`, has no behaviour -- like every Stage 4C tool
it declares metadata and stops.

**No tool can execute.** Not because a dispatcher declines to call one, but
because `Tool` defines no method through which anything could be called, and
no registered definition may declare itself executable. Stage 4D adds both,
deliberately, alongside whatever gates them.

Three authority rules carry forward from earlier stages:

- The application decides what tools exist (`catalog.py`, `registry.py`).
- The application decides whether one is permitted (`policy.py`), monotonically.
- Stage 4A's capability boundary stays authoritative; where the two overlap,
  the more restrictive decision wins.
"""

from app.tools.authorization import AuthorizationService
from app.tools.base import ArgumentValidationError, Tool, ToolArguments
from app.tools.catalog import EchoTool, build_catalog
from app.tools.policy import evaluate
from app.tools.registry import DuplicateToolError, ToolRegistry, get_registry
from app.tools.schemas import (
    ActionProposal,
    ActionSource,
    AuthorizationDecision,
    AuthorizationStatus,
    DenialReason,
    ExecutionMode,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
    ToolResult,
    most_restrictive,
)

__all__ = [
    "ActionProposal",
    "ActionSource",
    "ArgumentValidationError",
    "AuthorizationDecision",
    "AuthorizationService",
    "AuthorizationStatus",
    "DenialReason",
    "DuplicateToolError",
    "EchoTool",
    "ExecutionMode",
    "RiskLevel",
    "Tool",
    "ToolArguments",
    "ToolCategory",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "build_catalog",
    "evaluate",
    "get_registry",
    "most_restrictive",
]
