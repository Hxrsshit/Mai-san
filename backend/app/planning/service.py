"""Planning orchestration.

    IntentResult -> policy.decide -> Planner -> PlanProposal -> validator -> Plan

**Never raises.** Every failure becomes a `PlanningResult` with no plan. A chat
turn must not fail because Mai could not draw up a plan.

**Never writes.** This module imports nothing from `app.memory`,
`app.entities`, `app.relationships` or `app.knowledge`, and holds no session.
In particular it never writes a plan's *assumptions* into the memory system:
an assumption promoted to a memory becomes a fact about the user that the user
never stated, and would then be retrieved for years as though they had.

**Never executes.** Stage 4B has no executor. A plan whose first task is "Send
the outreach email" is a sentence about future work.
"""

import time
from typing import Optional

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intent.schemas import IntentResult
from app.llm.base import LLMProvider
from app.planning import policy
from app.planning.planner import PlanGenerationError, Planner
from app.planning.schemas import Goal, PlanningResult, PlanStatus

logger = get_logger(__name__)


class PlanningService:
    def __init__(
        self,
        provider: Optional[LLMProvider] = None,
        settings: Optional[Settings] = None,
        planner: Optional[Planner] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider = provider
        self._planner = planner or (
            Planner(provider, self._settings) if provider is not None else None
        )

    async def plan_for(
        self, message: str, intent: Optional[IntentResult]
    ) -> PlanningResult:
        """Plan for one message, if it warrants one. Never raises.

        Makes at most one model call, and only after the deterministic
        eligibility check has already said yes. An ineligible message costs
        nothing -- which is what keeps ordinary conversation as cheap after
        Stage 4B as it was before.
        """
        started = time.perf_counter()

        eligible, status, reason = policy.decide(
            intent=intent,
            message=message,
            enabled=self._settings.PLANNING_ENABLED,
        )

        if not eligible:
            return PlanningResult(
                status=status,
                reason=reason,
                clarification_needed=(
                    policy.CLARIFICATION_PROMPT
                    if status is PlanStatus.NEEDS_CLARIFICATION
                    else None
                ),
                duration_ms=self._elapsed(started),
            )

        if self._planner is None:
            return PlanningResult(
                status=PlanStatus.FAILED,
                reason="planner_unavailable",
                duration_ms=self._elapsed(started),
            )

        goal = Goal(
            # The goal the classifier read, falling back to the message. Both
            # are the user's own words; neither is invented here.
            summary=(intent.goal or message.strip())[:300],
            desired_outcome=intent.requested_outcome,
            source_intent=intent.intent_type,
        )

        try:
            plan = await self._planner.plan(message, goal)
        except PlanGenerationError as exc:
            logger.info(
                "Planning did not produce a valid plan",
                extra={"reason": exc.reason, "intent": intent.intent_type.value},
            )
            return PlanningResult(
                status=PlanStatus.FAILED,
                reason=exc.reason,
                model_calls=1,
                duration_ms=self._elapsed(started),
            )
        except Exception as exc:  # noqa: BLE001 - chat must not break on this
            logger.error(
                "Planning failed unexpectedly",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return PlanningResult(
                status=PlanStatus.FAILED,
                reason="unexpected_error",
                duration_ms=self._elapsed(started),
            )

        result = PlanningResult(
            status=PlanStatus.READY,
            plan=plan,
            model_calls=1,
            duration_ms=self._elapsed(started),
        )

        logger.info(
            "Plan produced",
            extra={
                # Counts and shape only. Task titles are the user's goal in
                # the model's words and stay out of the logs.
                "intent": intent.intent_type.value,
                "tasks": plan.task_count,
                "dependencies": plan.dependency_count,
                "assumptions": len(plan.assumptions),
                "risks": len(plan.risks),
                "max_depth": max((task.depth for task in plan.tasks), default=0),
                "model_calls": result.model_calls,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    @staticmethod
    def _elapsed(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)


__all__ = ["PlanningService"]
