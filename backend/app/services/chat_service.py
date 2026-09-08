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
from app.research.schemas import ResearchResult
from app.research.service import ResearchService
from app.workflows.schemas import WorkflowOutcome, WorkflowResult
from app.workflows.service import WorkflowService
from app.database.models import Conversation, Message, MessageRole
from app.intent.schemas import IntentResult
from app.intent.service import IntentService
from app.orchestration.schemas import OrchestrationResult
from app.orchestration.service import OrchestrationService
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
        orchestration_service: Optional[OrchestrationService] = None,
        research_service: Optional["ResearchService"] = None,
        workflow_service: Optional["WorkflowService"] = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._settings = settings or get_settings()
        self._conversations = conversation_service or ConversationService(session)
        self._context = context_service or ContextService(session, self._settings)
        self._formatter = prompt_formatter or PromptFormatter(
            self._settings.MAI_SYSTEM_PROMPT
        )
        self._workflows = workflow_service or WorkflowService(
            session, settings=settings
        )
        self._research = research_service or ResearchService(
            session, settings=self._settings
        )
        self._intent = intent_service or IntentService(
            session=session, provider=provider, settings=self._settings
        )
        self._planning = planning_service or PlanningService(
            provider=provider, settings=self._settings
        )
        self._orchestration = orchestration_service or OrchestrationService(
            settings=self._settings
        )

    async def send_message(
        self, conversation_id: uuid.UUID, content: str
    ) -> Tuple[
        Message, Message, IntentResult, PlanningResult, OrchestrationResult,
        ResearchResult,
    ]:
        """Handle one user turn.

        Returns (user_message, assistant_message, intent, planning,
        orchestration, research).

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

        # Stage 4D. Consumes the same intent -- the message is classified
        # once. Only an ACTION turn is examined, and identification is a
        # deterministic phrase lookup, so this adds no model call at all.
        #
        # Like intent and planning, the result is application state: it is
        # returned to the caller and never handed to the formatter. It also
        # cannot be acted on -- there is no executor anywhere below it.
        orchestration = self._orchestration.orchestrate(content, intent)

        # Stage 4F-D. The one place a chat turn can cause a side effect, and
        # only for the single read-only tool named in
        # `CHAT_CONFIRMABLE_TOOLS`, and only after the user confirmed the
        # exact query on the previous turn.
        #
        # Adds no model call: identification reuses Stage 4D's phrase table,
        # confirmation is a phrase table, and the proposal text is written in
        # application code. A turn that proposes a search costs *less* than an
        # ordinary turn, because it answers without the model at all.
        # Stage 4F-E. Composite requests -- "research X and write me a
        # summary" -- are recognised first, because the workflow matcher is
        # the more specific of the two: it requires both halves of the
        # request, so anything it matches would otherwise be handled as
        # research alone and lose the second half silently.
        #
        # Adds no model call of its own. Planning is a phrase table, the
        # proposal text is application-written, and the synthesis it needs is
        # the same single generation the turn was already making.
        workflow = await self._workflows.handle(conversation_id, content, intent)

        if workflow.has_reply:
            return await self._answer_without_the_model(
                conversation, conversation_id, content, ResearchResult(),
                intent, planning, orchestration, workflow=workflow,
            )

        # A turn the workflow layer claimed is not also a research turn.
        # Running both would propose the same search twice.
        research = (
            ResearchResult()
            if workflow.outcome is not WorkflowOutcome.NOT_WORKFLOW
            else await self._research.handle(conversation_id, content, intent)
        )

        if research.has_reply:
            # The application is answering. A confirmation prompt, a refusal
            # or a "no provider configured" is application text on purpose:
            # it must be exactly true, and a model asked to phrase it could
            # embellish -- "I'll search now" instead of "may I search?" is a
            # small difference that would matter a great deal.
            return await self._answer_without_the_model(
                conversation, conversation_id, content, research,
                intent, planning, orchestration,
            )

        prompt, timings = await self._build_prompt(
            conversation_id, content, research=research, workflow=workflow
        )
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
                "action_outcome": orchestration.outcome.value,
                "action_proposals": len(orchestration.proposals),
                "research_outcome": research.outcome.value,
                "research_results": research.result_count,
                "research_chars": prompt.stats.research_chars,
                "plan_tasks": planning.plan.task_count if planning.plan else 0,
                # The Stage 3B guarantee, recorded on every turn: exactly
                # one response *generation* call. Stage 4A adds at most one
                # structured classification call, counted separately so the
                # two bounds stay independently checkable.
                "request_path_generation_calls": 1,
                "request_path_classification_calls": intent.model_calls,
                "request_path_planning_calls": planning.model_calls,
                # Stage 4D adds none: identification is deterministic.
                "request_path_orchestration_calls": orchestration.model_calls,
                # Stage 4F-D adds none either.
                "request_path_research_calls": research.model_calls,
                # Stage 4F-D: a confirmed web search is an execution, and it
                # is counted. Every other turn is still zero.
                "actions_executed": 1 if research.succeeded else 0,
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

        reply = llm_response.content

        if workflow.needs_synthesis:
            # The synthesis just generated becomes the artifact's body. This
            # is the only ordering that works: the file's content is the
            # answer, so the file cannot be written before the answer exists.
            finished = await self._workflows.finalise(
                workflow.workflow_id, reply
            )
            # Merged rather than replaced. `finalise` reports the artifact
            # half and knows nothing about the research half, so taking its
            # result wholesale dropped the result count and the block -- the
            # API then reported a successful workflow that had found nothing.
            workflow = finished.model_copy(
                update={
                    "research_block": workflow.research_block,
                    "result_count": workflow.result_count,
                    "steps": workflow.steps + finished.steps,
                }
            )
            # Appended by the application, from the execution record -- never
            # left to the model to claim. A model that says "I've saved this"
            # when the write failed is the exact failure Stage 4E.1 exists to
            # prevent, and it cannot know the outcome in any case: the write
            # happens after it has finished speaking.
            reply = f"{reply}\n\n{self._artifact_note(workflow)}"

        assistant_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=reply,
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
        return (
            user_message, assistant_message, intent, planning, orchestration,
            research, workflow,
        )

    @staticmethod
    def _artifact_note(workflow: WorkflowResult) -> str:
        """One sentence about the file, true by construction.

        Built from `artifact_written`, which the workflow set from the
        execution record's state. There is no branch here that can report a
        file that does not exist.
        """
        if workflow.artifact_written:
            return f"I've saved this to `{workflow.artifact_path}` in my workspace."
        return (
            "I couldn't save this to a file — the write didn't succeed, so "
            "nothing was created."
        )

    async def _answer_without_the_model(
        self, conversation, conversation_id, content, research,
        intent, planning, orchestration, workflow=None,
    ):
        """Persist a turn the application answered itself. No model call.

        Shared by the research and workflow layers. Whichever produced the
        reply, the text is application-written -- see `workflow` below.

        Used for a confirmation prompt, a decline, and the two "cannot search"
        cases. The reply is written in `app/research/service.py`, so it is
        exactly true by construction -- there is no model in the loop to
        rephrase "may I search?" into "I searched".

        Everything else about the turn is normal: the user message is stored,
        the conversation is titled and touched, and the caller receives the
        same shape it always does.
        """
        user_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
        )
        await self._conversations.maybe_autotitle(conversation, content)

        assistant_message = await self._conversations.add_message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            # Whichever layer answered. Both texts are written in
            # application code precisely so they are true by construction.
            content=(workflow.reply if workflow is not None and workflow.has_reply
                     else research.reply),
        )
        await self._conversations.touch_conversation(conversation)

        logger.info(
            "Chat turn answered by the application",
            extra={
                "conversation_id": str(conversation_id),
                "research_outcome": research.outcome.value,
                # The guarantee worth recording: this turn cost nothing.
                "request_path_generation_calls": 0,
                "request_path_research_calls": research.model_calls,
                "actions_executed": 0,
            },
        )

        return (
            user_message, assistant_message, intent, planning, orchestration,
            research, workflow if workflow is not None else WorkflowResult(),
        )

    async def start_conversation_with_message(
        self, content: str, title: Optional[str] = None
    ) -> Tuple[
        Conversation, Message, Message, IntentResult, PlanningResult,
        OrchestrationResult,
    ]:
        """Convenience path: create a conversation and send its first message."""
        conversation = await self._conversations.create_conversation(title=title)
        (
            user_message,
            assistant_message,
            intent,
            planning,
            orchestration,
        ) = await self.send_message(conversation.id, content)
        return (
            conversation, user_message, assistant_message, intent, planning,
            orchestration,
        )

    # --- Internals ----------------------------------------------------------
    # Orchestration only. Everything below chooses *which* inputs reach the
    # formatter; none of it decides how a message is worded or ordered.

    async def _build_prompt(
        self,
        conversation_id: uuid.UUID,
        content: str,
        research: Optional[ResearchResult] = None,
        workflow: Optional[WorkflowResult] = None,
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

        # A formatter carrying this turn's results, or the shared one. Never
        # the shared instance mutated: results left on it would appear in the
        # next turn of an unrelated conversation.
        formatter = self._formatter
        # Both layers render into the *same* untrusted-research section. A
        # workflow's results are web content exactly as a bare search's are,
        # and giving them a second channel would mean a second place to get
        # the trust labelling right.
        block = ""
        if research is not None and research.results_block:
            block = research.results_block
        elif workflow is not None and workflow.research_block:
            block = workflow.research_block
        if block:
            formatter = self._formatter.with_research(block)

        if workflow is not None and workflow.needs_synthesis and workflow.artifact_path:
            # Told what the application has already decided, so it does not
            # ask for permission it has been given. It still decides nothing:
            # the path is fixed, the write is already approved, and the
            # application performs it after this reply exists.
            # Phrased as an instruction about *output shape*, not about
            # files. An earlier version described the pending write in
            # operational terms and the model responded by attempting a tool
            # call, which the provider rejected outright -- the turn failed.
            # Naming the destination is enough; describing the operation
            # invites the model to try performing it.
            formatter = formatter.with_workflow_note(
                "Write only the summary itself, as plain prose. Do not ask "
                "the user any questions, do not offer to take any action, "
                "and do not describe what will happen to your reply. "
                "Everything the user asked for beyond the summary has "
                "already been arranged and is handled outside this reply."
            )

        started = time.perf_counter()
        try:
            if package is not None:
                try:
                    return formatter.format(package), timings
                except Exception as exc:  # noqa: BLE001 - chat must still answer
                    logger.error(
                        "Prompt formatting failed; falling back to a minimal prompt",
                        extra={
                            "conversation_id": str(conversation_id),
                            "error": str(exc),
                        },
                        exc_info=exc,
                    )
                    # The fallback deliberately carries no research block.
                    # If formatting failed, the safest prompt is the smallest
                    # one -- and external content is the last thing to
                    # reintroduce through a degraded path.
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
