"""Intent understanding orchestration.

    message -> IntentClassifier (one bounded model call)
            -> IntentClassification (validated, untrusted)
            -> policy.derive (deterministic, authoritative)
            -> IntentResult

**Never raises.** Every failure becomes an UNKNOWN result with no capability
flags set. A chat turn must not fail because Mai could not label it.

**Never writes.** This module imports nothing from `app.memory`,
`app.entities`, `app.relationships` or `app.knowledge`, and holds no session
it could write through. Reading recent conversation is the only database
access, it is bounded, and it is read-only.

**Never executes.** Stage 4A has no executor. `IntentResult` is a frozen value
object; producing one with `requires_execution=True` records that a future
stage would need to arrange execution, and arranges nothing.
"""

import time
import uuid
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intent import policy
from app.intent.classifier import IntentClassificationError, IntentClassifier
from app.intent.schemas import IntentResult, IntentType
from app.llm.base import LLMProvider
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)

#: Returned when classification is switched off. Distinct from a failure: no
#: call was attempted, so nothing degraded.
DISABLED_REASON = "classification_disabled"


class IntentService:
    def __init__(
        self,
        session: Optional[AsyncSession] = None,
        provider: Optional[LLMProvider] = None,
        settings: Optional[Settings] = None,
        conversation_service: Optional[ConversationService] = None,
        classifier: Optional[IntentClassifier] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._provider = provider
        self._conversations = conversation_service or (
            ConversationService(session) if session is not None else None
        )
        self._classifier = classifier or (
            IntentClassifier(provider, self._settings) if provider is not None else None
        )

    async def understand(
        self,
        message: str,
        conversation_id: Optional[uuid.UUID] = None,
    ) -> IntentResult:
        """Classify one user message. Never raises.

        Makes at most one model call. There is no retry, no second pass and no
        recursion: `policy.fallback` is a pure function, so the failure path
        cannot re-enter the model.
        """
        started = time.perf_counter()

        if not self._settings.INTENT_CLASSIFICATION_ENABLED:
            return policy.fallback(DISABLED_REASON)

        if self._classifier is None:
            return policy.fallback("classifier_unavailable")

        if not message or not message.strip():
            return policy.fallback("empty_message", self._elapsed(started))

        context = await self._safe_recent(conversation_id)

        try:
            classification = await self._classifier.classify(
                message, recent_context=context
            )
        except IntentClassificationError as exc:
            # `exc.reason` is one of a fixed set of application constants, so
            # no model output or user text reaches the result.
            return policy.fallback(exc.reason, self._elapsed(started), model_calls=1)
        except Exception as exc:  # noqa: BLE001 - chat must not break on this
            logger.error(
                "Intent understanding failed unexpectedly",
                extra={"error": str(exc)},
                exc_info=exc,
            )
            return policy.fallback("unexpected_error", self._elapsed(started))

        result = policy.derive(classification).model_copy(
            update={"duration_ms": self._elapsed(started)}
        )

        logger.info(
            "Intent classified",
            extra={
                "conversation_id": str(conversation_id) if conversation_id else None,
                "intent_type": result.intent_type.value,
                "confidence": round(result.confidence, 2),
                "ambiguity": result.ambiguity.value,
                "secondary_intents": len(result.secondary_intents),
                "requires_planning": result.requires_planning,
                "requires_research": result.requires_research,
                "requires_execution": result.requires_execution,
                "requires_user_approval": result.requires_user_approval,
                # The Stage 4A bound, recorded on every turn.
                "model_calls": result.model_calls,
                "duration_ms": result.duration_ms,
            },
        )
        return result

    # --- Context ------------------------------------------------------------

    async def _safe_recent(
        self, conversation_id: Optional[uuid.UUID]
    ) -> List[str]:
        """A few recent turns, for disambiguating a short follow-up.

        Deliberately *not* Stage 2D retrieval. Long-term knowledge is not
        needed to tell a question from an action, and sending it here would
        create a second context system, widen what reaches the provider, and
        duplicate a pipeline that already has one owner.
        """
        if conversation_id is None or self._conversations is None:
            return []
        try:
            messages = await self._conversations.get_messages(
                conversation_id, limit=self._settings.INTENT_CONTEXT_MESSAGES
            )
        except Exception as exc:  # noqa: BLE001 - context is optional
            logger.warning(
                "Recent conversation unavailable for intent classification",
                extra={"error": str(exc)},
            )
            return []

        return [
            f"{getattr(message.role, 'value', message.role)}: {message.content}"
            for message in messages
        ]

    @staticmethod
    def _elapsed(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)


__all__ = ["DISABLED_REASON", "IntentService"]
