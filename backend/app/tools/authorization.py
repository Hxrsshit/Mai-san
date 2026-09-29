"""Authorization orchestration.

    ActionProposal -> registry lookup -> policy.evaluate -> argument validation
                   -> AuthorizationDecision

**No model call. No database. No network.** Authorization is a dictionary
lookup and a handful of comparisons over typed data, so it is deterministic,
local, and costs the same every time.

**No execution, and no path to it.** This service returns a decision. It holds
no executor, and there is nothing to hold: no tool defines a way to be run.
A decision of `ALLOWED` means policy does not forbid the action -- it is not a
handle, a token, or a permission that anything can spend.
"""

import time
from typing import Optional

from app.core.logging import get_logger
from app.intent.schemas import IntentResult
from app.tools import policy
from app.tools.base import ArgumentValidationError
from app.tools.registry import ToolRegistry, get_registry
from app.tools.schemas import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationStatus,
    DenialReason,
    most_restrictive,
    risk_rank,
)

logger = get_logger(__name__)


class AuthorizationService:
    """Decides whether a proposed action is unknown, forbidden, gated or permitted."""

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self._registry = registry if registry is not None else get_registry()

    def authorize(
        self,
        proposal: ActionProposal,
        intent: Optional[IntentResult] = None,
    ) -> AuthorizationDecision:
        """Evaluate one proposal. Never raises; fails closed.

        `intent` is the Stage 4A result for the turn, when there is one. Where
        the two policies overlap the more restrictive wins -- see
        `policy._intent_rule`.
        """
        started = time.perf_counter()
        name = self._registry.canonical(proposal.tool_name)
        definition = self._registry.definition(name)

        status, reason = policy.evaluate(definition, name, intent)

        validated = None
        if definition is not None and not _is_refused(status):
            # Arguments are checked only once the tool is known and not
            # refused: validating against an unknown tool's schema is
            # meaningless, and a refusal should not depend on argument shape.
            tool = self._registry.get(name)
            try:
                validated = tool.validate_arguments(dict(proposal.arguments))
            except ArgumentValidationError as exc:
                # Bad arguments tighten the outcome; they can never loosen it.
                status = most_restrictive(status, AuthorizationStatus.FORBIDDEN)
                reason = DenialReason.INVALID_ARGUMENTS
                validated = None
                logger.info(
                    "Tool arguments failed validation",
                    extra={"tool": name, "invalid_fields": exc.fields},
                )

        decision = AuthorizationDecision(
            status=status,
            reason=reason,
            tool_name=name,
            category=definition.category if definition else None,
            risk_level=definition.risk_level if definition else None,
            # Never lowered by anything. A permitted action is one that does
            # not require approval; every other outcome requires it.
            requires_approval=status is not AuthorizationStatus.ALLOWED,
            validated_arguments=validated,
        )

        logger.info(
            "Action authorization decided",
            extra={
                # Names, statuses and counts. Argument *values* are model
                # output and may contain anything, so they stay out.
                "tool": name,
                "source": proposal.source.value,
                "status": decision.status.value,
                "reason": decision.reason,
                "risk_level": decision.risk_level.value if decision.risk_level else None,
                "requires_approval": decision.requires_approval,
                "argument_count": len(proposal.arguments),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
        return decision


    async def authorize_with_grants(
        self,
        proposal: ActionProposal,
        grants,
        intent: Optional[IntentResult] = None,
        now=None,
    ) -> AuthorizationDecision:
        """The same decision, with a standing grant allowed to supply the
        human approval it would otherwise have needed.

        Stage 6E's only addition to the authorization path, and deliberately
        a thin one. It calls `authorize` first and unchanged, so the policy
        answer is identical whether or not grants exist -- then, and only
        then, asks whether a live grant covers the approval requirement.

        A grant can do exactly one thing: turn `requires_approval` from True
        to False. It cannot change the status, cannot make a forbidden action
        permitted, cannot make an unknown capability known, and cannot
        validate arguments that failed. `most_restrictive` is not consulted
        because there is nothing to combine: this never loosens a *status*.

        `grants` is an `app.authorization.grants.GrantService`. It is passed
        in rather than constructed here so this module keeps no database
        session and no import of the persistence layer -- the caller supplies
        the lookup, this method makes the decision.
        """
        decision = self.authorize(proposal, intent=intent)

        if decision.status is not AuthorizationStatus.APPROVAL_REQUIRED:
            # Nothing to supply. `ALLOWED` needed no approval; every other
            # status is a refusal, and a grant may not overturn one. This is
            # what makes CRITICAL structurally unreachable: `policy` returns
            # FORBIDDEN for it, so the lookup below never runs.
            return decision

        if grants is None:
            return decision

        grant = await grants.active_for(decision.tool_name, now=now)
        if grant is None:
            return decision

        if risk_rank(decision.risk_level) > risk_rank(grant.risk_level):
            # The capability is riskier now than when the grant was given.
            # A grant covers the risk a person actually agreed to, so a
            # capability whose risk was raised since stops being covered
            # without anyone having to remember to revoke it.
            logger.info(
                "Standing grant does not cover the current risk",
                extra={
                    "tool": decision.tool_name,
                    "granted_risk": grant.risk_level.value,
                    "current_risk": (
                        decision.risk_level.value if decision.risk_level else None
                    ),
                },
            )
            return decision

        logger.info(
            "Standing grant satisfied an approval requirement",
            # Ids, a name and a risk. No arguments: they are the caller's
            # payload and may contain anything.
            extra={
                "tool": decision.tool_name,
                "grant_id": str(grant.id),
                "risk": grant.risk_level.value,
            },
        )
        return decision.model_copy(
            update={
                # The status is unchanged and stays truthful: policy does
                # require an approval for this. What changed is that one
                # already exists.
                "requires_approval": False,
                "standing_grant_id": grant.id,
                "reason": DenialReason.STANDING_GRANT,
            }
        )


def _is_refused(status: AuthorizationStatus) -> bool:
    return status in {
        AuthorizationStatus.UNKNOWN_TOOL,
        AuthorizationStatus.FORBIDDEN,
    }


__all__ = ["AuthorizationService"]
