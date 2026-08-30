"""Intent classification: one bounded model call, then strict validation.

The untrusted boundary of Stage 4A. It calls the model through the generic
provider interface, parses whatever comes back, and validates it against a
closed schema. Everything after this point works with typed data.

**Exactly one model call, or zero.** There is no retry loop here: the provider
already retries transport failures internally with its own bound, and a second
classification attempt on a *semantic* failure would just re-roll the same
dice at double the cost. A response that cannot be validated becomes a
fallback, not another call.

**No database.** **No recursion.** **No execution.** The classifier returns a
value; it has nothing to act with.
"""

import json
import re
from typing import List, Optional

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.intent.prompts import (
    CLASSIFICATION_SYSTEM_PROMPT,
    build_classification_user_prompt,
)
from app.intent.schemas import IntentClassification
from app.llm.base import LLMMessage, LLMProvider

logger = get_logger(__name__)

# Models sometimes wrap JSON in a markdown fence despite instructions.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

#: Upper bound on the message text handed to the classifier. Intent is visible
#: in the opening of a request; sending 32,000 characters would cost tokens
#: without improving the label, and would let one message dominate the budget.
MAX_CLASSIFIED_CHARS = 4000

#: How many recent turns may be shown for disambiguation, and how much of each.
MAX_CONTEXT_LINES = 4
MAX_CONTEXT_LINE_CHARS = 300


class IntentClassificationError(Exception):
    """Raised internally with a reason code. Never escapes the service."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IntentClassifier:
    """Produces a validated `IntentClassification`, or raises with a reason."""

    def __init__(
        self, provider: LLMProvider, settings: Optional[Settings] = None
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def classify(
        self, message: str, recent_context: Optional[List[str]] = None
    ) -> IntentClassification:
        """Classify one message.

        Raises `IntentClassificationError` with a reason code the service turns
        into a fallback. Reasons are application constants, never model text.
        """
        if not message or not message.strip():
            raise IntentClassificationError("empty_message")

        messages = [
            LLMMessage(role="system", content=CLASSIFICATION_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_classification_user_prompt(
                    message=message[:MAX_CLASSIFIED_CHARS],
                    recent_context=_trim_context(recent_context),
                ),
            ),
        ]

        try:
            response = await self._provider.generate_response(
                messages,
                # Zero temperature: the same message must classify the same way
                # every time, or the layer is not something a planner can rely
                # on.
                temperature=self._settings.INTENT_CLASSIFICATION_TEMPERATURE,
                max_tokens=self._settings.INTENT_CLASSIFICATION_MAX_TOKENS,
                json_mode=True,
            )
        except LLMError as exc:
            logger.warning(
                "Intent classification failed at the model call",
                extra={"error_code": exc.code, "provider": self._provider.name},
            )
            raise IntentClassificationError("provider_error") from exc
        except Exception as exc:  # noqa: BLE001 - must never escape as itself
            logger.error(
                "Unexpected error during intent classification",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            raise IntentClassificationError("provider_error") from exc

        return self._parse(response.content)

    # --- Parsing / validation ----------------------------------------------

    def _parse(self, raw: str) -> IntentClassification:
        payload = self._load_json(raw)
        if payload is None:
            raise IntentClassificationError("unparsable_response")

        try:
            return IntentClassification.model_validate(payload)
        except ValidationError as exc:
            # No salvage path, deliberately. A memory batch can lose one bad
            # entry and keep the rest; a classification is a single answer, and
            # half of one is not a weaker answer but a different one.
            logger.warning(
                "Intent classification failed validation",
                extra={
                    "error_count": exc.error_count(),
                    # Field names only. The values are model output and may
                    # contain whatever the user wrote.
                    "invalid_fields": sorted(
                        {
                            str(error["loc"][0])
                            for error in exc.errors()
                            if error.get("loc")
                        }
                    ),
                },
            )
            raise IntentClassificationError("schema_validation_failed") from exc

    @staticmethod
    def _load_json(raw: str) -> Optional[dict]:
        """Best-effort JSON extraction from a model response."""
        if not raw or not raw.strip():
            logger.warning("Intent classification returned an empty response")
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
                logger.warning("Intent classification returned no parsable JSON")
                return None
            try:
                payload = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("Intent classification returned malformed JSON")
                return None

        if not isinstance(payload, dict):
            logger.warning("Intent classification returned a non-object payload")
            return None
        return payload


def _trim_context(lines: Optional[List[str]]) -> List[str]:
    """Bound the disambiguation context. Never loads anything itself."""
    if not lines:
        return []
    return [line[:MAX_CONTEXT_LINE_CHARS] for line in lines[-MAX_CONTEXT_LINES:]]


__all__ = [
    "IntentClassificationError",
    "IntentClassifier",
    "MAX_CLASSIFIED_CHARS",
    "MAX_CONTEXT_LINES",
]
