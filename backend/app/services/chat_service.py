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
from app.research.schemas import ResearchOutcome, ResearchResult
from app.research.service import ResearchService
from app.calendar.schemas import CalendarOutcome, CalendarResult
from app.calendar.service import CalendarService
from app.language.normalise import normalise
from app.prompt.formatter import with_recovery_instruction
from app.synthesis.contract import validate as validate_response
from app.orchestration.freshness import assess as assess_freshness
from app.mail.schemas import MailOutcome, MailResult
from app.mail.service import MailService
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
        mail_service: Optional["MailService"] = None,
        calendar_service: Optional["CalendarService"] = None,
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
        self._calendar = calendar_service or CalendarService(
            session, settings=settings
        )
        self._mail = mail_service or MailService(session, settings=settings)
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
        # Stage 4F-G. A calendar question is recognised before research and
        # before a workflow: its grammar requires the calendar to be *named*,
        # so it matches a narrower set of messages than either, and a message
        # it claims is not one the others would have handled.
        #
        # No confirmation turn, deliberately -- see
        # `app.tools.catalog._declare_calendar_read` for that decision and the
        # argument against it. Adds no model call: recognition is a grammar,
        # the time window comes from the application clock, and every refusal
        # is application-written.
        # Stage 5A. One normalisation, computed here and passed to the
        # recognisers only.
        #
        # `content` remains the user's own words everywhere it matters: it is
        # what gets stored as the message, what reaches the prompt, and what
        # an audit reader sees. Only the grammars read the repaired text, and
        # they can only ever be handed a word Mai already recognises -- the
        # normaliser's output vocabulary is closed. So this can change *which*
        # grammar matches and nothing about what any of them is permitted to
        # do.
        #
        # Applied to the user's message and to nothing else. Calendar events,
        # web results and retrieved memories never pass through it: a
        # normaliser over untrusted content would be a way to nudge a hostile
        # string until it matched a request grammar.
        reading = normalise(content)

        calendar = await self._calendar.handle(
            conversation_id, content, normalised=reading.text
        )

        if calendar.has_reply:
            return await self._answer_without_the_model(
                conversation, conversation_id, content, ResearchResult(),
                intent, planning, orchestration, calendar=calendar,
            )

        # Stage 5B. Mail is recognised after the calendar and before the
        # workflow and research layers.
        #
        # After the calendar because the two grammars are disjoint and the
        # calendar one is older and narrower. Before research because the mail
        # grammar requires a mail noun *and* a reading verb, so anything it
        # claims would otherwise have been mishandled -- and because the mail
        # grammar itself refuses anything that looks like a web search, which
        # is what keeps "search the web for Gmail pricing" on the research path.
        mail = (
            MailResult()
            if calendar.outcome is not CalendarOutcome.NOT_CALENDAR
            else await self._mail.handle(
                conversation_id, content, normalised=reading.text
            )
        )

        if mail.has_reply:
            return await self._answer_without_the_model(
                conversation, conversation_id, content, ResearchResult(),
                intent, planning, orchestration, mail=mail,
            )

        workflow = (
            WorkflowResult()
            if (
                calendar.outcome is not CalendarOutcome.NOT_CALENDAR
                or mail.outcome is not MailOutcome.NOT_MAIL
            )
            else await self._workflows.handle(
                conversation_id, content, intent, normalised=reading.text
            )
        )

        if workflow.has_reply:
            return await self._answer_without_the_model(
                conversation, conversation_id, content, ResearchResult(),
                intent, planning, orchestration, workflow=workflow,
            )

        # A turn the workflow layer claimed is not also a research turn.
        # Running both would propose the same search twice.
        research = (
            ResearchResult()
            if (
                workflow.outcome is not WorkflowOutcome.NOT_WORKFLOW
                or calendar.outcome is not CalendarOutcome.NOT_CALENDAR
                or mail.outcome is not MailOutcome.NOT_MAIL
            )
            else await self._research.handle(
                conversation_id, content, intent, normalised=reading.text
            )
        )

        # Stage 5A.1. Freshness runs **last**, and only when every recogniser
        # above has declined.
        #
        # That ordering is the whole of the personal-data guarantee. "What's
        # on my calendar tomorrow?" is a currentness question, and so is "what
        # are my latest emails?" -- but the calendar and mail recognisers have
        # already claimed them by the time control reaches here, so freshness
        # is structurally incapable of routing a personal question to the web.
        # It is not a rule this layer follows; it is a place it stands.
        #
        # Assessed on `reading.text`, the *user's* repaired message. Never on
        # a search result, an email body, a calendar title or a retrieved
        # memory -- see `app.orchestration.freshness`.
        if (
            not research.has_reply
            and research.outcome is ResearchOutcome.NOT_RESEARCH
            and calendar.outcome is CalendarOutcome.NOT_CALENDAR
            and mail.outcome is MailOutcome.NOT_MAIL
            and workflow.outcome is WorkflowOutcome.NOT_WORKFLOW
        ):
            freshness = assess_freshness(reading.text)
            if freshness.wants_web:
                # A proposal, not a search. This returns the same
                # `AWAITING_CONFIRMATION` a typed "search the web for X"
                # returns, through the same gates, and the user still answers.
                research = await self._research.propose_current_information(
                    conversation_id, freshness.subject
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
            conversation_id, content, research=research, workflow=workflow,
            calendar=calendar, mail=mail,
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

        # Stage 5A.2. What the model returned is a *candidate*, not an answer.
        #
        # Before this, `llm_response.content` went straight into conversation
        # history. When the model answered a research turn with a tool-call
        # object instead of prose, the object became the assistant's message:
        # the user saw JSON, and the blob entered history, where the model
        # read it next turn as an example of how Mai replies and produced
        # another one. One malformed response became a pattern.
        validated = validate_response(llm_response.content)

        if not validated.accepted:
            validated, llm_response = await self._recover_synthesis(
                prompt, llm_response, validated, conversation_id
            )

        if validated.accepted:
            reply = validated.text
        else:
            # Truthful, and built from what actually ran -- never from the
            # model's own account of it. A turn whose synthesis failed after a
            # real search must say the search happened, and a turn where
            # nothing ran must not imply it did.
            reply = self._synthesis_failed_reply(
                validated, research=research, calendar=calendar,
                mail=mail, workflow=workflow,
            )

        if workflow.needs_synthesis and workflow.artifact_requested:
            # The synthesis just generated becomes the artifact's body. This
            # is the only ordering that works: the file's content is the
            # answer, so the file cannot be written before the answer exists.
            #
            # Gated on the plan having asked for an artifact. Without that
            # gate a briefing with no document still called `finalise`, whose
            # return value then replaced a PARTIAL outcome with COMPLETED --
            # reporting a composition whose search had failed as a success.
            finished = await self._workflows.finalise(
                workflow.workflow_id, reply
            )
            # Merged rather than replaced. `finalise` reports the artifact
            # half and knows nothing about the research half, so taking its
            # result wholesale dropped the result count and the block -- the
            # API then reported a successful workflow that had found nothing.
            # Merged rather than replaced, and the field list is exhaustive
            # on purpose. `finalise` reports the artifact half and knows
            # nothing about what came before it, so every value describing an
            # earlier step has to be carried across explicitly -- a field
            # forgotten here becomes a false report about work that did
            # happen. Stage 4F-E lost the result count this way.
            workflow = finished.model_copy(
                update={
                    "research_block": workflow.research_block,
                    "result_count": workflow.result_count,
                    "calendar_block": workflow.calendar_block,
                    "calendar_event_count": workflow.calendar_event_count,
                    "calendar_read": workflow.calendar_read,
                    "researched": workflow.researched,
                    "research_attempted": workflow.research_attempted,
                    "steps": workflow.steps + finished.steps,
                }
            )
            # Appended by the application, from the execution record -- never
            # left to the model to claim. A model that says "I've saved this"
            # when the write failed is the exact failure Stage 4E.1 exists to
            # prevent, and it cannot know the outcome in any case: the write
            # happens after it has finished speaking.
        note = self._composition_note(workflow)
        if note:
            # Outside the artifact branch: a composition that wrote no file
            # can still have something the application must say about it,
            # such as a search that did not land.
            reply = f"{reply}\n\n{note}"

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
            research, workflow, calendar, mail,
        )

    @staticmethod
    def _composition_note(workflow: WorkflowResult) -> str:
        """What actually happened, appended by the application.

        Every sentence here is built from execution records, never from the
        model -- which could not know the outcomes in any case, because the
        artifact is written after it has finished speaking.

        Silence is a valid answer. Stage 4F-E always had a file to report, so
        it always said something; a briefing that asked for no document must
        not apologise for failing to write one, which is what an
        unconditional artifact sentence did.
        """
        parts = []

        if workflow.research_attempted and not workflow.researched:
            # The distinction that keeps a briefing honest. The model has just
            # written prose from the calendar alone; without this line the
            # user has no way to know the research half never landed.
            parts.append(
                "I couldn't complete the web search, so there's no outside "
                "research in this — it's from your calendar only."
            )

        if workflow.artifact_requested:
            parts.append(
                f"I've saved this to `{workflow.artifact_path}` in my workspace."
                if workflow.artifact_written
                else (
                    "I couldn't save this to a file — the write didn't "
                    "succeed, so nothing was created."
                )
            )

        return " ".join(parts)


    async def _recover_synthesis(
        self, prompt, llm_response, validated, conversation_id
    ):
        """One more attempt, with the contract restated. Exactly one.

        Bounded at a single retry on purpose. A model that has just ignored
        the contract is not obviously going to honour it on the third ask, and
        an unbounded loop would be a denial of service the model triggers
        against itself. No new research runs, no tool is reached and no
        approval is consulted -- the same prompt goes back with one corrective
        instruction appended, so the recovery costs one generation and can
        acquire nothing.
        """
        logger.warning(
            "Synthesis did not meet the response contract; retrying once",
            extra={
                "conversation_id": str(conversation_id),
                "kind": validated.kind.value,
            },
        )

        # Built by the formatter, which owns chat prompt text. Reuses the
        # original parts exactly -- nothing is retrieved or researched again.
        retry_prompt = with_recovery_instruction(prompt)
        try:
            retried = await self._provider.generate_response(retry_prompt.messages)
        except LLMError:
            # The recovery call failed. That is a provider failure on top of a
            # contract failure; the caller writes a truthful line rather than
            # raising, because the turn already has real work behind it.
            logger.warning(
                "Synthesis recovery call failed",
                extra={"conversation_id": str(conversation_id)},
            )
            return validated, llm_response

        recovered = validate_response(retried.content)
        if recovered.accepted:
            logger.info(
                "Synthesis recovered on the retry",
                extra={"conversation_id": str(conversation_id)},
            )
            return recovered, retried

        logger.warning(
            "Synthesis recovery also failed the response contract",
            extra={"kind": recovered.kind.value},
        )
        return recovered, retried

    @staticmethod
    def _synthesis_failed_reply(
        validated, research=None, calendar=None, mail=None, workflow=None
    ) -> str:
        """What to say when nothing usable was generated.

        Every clause is built from execution state. The distinction the brief
        insists on is real and easy to get wrong: "no answer was generated" is
        not "no information was found", and neither is "the search failed".
        Collapsing them would report a working search as an empty internet.
        """
        did = []
        if calendar is not None and getattr(calendar, "events_block", ""):
            did.append("read your calendar")
        if mail is not None and getattr(mail, "messages_block", ""):
            did.append("read your mail")
        if research is not None and getattr(research, "succeeded", False):
            did.append("searched the web")
        elif workflow is not None and getattr(workflow, "researched", False):
            did.append("searched the web")

        if did:
            performed = did[0] if len(did) == 1 else (
                ", ".join(did[:-1]) + " and " + did[-1]
            )
            return (
                f"I {performed} and got the information back, but I couldn't "
                "turn it into an answer just then — what came back from the "
                "model wasn't usable. Nothing was lost; ask me again and I'll "
                "have another go."
            )

        return (
            "I couldn't produce an answer just then — what came back from the "
            "model wasn't usable. Nothing was searched or read, so nothing "
            "was lost. Ask me again and I'll have another go."
        )

    async def _answer_without_the_model(
        self, conversation, conversation_id, content, research,
        intent, planning, orchestration, workflow=None, calendar=None,
        mail=None,
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
            content=(
                calendar.reply if calendar is not None and calendar.has_reply
                else mail.reply if mail is not None and mail.has_reply
                else workflow.reply if workflow is not None and workflow.has_reply
                else research.reply
            ),
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
            calendar if calendar is not None else CalendarResult(),
            mail if mail is not None else MailResult(),
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
        mail: Optional[MailResult] = None,
        calendar: Optional[CalendarResult] = None,
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
        if (
            calendar is None or not calendar.events_block
        ) and workflow is not None and workflow.calendar_block:
            # A briefing's calendar half. The same section, the same framing
            # and the same untrusted preamble as a bare calendar question --
            # it is the same data from the same integration, and giving a
            # composition its own channel would be a second place for the
            # labelling to be got right.
            formatter = formatter.with_calendar(
                workflow.calendar_block, "", availability=False
            )
        if mail is not None and mail.messages_block:
            # Its own section. Mail is the user's own correspondence and
            # carries the strongest untrusted framing in the system -- anyone
            # who knows an address can put text here.
            formatter = formatter.with_mail(mail.messages_block)
        if calendar is not None and calendar.events_block:
            # Calendar events go into the *personal data* section, not the
            # research one. They are the user's own schedule rather than a
            # stranger's web page -- a different provenance, a different
            # heading, and a different sentence framing them.
            formatter = formatter.with_calendar(
                calendar.events_block,
                calendar.window_label,
                availability=calendar.is_availability,
            )
        if block:
            formatter = formatter.with_research(block)

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
