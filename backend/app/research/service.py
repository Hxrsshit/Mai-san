"""Research from the chat path, behind the same gates as everything else.

Stage 4F-B built web search and reached it only through the execution API.
Stage 4F-B's own report said connecting it to chat was "a capability in its
own right and is not smuggled in here". This is that capability, and the whole
design question was how to add it **without** reversing the decision 4F-B made
deliberately: that a query sent to a third party needs per-query consent.

The answer is two turns:

    turn N      "search the web for X"
                -> a proposal is recorded, and Mai asks for confirmation
                -> nothing is sent anywhere

    turn N+1    "yes"
                -> the proposal is approved and run
                -> results are synthesised

Nothing here is decided by a model. Identification is Stage 4D's phrase table,
confirmation is a phrase table in `confirmation.py`, and the proposal text is
written in this file. The model's only involvement is synthesising an answer
from results it is given, on the turn after consent was granted.

What this deliberately does not become
--------------------------------------

A general chat executor. `CHAT_CONFIRMABLE_TOOLS` contains one entry, and a
future `send_email` does not become chat-executable by being registered. The
chat path is not a shortcut around the execution API; it is a narrow,
named-tool route to one read-only capability.
"""

import uuid
from typing import FrozenSet, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.errors import ExecutionError
from app.execution.models import Execution
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.intent.schemas import IntentResult
from app.orchestration.resolution import ResolvedTurn
from app.orchestration import matching
from app.research.confirmation import Confirmation, interpret
from app.research.schemas import ResearchOutcome, ResearchResult

logger = get_logger(__name__)

#: Tools a chat confirmation may run. Exactly one.
#:
#: The most important line in this module. Without it, every executable tool
#: would become reachable from chat the moment it was registered -- and the
#: whole point of the execution API's separate propose/approve/execute steps
#: is that reaching a side effect should be deliberate.
#:
#: A tool joins this set only by being added here, in code, with a reason.
#: `web_search` qualifies because it is read-only, bounded, and its entire
#: effect is retrieving public pages.
CHAT_CONFIRMABLE_TOOLS: FrozenSet[str] = frozenset({"web_search"})

#: The integration each confirmable tool needs. Checked before proposing, so
#: Mai does not offer to do something it would then fail at.
_REQUIRED_INTEGRATION = {"web_search": "web_search"}


class ResearchService:
    """Decides whether a turn is research, and carries it through if so."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        executions: Optional[ExecutionService] = None,
        integrations=None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._executions = executions or ExecutionService(
            session, settings=self._settings
        )
        self._integrations = integrations

    # --- The one entry point ------------------------------------------------

    async def handle(
        self,
        conversation_id: uuid.UUID,
        message: str,
        intent: IntentResult,
        normalised: Optional[str] = None,
        resolved: Optional[ResolvedTurn] = None,
    ) -> ResearchResult:
        """Examine one turn. Never raises; degrades to NOT_RESEARCH.

        Order matters and is not arbitrary. A pending proposal is resolved
        *first*, because a "yes" must be read against what was actually
        proposed rather than re-matched as a fresh request -- otherwise "yes"
        would fall through to identification, match nothing, and silently
        strand the proposal.
        """
        try:
            pending = await self._pending_for(conversation_id)

            if pending is not None:
                # Deliberately the original. A confirmation is its own narrow
                # phrase table, and normalisation's vocabulary holds no
                # confirmation words -- running it here would add surface for
                # no coverage.
                return await self._resolve(pending, message)

            return await self._maybe_propose(
                conversation_id, message, intent, normalised, resolved
            )
        except Exception:  # noqa: BLE001
            # Research must never fail a chat turn. A failure here degrades to
            # an ordinary turn, which is the same principle retrieval and
            # assembly follow: the user gets an answer, just a smaller one.
            logger.warning(
                "Research handling failed; continuing as an ordinary turn",
                extra={"conversation_id": str(conversation_id)},
            )
            return ResearchResult(outcome=ResearchOutcome.NOT_RESEARCH)

    # --- Turn N: propose ----------------------------------------------------

    async def _maybe_propose(
        self,
        conversation_id: uuid.UUID,
        message: str,
        intent: IntentResult,
        normalised: Optional[str] = None,
        resolved: Optional[ResolvedTurn] = None,
    ) -> ResearchResult:
        """Identify a research request and record it. Sends nothing anywhere."""
        # Recognition reads the repaired text; everything else reads what the
        # user actually wrote.
        reading = normalised or message
        candidate = self._research_candidate(reading)
        if candidate is None:
            if self._clarification_needed(reading):
                # Stage 5D.2. Recognised as a request, but its subject is not
                # in this message -- "search up the net and let me know". The
                # subject may be in something the user said earlier.
                #
                # The resolved subject is a *reading*, not a permission. It
                # goes through `_propose_query` exactly as a typed-out subject
                # does, so the user still sees the exact query and still has
                # to say yes before anything leaves the process.
                inherited = self._inherited_subject(resolved)
                if inherited is not None:
                    logger.info(
                        "Research subject resolved from earlier user turns",
                        extra={
                            "conversation_id": str(conversation_id),
                            # The provenance, not the text: the subject is the
                            # user's own words and belongs in the proposal
                            # shown to them, not in a log with different
                            # retention.
                            "resolution_source": resolved.source.value,
                            "subject_chars": len(inherited),
                        },
                    )
                    return await self._propose_query(conversation_id, inherited)
                # Recognised, unreadable. Asking costs a turn; guessing would
                # send a query nobody wrote to an external provider.
                return ResearchResult(
                    outcome=ResearchOutcome.NEEDS_CLARIFICATION,
                    reply=(
                        "I can search the web for you — what should I search "
                        "for? Naming the topic in your message lets me show "
                        "you the exact query before anything is sent."
                    ),
                )
            return ResearchResult(outcome=ResearchOutcome.NOT_RESEARCH)

        return await self._propose_query(conversation_id, candidate)

    async def propose_current_information(
        self, conversation_id: uuid.UUID, subject: str
    ) -> ResearchResult:
        """Propose a search because the question needs current information.

        Stage 5A.1's one entry point into this service. The freshness layer
        decided the *question* needs fresh evidence; this decides nothing
        except the wording, and then runs the identical gates a user-typed
        "search the web for X" runs: execution must be switched on, the
        provider must be configured, an execution record is created in
        `PROPOSED`, and the user still has to say yes.

        Freshness therefore cannot bypass consent, because there is no path
        here that skips it -- the same `_propose_query` serves both callers.
        """
        subject = (subject or "").strip()
        if not subject:
            return ResearchResult(outcome=ResearchOutcome.NOT_RESEARCH)
        return await self._propose_query(
            conversation_id, subject, from_freshness=True
        )

    async def _propose_query(
        self,
        conversation_id: uuid.UUID,
        candidate: str,
        from_freshness: bool = False,
    ) -> ResearchResult:
        """The gates, the record and the disclosure. One implementation.

        Shared by the explicit request path and the freshness path so there is
        exactly one place where a search becomes proposable -- two would be
        two places for a gate to be forgotten, and the freshness path is
        precisely where someone would be tempted to skip one.

        `from_freshness` changes the sentences and nothing else. It says *why*
        Mai wants to look, which matters most when it cannot: telling someone
        "there is no recent news" when the truth is "I could not go and look"
        is the untruthfulness this stage exists to prevent.
        """
        if not self._settings.EXECUTION_ENABLED:
            # Truthful, and specific about which switch. "I can't search" when
            # the real answer is "an operator has this switched off" sends
            # someone looking in the wrong place.
            return ResearchResult(
                outcome=ResearchOutcome.DISABLED,
                query=candidate,
                reply=(
                    "Answering that accurately needs current information from "
                    "the web, and I can't look it up: action execution is "
                    "switched off for this deployment. I'd rather say so than "
                    "answer from what I was trained on and let it read as "
                    "current."
                    if from_freshness else
                    "I can't search the web right now: action execution is "
                    "switched off for this deployment. I can still help you "
                    "think the question through."
                ),
            )

        if not self._integration_available("web_search"):
            return ResearchResult(
                outcome=ResearchOutcome.NOT_CONFIGURED,
                query=candidate,
                reply=(
                    "Answering that accurately needs current information from "
                    "the web, and no search provider is configured for this "
                    "instance. I'd rather tell you that than answer from "
                    "training data and let it read as current."
                    if from_freshness else
                    "I can't search the web because no search provider is "
                    "configured for this instance. I can still help you think "
                    "the question through, or suggest what to search for."
                ),
            )

        execution = await self._executions.create(
            ExecutionRequest(
                tool_name="web_search",
                arguments={"query": candidate},
                # Scoped to the conversation and the query, so proposing the
                # same search twice in one conversation reuses one record
                # rather than accumulating them.
                idempotency_key=f"chat:{conversation_id}:{candidate}"[:128],
            ),
            conversation_id=conversation_id,
        )

        logger.info(
            "Research proposed",
            extra={
                "conversation_id": str(conversation_id),
                "execution_id": str(execution.id),
                # Length, not the query. A search query can name a person or a
                # diagnosis, and Stage 3D's rule is that such data does not go
                # to INFO.
                "query_chars": len(candidate),
                "from_freshness": from_freshness,
            },
        )

        opening = (
            "That looks like a question where the answer may have changed "
            f"since I was trained, so I'd rather check than guess. I can "
            f"search the web for **{candidate}** and answer from what comes "
            "back."
            if from_freshness else
            f"I can search the web for **{candidate}** and use the "
            "results to answer you."
        )

        return ResearchResult(
            outcome=ResearchOutcome.AWAITING_CONFIRMATION,
            query=candidate,
            execution_id=execution.id,
            reply=(
                f"{opening}\n\n"
                "That sends the query to an external search provider. Reply "
                "\"yes\" to go ahead, or anything else to skip it."
            ),
        )

    # --- Turn N+1: resolve --------------------------------------------------

    async def _resolve(
        self, pending: Execution, message: str
    ) -> ResearchResult:
        """Apply the user's reply to the pending proposal. Deterministic."""
        decision = interpret(message)
        query = str((pending.arguments or {}).get("query", ""))

        if decision is Confirmation.DECLINED:
            await self._discard(pending, "declined")
            return ResearchResult(
                outcome=ResearchOutcome.DECLINED,
                query=query,
                execution_id=pending.id,
                reply="Understood — I won't run that search.",
            )

        if decision is Confirmation.UNRELATED:
            # The turn moved on. The proposal is dropped rather than left to
            # be confirmed by an unrelated "yes" several messages later.
            await self._discard(pending, "abandoned")
            return ResearchResult(
                outcome=ResearchOutcome.ABANDONED, query=query,
                execution_id=pending.id,
            )

        return await self._run(pending, query)

    async def _run(self, pending: Execution, query: str) -> ResearchResult:
        """Approve and execute. Every Stage 4E gate still applies.

        Approval here is a genuine approval: the user was shown the exact
        query and said yes to it, and the fingerprint recorded at approval is
        over that same payload. Nothing about this path skips a check -- it
        supplies the human decision the checks were waiting for.
        """
        try:
            await self._executions.approve(pending.id)
            execution, outcome = await self._executions.run_returning_outcome(
                pending.id
            )
        except ExecutionError as refusal:
            logger.info(
                "Research execution refused",
                extra={
                    "execution_id": str(pending.id),
                    "reason": refusal.reason,
                },
            )
            return ResearchResult(
                outcome=ResearchOutcome.FAILED,
                query=query,
                execution_id=pending.id,
                reason=refusal.reason,
                reply=self._failure_text(refusal.reason),
            )

        block, count = self._rendered_results(outcome)
        return ResearchResult(
            outcome=ResearchOutcome.COMPLETED,
            query=query,
            execution_id=execution.id,
            results_block=block,
            result_count=count,
        )

    # --- Internals ----------------------------------------------------------

    def _research_candidate(self, message: str) -> Optional[str]:
        """The extracted subject, if this message is a research request.

        Stage 4D's matcher, filtered to the one tool chat may confirm. Since
        Stage 4F-F.1 the `web_search` entry is recognised by the grammar in
        `app.research.language`, so what comes back is the *subject* rather
        than the whole sentence -- but the route is unchanged, and there is
        still one place to audit.

        Returns None both for "not a research request" and for a request whose
        subject could not be read. `_clarification_needed` tells those apart,
        because they deserve different replies.
        """
        for candidate in matching.find_candidates(message):
            if candidate.tool_name in CHAT_CONFIRMABLE_TOOLS:
                query = str(candidate.arguments.get("query", "")).strip()
                # The matcher's placeholder for "recognised, but no subject".
                # Searching for it would send a string nobody wrote to a
                # third party.
                if query and query != "(empty)":
                    return query
        return None

    @staticmethod
    def _inherited_subject(resolved: Optional[ResolvedTurn]) -> Optional[str]:
        """The subject carried over from earlier user turns, if there is one.

        Returns None for every unresolved and ambiguous case, which sends the
        turn to the existing clarification reply. Refusing to guess is the
        point: a wrong guess here is a query nobody wrote being shown to a
        third party, and asking costs one message.
        """
        if resolved is None or not resolved.is_resolved:
            return None
        subject = resolved.subject.strip()
        return subject or None

    @staticmethod
    def _clarification_needed(message: str) -> bool:
        """Whether this was a research request Mai could not read.

        Distinct from "not research at all": someone who typed "look this up
        online" asked for something, and answering as though they had not is
        a worse failure than asking what they meant.
        """
        from app.research.language import recognise

        recognition = recognise(message)
        return recognition.is_request and not recognition.query

    def _integration_available(self, name: str) -> bool:
        registry = self._integrations
        if registry is None:
            from app.integrations.registry import get_integration_registry

            registry = get_integration_registry()

        integration = registry.get(_REQUIRED_INTEGRATION.get(name, name))
        return bool(integration is not None and integration.available)

    async def _pending_for(
        self, conversation_id: uuid.UUID
    ) -> Optional[Execution]:
        """The proposal this conversation is waiting on, if any.

        Only `PROPOSED`, only this conversation, only a chat-confirmable tool,
        and only the most recent. An execution created through the API carries
        no conversation, so it can never be confirmed by a chat message.
        """
        result = await self._session.execute(
            select(Execution)
            .where(
                Execution.conversation_id == conversation_id,
                Execution.state == ExecutionState.PROPOSED,
                Execution.tool_name.in_(tuple(CHAT_CONFIRMABLE_TOOLS)),
            )
            .order_by(Execution.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def _discard(self, pending: Execution, reason: str) -> None:
        """Withdraw a proposal so it cannot be confirmed later.

        Uses the Stage 4E revoke path rather than deleting the row: the
        journal should record that a search was proposed and not run, which
        is exactly the kind of thing an audit reader wants to see.
        """
        try:
            await self._executions.revoke(pending.id)
        except ExecutionError:
            # Already terminal. Nothing to withdraw, and nothing to fix.
            logger.debug(
                "Pending research proposal was already resolved",
                extra={"execution_id": str(pending.id), "reason": reason},
            )

    @staticmethod
    def _rendered_results(outcome) -> Tuple[str, int]:
        """Pull the rendered external content out of the tool's outcome.

        The content arrives as the `ExternalData` the search integration
        built, so it is already flattened, already attributed to named
        sources, and already labelled untrusted. Nothing is re-rendered here
        -- re-rendering would be a second place for that labelling to be
        forgotten.

        Returns `("", 0)` rather than raising on an unexpected shape: a search
        that succeeded but whose results cannot be read should produce an
        honest empty answer rather than a failed turn.
        """
        if outcome is None:
            return "", 0

        external = (outcome.data or {}).get("external")
        if not isinstance(external, dict):
            return "", 0

        content = external.get("content")
        if not isinstance(content, str) or not content.strip():
            return "", 0

        # One line per source heading, which is how the search layer renders
        # them. Counting headings rather than trusting a number from anywhere.
        count = sum(
            1 for line in content.split("\n") if line.startswith("[")
        )
        return content, count

    @staticmethod
    def _failure_text(reason: Optional[str]) -> str:
        """What to say when a search did not happen. Never "I searched".

        Mapped from an application reason code, so the sentence cannot
        describe an outcome the record does not support.
        """
        return _FAILURE_TEXT.get(
            reason or "",
            "I couldn't complete that web search. Nothing was retrieved.",
        )


#: One sentence per refusal. Each says plainly that no search happened.
_FAILURE_TEXT = {
    "provider_timeout": (
        "I couldn't complete that web search: the search service timed out. "
        "Nothing was retrieved."
    ),
    "provider_rate_limited": (
        "I couldn't complete that web search: the search service is rate "
        "limiting requests. Nothing was retrieved."
    ),
    "provider_unauthorized": (
        "I couldn't complete that web search: the search provider rejected "
        "this instance's credentials. Nothing was retrieved."
    ),
    "provider_unavailable": (
        "I couldn't complete that web search: the search service is "
        "unavailable. Nothing was retrieved."
    ),
    "integration_unavailable": (
        "I couldn't complete that web search: no search provider is "
        "configured for this instance. Nothing was retrieved."
    ),
    "execution_disabled": (
        "I couldn't complete that web search: action execution is switched "
        "off for this deployment. Nothing was retrieved."
    ),
    "destination_not_allowed": (
        "I couldn't complete that web search: the request was refused by "
        "this instance's network policy. Nothing was retrieved."
    ),
}


__all__ = ["CHAT_CONFIRMABLE_TOOLS", "ResearchService"]
