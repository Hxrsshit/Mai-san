"""Action orchestration.

    intent -> eligibility -> matching -> proposal -> Stage 4C authorization
                                                  -> OrchestrationResult

**Orchestrates; does not authorize.** Every candidate becomes an
`ActionProposal` and goes through `AuthorizationService`. Stage 4C's policy is
not reimplemented, consulted conditionally, or short-circuited -- there is no
branch in this module that skips it, and a structural test asserts the count
of proposals equals the count of decisions.

**Zero model calls.** Identification is a phrase lookup. An orchestration pass
costs a set membership test, a regex scan and up to five dictionary lookups.

**Never raises.** Every failure becomes a result with no proposals.

**Never executes, and cannot claim it did.** There is no executor and no field
in which a completion could be recorded. `OrchestrationResult.acted` is a
property that returns False, so it cannot be set by construction,
deserialisation or a later refactor.
"""

import time
from typing import Dict, List, Optional, Tuple

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intent.schemas import IntentResult
from app.orchestration import eligibility, matching
from app.orchestration.schemas import (
    MAX_PROPOSALS,
    STATUS_TO_OUTCOME,
    ActionCandidate,
    ActionOutcome,
    IneligibilityReason,
    OrchestrationResult,
    ProposalOutcome,
    outcome_rank,
)
from app.tools.authorization import AuthorizationService
from app.tools.schemas import ActionProposal, ActionSource

logger = get_logger(__name__)


class OrchestrationService:
    def __init__(
        self,
        authorization: Optional[AuthorizationService] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._authorization = authorization or AuthorizationService()

    def orchestrate(
        self,
        message: str,
        intent: Optional[IntentResult],
        source: ActionSource = ActionSource.USER,
    ) -> OrchestrationResult:
        """Run one orchestration pass. Never raises.

        `source` is recorded and reported. It grants nothing: an identical
        candidate from a user, a model, a plan or the system receives the same
        decision, because the decision is made from registry metadata and
        policy alone.
        """
        started = time.perf_counter()

        eligible, reason = eligibility.decide(
            intent=intent,
            message=message,
            enabled=self._settings.ORCHESTRATION_ENABLED,
        )
        if not eligible:
            # The gate. An ineligible turn does no matching, builds no
            # proposal and makes no authorization call.
            return OrchestrationResult(
                outcome=ActionOutcome.NOT_ELIGIBLE,
                reason=reason,
                duration_ms=self._elapsed(started),
            )

        try:
            candidates = matching.find_candidates(message)
        except Exception as exc:  # noqa: BLE001 - a chat turn must not fail
            logger.error(
                "Action identification failed", extra={"error": str(exc)}, exc_info=exc
            )
            return OrchestrationResult(
                outcome=ActionOutcome.NO_ACTION,
                reason=IneligibilityReason.NOTHING_MATCHED,
                duration_ms=self._elapsed(started),
            )

        if not candidates:
            return OrchestrationResult(
                outcome=ActionOutcome.NO_ACTION,
                reason=IneligibilityReason.NOTHING_MATCHED,
                duration_ms=self._elapsed(started),
            )

        kept, duplicates = _deduplicate(candidates)
        discarded = max(0, len(kept) - MAX_PROPOSALS)
        kept = kept[:MAX_PROPOSALS]

        proposals: List[ProposalOutcome] = []
        for candidate in kept:
            proposals.append(self._authorize(candidate, intent, source))

        # The overall outcome is the most restrictive across every proposal,
        # so one refused action is never hidden behind another that passed.
        outcome = max(
            (proposal.outcome for proposal in proposals), key=outcome_rank
        )

        result = OrchestrationResult(
            outcome=outcome,
            proposals=proposals,
            candidates_discarded=discarded,
            duplicates_removed=duplicates,
            duration_ms=self._elapsed(started),
        )

        logger.info(
            "Action orchestration completed",
            extra={
                # Tool names, statuses and counts. Argument values and message
                # text are user data and stay out.
                "outcome": outcome.value,
                "proposals": len(proposals),
                "tools": [proposal.tool_name for proposal in proposals],
                "statuses": [proposal.status.value for proposal in proposals],
                "duplicates_removed": duplicates,
                "candidates_discarded": discarded,
                # The Stage 4D guarantees, recorded on every pass.
                "model_calls": 0,
                "executed": False,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    # --- Authorization ------------------------------------------------------

    def _authorize(
        self,
        candidate: ActionCandidate,
        intent: Optional[IntentResult],
        source: ActionSource,
    ) -> ProposalOutcome:
        """Build a proposal and put it through Stage 4C.

        Every candidate reaches this method, and this method always calls
        `authorize`. There is no path that produces a `ProposalOutcome`
        without a decision behind it.
        """
        proposal = ActionProposal(
            tool_name=candidate.tool_name,
            arguments=dict(candidate.arguments),
            source=source,
        )
        decision = self._authorization.authorize(proposal, intent=intent)

        return ProposalOutcome(
            tool_name=decision.tool_name,
            source=source,
            outcome=STATUS_TO_OUTCOME[decision.status],
            status=decision.status,
            reason=decision.reason,
            category=decision.category,
            risk_level=decision.risk_level,
            requires_approval=decision.requires_approval,
            matched_phrase=candidate.matched_phrase,
        )

    @staticmethod
    def _elapsed(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 3)


def _deduplicate(
    candidates: List[ActionCandidate],
) -> Tuple[List[ActionCandidate], int]:
    """Drop exact repeats, preserving order.

    Exact only: same canonical tool name *and* identical arguments. Two
    proposals for the same tool with different arguments are two different
    actions and both survive.

    No fuzzy merging -- collapsing near-identical proposals would mean one
    authorization decision standing in for an action nobody authorized.
    """
    seen = set()
    kept: List[ActionCandidate] = []
    removed = 0

    for candidate in candidates:
        key = (
            candidate.tool_name,
            tuple(sorted((str(k), str(v)) for k, v in candidate.arguments.items())),
        )
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(candidate)

    return kept, removed


__all__ = ["OrchestrationService"]
