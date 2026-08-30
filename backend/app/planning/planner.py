"""Plan generation: one bounded model call, then two layers of validation.

The untrusted boundary of Stage 4B. It asks a model for a plan, parses the
answer, and validates it -- first for shape, then for graph soundness. Only a
proposal that survives both becomes a `Plan`.

**Exactly one model call, or zero.** No retry: the provider already retries
transport failures with its own bound, and a second attempt at a *semantic*
failure -- a cycle, a missing dependency -- re-rolls the same dice at double
the cost. A proposal that cannot be validated becomes a failure, not another
call. There is no replanning loop anywhere in this stage.

**No database. No recursion. No execution.** The planner returns a value, and
has nothing to act with.
"""

import json
import re
from typing import Optional

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.llm.base import LLMMessage, LLMProvider
from app.planning import limits
from app.planning.prompts import (
    PLANNING_SYSTEM_PROMPT,
    build_planning_user_prompt,
)
from app.planning.schemas import Goal, Plan, PlanProposal
from app.planning.validator import PlanValidationError, build_plan

logger = get_logger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class PlanGenerationError(Exception):
    """Raised internally with a reason code. Never escapes the service."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class Planner:
    """Produces a validated `Plan`, or raises with a reason."""

    def __init__(
        self, provider: LLMProvider, settings: Optional[Settings] = None
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def plan(self, message: str, goal: Goal) -> Plan:
        """Generate and validate one plan for a goal.

        Raises `PlanGenerationError` with a reason code the service turns into
        a failed result. Reasons are application constants, never model text.
        """
        messages = [
            LLMMessage(role="system", content=PLANNING_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_planning_user_prompt(
                    message=message[: limits.MAX_PLANNED_MESSAGE_CHARS],
                    goal_hint=goal.summary,
                ),
            ),
        ]

        try:
            response = await self._provider.generate_response(
                messages,
                # Low, not zero. Planning benefits from a little variation in
                # how work is decomposed, where classification does not -- but
                # a plan should still be broadly reproducible for the same
                # goal, so this stays well below conversational temperature.
                temperature=self._settings.PLANNING_TEMPERATURE,
                max_tokens=self._settings.PLANNING_MAX_TOKENS,
                json_mode=True,
            )
        except LLMError as exc:
            logger.warning(
                "Plan generation failed at the model call",
                extra={"error_code": exc.code, "provider": self._provider.name},
            )
            raise PlanGenerationError("provider_error") from exc
        except Exception as exc:  # noqa: BLE001 - must never escape as itself
            logger.error(
                "Unexpected error during plan generation",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            raise PlanGenerationError("provider_error") from exc

        proposal = self._parse(response.content)

        # Layer 2. A proposal with a perfect shape can still describe an
        # impossible graph, so this is where most real rejections happen.
        try:
            return build_plan(proposal, goal)
        except PlanValidationError as exc:
            logger.warning(
                "Proposed plan failed graph validation",
                extra={"reason": exc.reason, "detail": exc.detail},
            )
            raise PlanGenerationError(exc.reason, exc.detail) from exc

    # --- Parsing / layer 1 --------------------------------------------------

    def _parse(self, raw: str) -> PlanProposal:
        payload = self._load_json(raw)
        if payload is None:
            raise PlanGenerationError("unparsable_response")

        try:
            return PlanProposal.model_validate(payload)
        except ValidationError as exc:
            # No salvage. Dropping the invalid tasks from a plan would leave a
            # graph missing the very steps its dependencies point at, and a
            # plan with holes is more dangerous than no plan: it looks complete.
            logger.warning(
                "Proposed plan failed schema validation",
                extra={
                    "error_count": exc.error_count(),
                    # Field paths only. Values are model output.
                    "invalid_fields": sorted(
                        {
                            ".".join(str(part) for part in error["loc"][:2])
                            for error in exc.errors()
                            if error.get("loc")
                        }
                    )[:8],
                },
            )
            raise PlanGenerationError("schema_validation_failed") from exc

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        if not raw or not raw.strip():
            logger.warning("Plan generation returned an empty response")
            return None

        text = raw.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                logger.warning("Plan generation returned no parsable JSON")
                return None
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("Plan generation returned malformed JSON")
                return None

        if not isinstance(payload, dict):
            logger.warning("Plan generation returned a non-object payload")
            return None
        return payload


__all__ = ["PlanGenerationError", "Planner"]
