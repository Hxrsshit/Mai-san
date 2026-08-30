"""Deterministic authorization policy.

The authority boundary of Stage 4C. Every input is typed data the application
produced or validated; nothing here reads model output, queries a database,
calls a model, or touches the network.

**Monotonic by construction.** Each rule returns a status, and the outcome is
the *most restrictive* of them (`most_restrictive` takes a maximum over an
explicit ordering). A rule can therefore only ever tighten a decision. There
is no ordering of rules, no combination of inputs and no future added rule
that can make a proposal more permissible -- which is the property the
specification calls the monotonic safety rule, expressed as arithmetic rather
than as care.

Three facts decide everything, and all three come from application-controlled
sources:

    registry metadata  +  application policy  +  Stage 4A capability

**`ALLOWED` means "this layer does not forbid it".** It does not mean
"execute", and it is not a route to execution: nothing in Stage 4C can run a
tool, because no tool defines a way to be run. That guarantee is structural --
see `base.py` -- and deliberately *not* expressed as a policy rule here.
Folding it in would make `ALLOWED` unreachable and collapse four meaningful
states into three, hiding the difference between "policy permits this" and
"nothing can do this yet".
"""

from typing import FrozenSet, List, Optional, Tuple

from app.intent.schemas import IntentResult
from app.tools.schemas import (
    AuthorizationStatus,
    DenialReason,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
    most_restrictive,
    risk_rank,
)

#: Risk at or above this always requires human approval, whatever the tool's
#: own `requires_approval` says. A declaration can raise the bar for itself;
#: it cannot lower this one.
APPROVAL_REQUIRED_AT_OR_ABOVE: RiskLevel = RiskLevel.HIGH

#: Risk above this is refused outright in Stage 4C.
#:
#: CRITICAL means "irreversible or unbounded damage". Gating it behind an
#: approval prompt would be a claim that a prompt is sufficient protection,
#: and the architecture that would make that true -- an audited approval
#: record, a revocable grant, a bounded executor -- is Stage 4E's, not this
#: stage's. Until then the honest answer is no.
MAX_PERMITTED_RISK: RiskLevel = RiskLevel.HIGH

#: Categories no tool may be authorised in yet. Empty today: the catalogue
#: contains no real capability, so there is nothing to forbid by category.
#: The hook exists so a future capability can be registered and studied
#: before it is permitted.
FORBIDDEN_CATEGORIES: FrozenSet[ToolCategory] = frozenset()


def evaluate(
    definition: Optional[ToolDefinition],
    tool_name: str,
    intent: Optional[IntentResult] = None,
) -> Tuple[AuthorizationStatus, str]:
    """Decide the status for one proposal. Pure, total, deterministic.

    Returns `(status, reason)`. The reason is an application constant, so a
    decision never echoes model output or user text back to a caller.
    """
    if not tool_name.strip():
        return AuthorizationStatus.UNKNOWN_TOOL, DenialReason.EMPTY_NAME

    if definition is None:
        # Unknown means unavailable. Nothing is imported, searched for,
        # auto-registered, or interpreted as a command.
        return AuthorizationStatus.UNKNOWN_TOOL, DenialReason.UNKNOWN_TOOL

    # Every rule contributes a status; the tightest one wins.
    verdicts: List[Tuple[AuthorizationStatus, str]] = [
        _enabled_rule(definition),
        _category_rule(definition),
        _risk_ceiling_rule(definition),
        _risk_approval_rule(definition),
        _declared_approval_rule(definition),
        _intent_rule(intent),
    ]

    status = most_restrictive(*(verdict for verdict, _ in verdicts))

    # Report the reason belonging to the rule that produced the outcome. When
    # several tie, the first in declaration order is used, so the message is
    # stable for a given input.
    reason = next(
        reason for verdict, reason in verdicts if verdict is status
    )
    return status, reason


# --- Rules ------------------------------------------------------------------
# Each returns the least restrictive status it is willing to permit. Returning
# ALLOWED means "this rule has no objection", never "this is permitted".


def _enabled_rule(definition: ToolDefinition) -> Tuple[AuthorizationStatus, str]:
    if not definition.enabled:
        return AuthorizationStatus.FORBIDDEN, DenialReason.TOOL_DISABLED
    return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED


def _category_rule(definition: ToolDefinition) -> Tuple[AuthorizationStatus, str]:
    if definition.category in FORBIDDEN_CATEGORIES:
        return AuthorizationStatus.FORBIDDEN, DenialReason.CATEGORY_FORBIDDEN
    return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED


def _risk_ceiling_rule(
    definition: ToolDefinition,
) -> Tuple[AuthorizationStatus, str]:
    if risk_rank(definition.risk_level) > risk_rank(MAX_PERMITTED_RISK):
        return AuthorizationStatus.FORBIDDEN, DenialReason.RISK_FORBIDDEN
    return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED


def _risk_approval_rule(
    definition: ToolDefinition,
) -> Tuple[AuthorizationStatus, str]:
    if risk_rank(definition.risk_level) >= risk_rank(APPROVAL_REQUIRED_AT_OR_ABOVE):
        return AuthorizationStatus.APPROVAL_REQUIRED, DenialReason.HIGH_RISK_APPROVAL
    return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED


def _declared_approval_rule(
    definition: ToolDefinition,
) -> Tuple[AuthorizationStatus, str]:
    if definition.requires_approval:
        return AuthorizationStatus.APPROVAL_REQUIRED, DenialReason.APPROVAL_REQUIRED
    return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED


def _intent_rule(intent: Optional[IntentResult]) -> Tuple[AuthorizationStatus, str]:
    """Stage 4A remains authoritative. Where the two overlap, the tighter wins.

    Stage 4A clamps `requires_execution` to false for conversational intents,
    so a turn read as a question carries no execution capability -- and a tool
    proposal arriving inside one is refused regardless of what the tool is.
    That closes the specification's example attack directly: a user asks a
    question, the model answers with "use tool future_delete_file", and the
    proposal is forbidden because the *turn* had no such capability, not
    because the tool did.

    No intent supplied means no intent-level restriction to apply. It never
    means permission: the other six rules still run, and the execution-
    capability backstop still caps the outcome at APPROVAL_REQUIRED.
    """
    if intent is None:
        return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED
    if not intent.requires_execution:
        return AuthorizationStatus.FORBIDDEN, DenialReason.INTENT_FORBIDS
    return AuthorizationStatus.ALLOWED, DenialReason.ALLOWED


__all__ = [
    "APPROVAL_REQUIRED_AT_OR_ABOVE",
    "FORBIDDEN_CATEGORIES",
    "MAX_PERMITTED_RISK",
    "evaluate",
]
