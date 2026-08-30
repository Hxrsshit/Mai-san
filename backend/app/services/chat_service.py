"""Chat orchestration.

One user turn, end to end:

    load conversation (404 first)
      -> Stage 3A assembles context   (recent conversation + Stage 2D retrieval)
      -> Stage 3B formats the prompt  (the only knowledge-to-prompt path)
      -> store the user message
      -> ONE synchronous LLM generation call
      -> store the assistant reply

This service **orchestrates and does not format**. It never appends memories,
entities, relationships or system text to a message list; every message the
provider sees is produced by `PromptFormatter`. Stage 2D's inline rendering,
which used to be spliced in here as a second system message, was removed in
Stage 3B -- `RetrievalService.render` no longer has a caller on the request
path, and the fallback below deliberately does not resurrect it.

Exactly one synchronous model call happens per turn: the response generation
at the end. Retrieval, assembly and formatting are all deterministic and add
none. Post-turn memory, entity and relationship extraction still run in the
background, on their own session, after the response has been returned.
"""

import time
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.assembler import to_recent_messages
from app.context.schemas import ContextPackage, RecentMessage
from app.context.service import ContextService
from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger
from app.database.models import Conversation, Message, MessageRole
from app.intent.schemas import IntentResult
from app.intent.service import IntentService
from app.planning.schemas import PlanningResult
from app.planning.service import PlanningService
from app.llm.base import LLMProvider
from app.prompt.formatter import PromptFormatter
from app.prompt.schemas import FormattedPrompt
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        settings: Optional[Settings] = None,
        conversation_service: Optional[ConversationService] = None,
        context_service: Optional[ContextService] = None,
        prompt_formatter: Optional[PromptFormatter] = None,
        intent_service: Optional[IntentService] = None,
        planning_service: Optional[PlanningService] = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._settings = settings or get_settings()
        self._conversations = conversation_service or ConversationService(session)
        self._context = context_service or ContextService(session, self._settings)
        self._formatter = prompt_formatter or PromptFormatter(
            self._settings.MAI_SYSTEM_PROMPT
        )
        self._intent = intent_service or IntentService(
            session=session, provider=provider, settings=self._settings
        )
        self._planning = planning_service or PlanningService(
            provider=provider, settings=self._settings
        )

    async def send_message(
        self, conversation_id: uuid.UUID, content: str
    ) -> Tuple[Message, Message, IntentResult, PlanningResult]:
        """Handle one user turn.

        Returns (user_message, assistant_message, intent, planning).

        Raises ConversationNotFoundError if the conversation does not exist,
        or an LLMError subclass if the model call fails. Nothing between those
        two points can fail the turn: retrieval, assembly and formatting all
        degrade to a smaller prompt rather than raising.
        """
        # 404 before writing anything.
        conversation = await self._conversations.get_conversation(conversation_id)

        # Context is assembled *before* the user message is persisted, so the
        # recent conversation Stage 3A selects is genuine history. Assembling
        # afterwards would put the current message into the prompt twice and
        # charge it to the budget twice.
        started = time.perf_counter()

        # Stage 4A. Runs before the message is stored, for the same reason
        # context assembly does: the classifier's disambiguation context
        # should be prior turns, not the message being classified.
        #
        # The result is application state. It is returned to the caller and
        # NEVER passed to the formatter, so no label the model produces can
        # influence the prompt the model is then given.
        intent = await self._intent.understand(content, conversation_id)

        # Stage 4B. Consumes the intent above -- the message is never
        # classified twice. Whether to plan is decided deterministically
        # from that intent, so an ordinary message makes no planning call
        # at all and costs exactly what it did before Stage 4B.
        #
        # Like intent, the plan is application state: returned to the
        # caller, never handed to the formatter. A plan cannot steer the
        # reply, and the reply is byte-identical with planning on or off.
        planning = await self._planning.plan_for(content, intent)

        prompt, timings = await self._build_prompt(conversation_id, content)
        prepare_ms = round((time.perf_counter() - started) * 1000, 2)

        user_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
        )
        await self._conversations.maybe_autotitle(conversation, content)

        logger.info(
            "Chat turn started",
            extra={
                "conversation_id": str(conversation_id),
                "prompt_messages": prompt.stats.total_messages,
                "conversation_messages": prompt.stats.conversation_messages,
                "memories": prompt.stats.memories_rendered,
                "entities": prompt.stats.entities_rendered,
                "relationships": prompt.stats.relationships_rendered,
                "reference_chars": prompt.stats.reference_chars,
                "prompt_chars": prompt.stats.total_chars,
                "fallback_prompt": prompt.stats.fallback_used,
                "intent_type": intent.intent_type.value,
                "planning_status": planning.status.value,
                "plan_tasks": planning.plan.task_count if planning.plan else 0,
                # The Stage 3B guarantee, recorded on every turn: exactly
                # one response *generation* call. Stage 4A adds at most one
                # structured classification call, counted separately so the
                # two bounds stay independently checkable.
                "request_path_generation_calls": 1,
                "request_path_classification_calls": intent.model_calls,
                "request_path_planning_calls": planning.model_calls,
                # Retrieval and assembly time is reported in more detail by
                # their own log lines; these are the totals as chat sees them.
                "assembly_ms": timings.get("assembly_ms"),
                "format_ms": timings.get("format_ms"),
                "pre_llm_ms": prepare_ms,
            },
        )

        try:
            llm_response = await self._provider.generate_response(prompt.messages)
        except LLMError:
            # The user message is already persisted. The session is rolled back
            # by the request dependency, so the failed turn leaves no partial
            # state behind and the client can safely retry.
            logger.error(
                "Chat turn failed at the model call",
                extra={"conversation_id": str(conversation_id)},
            )
            raise

        assistant_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=llm_response.content,
        )
        await self._conversations.touch_conversation(conversation)

        logger.info(
            "Chat turn completed",
            extra={
                "conversation_id": str(conversation_id),
                "assistant_message_id": str(assistant_message.id),
                "total_tokens": llm_response.usage.get("total_tokens"),
            },
        )
        return user_message, assistant_message, intent, planning

    async def start_conversation_with_message(
        self, content: str, title: Optional[str] = None
    ) -> Tuple[Conversation, Message, Message, IntentResult, PlanningResult]:
        """Convenience path: create a conversation and send its first message."""
        conversation = await self._conversations.create_conversation(title=title)
        user_message, assistant_message, intent, planning = await self.send_message(
            conversation.id, content
        )
        return conversation, user_message, assistant_message, intent, planning

    # --- Internals ----------------------------------------------------------
    # Orchestration only. Everything below chooses *which* inputs reach the
    # formatter; none of it decides how a message is worded or ordered.

    async def _build_prompt(
        self, conversation_id: uuid.UUID, content: str
    ) -> Tuple[FormattedPrompt, Dict[str, float]]:
        """Assemble, then format. Never raises.

        Returns the prompt and per-stage timings, so a slow turn can be
        attributed to retrieval, assembly or formatting without guessing.

        Degradation is layered, and no layer reintroduces long-term knowledge
        by another route:

        - assembly failed          -> fallback on freshly loaded conversation
        - formatting failed        -> fallback on the conversation already
                                      assembled, without any knowledge
        - everything failed        -> the current message alone

        `PromptFormatter.fallback` is itself total, so the last line always
        produces something to send.
        """
        timings: Dict[str, float] = {}

        started = time.perf_counter()
        package = await self._assemble(conversation_id, content)
        # Retrieval runs inside assembly and logs its own duration; this is the
        # combined cost of Stage 2D plus Stage 3A as the request path sees it.
        timings["assembly_ms"] = round((time.perf_counter() - started) * 1000, 2)

        started = time.perf_counter()
        try:
            if package is not None:
                try:
                    return self._formatter.format(package), timings
                except Exception as exc:  # noqa: BLE001 - chat must still answer
                    logger.error(
                        "Prompt formatting failed; falling back to a minimal prompt",
                        extra={
                            "conversation_id": str(conversation_id),
                            "error": str(exc),
                        },
                        exc_info=exc,
                    )
                    return (
                        self._formatter.fallback(
                            content, self._safe_recent_of(package)
                        ),
                        timings,
                    )

            return (
                self._formatter.fallback(
                    content, await self._load_recent(conversation_id)
                ),
                timings,
            )
        finally:
            timings["format_ms"] = round((time.perf_counter() - started) * 1000, 2)

    async def _assemble(
        self, conversation_id: uuid.UUID, content: str
    ) -> Optional[ContextPackage]:
        """Stage 3A's package, or None if assembly failed outright.

        `ContextService.build` already degrades internally; this guard covers
        the case where it fails before it can degrade.
        """
        try:
            return await self._context.build(
                current_message=content, conversation_id=conversation_id
            )
        except Exception as exc:  # noqa: BLE001 - assembly must never break chat
            logger.error(
                "Context assembly failed; continuing without it",
                extra={"conversation_id": str(conversation_id), "error": str(exc)},
                exc_info=exc,
            )
            return None

    async def _load_recent(
        self, conversation_id: uuid.UUID
    ) -> List[RecentMessage]:
        """Recent conversation for the fallback path, bounded by the same limit."""
        try:
            stored = await self._conversations.get_messages(
                conversation_id,
                limit=self._context.limits.recent_message_limit,
            )
            return to_recent_messages(stored)
        except Exception as exc:  # noqa: BLE001 - the current message is enough
            logger.error(
                "Recent conversation unavailable for the fallback prompt",
                extra={"conversation_id": str(conversation_id), "error": str(exc)},
            )
            return []

    @staticmethod
    def _safe_recent_of(package: ContextPackage) -> Sequence[RecentMessage]:
        try:
            return package.recent_conversation
        except Exception:  # noqa: BLE001 - a broken package must not cascade
            return []
