"""Reading the user's Gmail from a chat turn.

Longer than the calendar service by exactly one thing: a confirmation turn.

Stage 4F-G let a calendar read run without asking, and argued it -- an event
title is low-sensitivity, the question is asked often, and a prompt every time
trains people to click through. **Neither half of that argument transfers to
mail.** `gmail.readonly` is a Google *restricted* scope covering the whole
mailbox; a message body is the most sensitive personal data Mai touches, and
it is routinely about third parties who consented to nothing. So Gmail follows
Stage 4F-D's research rule instead: the user is told what will be read, in
what quantity, and says yes.

    turn N      "do I have any unread emails from Netflix?"
                -> "I can look for unread messages from "netflix" -- up to 5,
                    senders and subjects only, no message bodies. Shall I?"
                -> nothing read, nothing sent to Google

    turn N+1    "yes"
                -> the read happens

What the approval binds
-----------------------

The proposal creates an `Execution` in `PROPOSED` with the whole typed query
as its arguments, so Stage 4E's fingerprint covers the sender, the terms, the
unread flag, the date bound, the result count *and* the body count. A stored
proposal edited between the two turns no longer matches, and the read is
refused -- which is what stops an approval for "5 subjects" becoming a read of
five bodies.

No model is consulted here. Recognition is a grammar, the query is built by
`MailQuery`, and every refusal is application-written text.
"""

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.errors import ExecutionError
from app.execution.models import Execution
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.integrations.base import IntegrationState
from app.mail.schemas import MailOutcome, MailResult
from app.orchestration import mail_language
from app.research.confirmation import Confirmation, interpret

logger = get_logger(__name__)

#: The one tool this service may propose. Exactly one, and read-only.
#:
#: `gmail_get_message` is declared and executable, but the chat path does not
#: reach it: a listing with a bounded `body_count` already fetches exactly the
#: selected messages, so routing through a second execution would double the
#: approval surface for no change in what leaves the process.
MAIL_TOOL = "gmail_list_messages"

#: Reason codes that mean something specific, mapped to what to tell the user.
_REPLIES = {
    "gmail_not_connected": (
        "I can read your Gmail, but no Google account is connected for mail "
        "yet. Connecting Gmail is separate from Calendar — connect it and "
        "I'll be able to answer that."
    ),
    "gmail_reauthorisation_required": (
        "My access to your Gmail has expired or been revoked. Reconnecting "
        "Gmail will restore it."
    ),
    "gmail_insufficient_scope": (
        "My access to your Google account does not cover reading Gmail. "
        "Reconnecting Gmail will fix that."
    ),
    "gmail_forbidden": (
        "Google refused the request for your mail. That usually means the "
        "Gmail permission needs granting again. Nothing was retrieved."
    ),
    "gmail_rate_limited": (
        "Google is rate limiting mail requests at the moment, so I couldn't "
        "read your messages. Nothing was retrieved."
    ),
    "gmail_unavailable": (
        "Gmail didn't respond, so I couldn't read your messages. Nothing was "
        "retrieved."
    ),
}

_DEFAULT_FAILURE = (
    "I couldn't read your mail just then. Nothing was retrieved."
)


class MailService:
    """Answers questions about the user's mail by reading a bounded set."""

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

    # --- One turn -----------------------------------------------------------

    async def handle(
        self,
        conversation_id: uuid.UUID,
        message: str,
        normalised: Optional[str] = None,
    ) -> MailResult:
        """Examine one turn. Never raises; degrades to NOT_MAIL."""
        try:
            pending = await self._pending_for(conversation_id)
            if pending is not None:
                # The original, deliberately: a confirmation is its own narrow
                # phrase table and Stage 5A's vocabulary holds no confirmation
                # words, so normalising here would add surface for no coverage.
                return await self._resolve(pending, message)
            return await self._maybe_propose(
                conversation_id, normalised or message
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Mail handling failed; continuing as an ordinary turn",
                extra={"conversation_id": str(conversation_id)},
            )
            return MailResult(outcome=MailOutcome.NOT_MAIL)

    # --- Turn N: propose ----------------------------------------------------

    async def _maybe_propose(
        self, conversation_id: uuid.UUID, message: str
    ) -> MailResult:
        """Recognise and disclose. Reads nothing."""
        request = mail_language.recognise(message)

        if request.is_write_request:
            # Recognised only so Mai can say plainly that it cannot. No write
            # capability exists in the catalogue, the executable registry or
            # the OAuth scope, so there is nothing here to refuse *access* to
            # -- this shapes the sentence, not the permission.
            return MailResult(
                outcome=MailOutcome.WRITE_NOT_SUPPORTED,
                reply=(
                    "I can only read your mail — I can't send, reply, "
                    "forward, delete, archive or label anything. My access to "
                    "Gmail is read-only, so nothing was changed."
                ),
            )

        if not request.is_readable:
            return MailResult(outcome=MailOutcome.NOT_MAIL)

        if not self._settings.EXECUTION_ENABLED:
            return MailResult(
                outcome=MailOutcome.DISABLED,
                intent=request.intent.value if request.intent else None,
                reply=(
                    "I can't read your mail right now: action execution is "
                    "switched off for this deployment."
                ),
            )

        unavailable = self._unavailable_reply(request)
        if unavailable is not None:
            return unavailable

        arguments = {
            "sender": request.sender,
            "subject_terms": list(request.subject_terms),
            "text_terms": [],
            "unread_only": request.unread_only,
            "newer_than_days": request.newer_than_days,
            "max_results": request.max_results,
            "body_count": request.body_count,
        }

        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name=MAIL_TOOL,
                    arguments=arguments,
                    idempotency_key=(
                        f"mail:{conversation_id}:{request.family}"
                        f":{request.sender}:{request.unread_only}"
                        f":{request.max_results}:{request.body_count}"
                    )[:128],
                ),
                conversation_id=conversation_id,
            )
        except ExecutionError as refusal:
            logger.info("Mail proposal refused", extra={"reason": refusal.reason})
            return self._failure(refusal.reason, request)

        logger.info(
            "Mail read proposed",
            # Counts and flags only. Never a sender, a subject or a body -- a
            # sender address is personal data about someone who is not the
            # user, and Stage 3D's rule is that such text does not reach INFO.
            extra={
                "integration": "google_gmail",
                "operation": MAIL_TOOL,
                "conversation_id": str(conversation_id),
                "max_results": request.max_results,
                "body_count": request.body_count,
                "unread_only": request.unread_only,
                "has_sender_filter": bool(request.sender),
            },
        )

        return MailResult(
            outcome=MailOutcome.AWAITING_CONFIRMATION,
            intent=request.intent.value if request.intent else None,
            execution_id=execution.id,
            reply=self._proposal_text(request),
        )

    @staticmethod
    def _proposal_text(request) -> str:
        """What the user is agreeing to, in their own terms.

        Says the quantity and the depth, because those are the parts that
        matter: "5 subjects" and "5 bodies" are very different disclosures to
        an external model provider, and the difference should be in the
        sentence rather than in a schema nobody reads.
        """
        what = []
        if request.unread_only:
            what.append("unread")
        what.append("messages")
        if request.priority:
            # Said plainly, because the user asked a question the mailbox
            # cannot answer: Gmail is not being asked for "important" ones.
            # A bounded recent set is read and the judgement is made from
            # what comes back, and the sentence should not imply otherwise.
            what.append("to see which look worth your attention")
        if request.sender:
            what.append(f'from "{request.sender}"')
        if request.subject_terms:
            what.append(f'about "{request.subject_terms[0]}"')
        if request.newer_than_days == 1:
            what.append("from today")
        elif request.newer_than_days:
            what.append(f"from the last {request.newer_than_days} days")

        depth = (
            f"reading the full text of {request.body_count}"
            if request.body_count
            else "senders and subjects only, no message bodies"
        )
        return (
            f"I can look for {' '.join(what)} — up to {request.max_results}, "
            f"{depth}.\n\n"
            "Whatever I read is sent to the configured AI model provider so "
            'it can answer. Reply "yes" to go ahead, or anything else to '
            "skip it."
        )

    # --- Turn N+1: resolve --------------------------------------------------

    async def _resolve(self, pending: Execution, message: str) -> MailResult:
        """Apply the user's reply. Deterministic; no model is consulted."""
        decision = interpret(message)

        if decision is Confirmation.DECLINED:
            await self._discard(pending, "declined")
            return MailResult(
                outcome=MailOutcome.DECLINED,
                reply="Understood — I won't read your mail.",
            )

        if decision is Confirmation.UNRELATED:
            await self._discard(pending, "abandoned")
            return MailResult(outcome=MailOutcome.ABANDONED)

        return await self._run(pending)

    async def _run(self, pending: Execution) -> MailResult:
        """Approve this exact proposal and read. Nothing was read before now."""
        arguments = dict(pending.arguments or {})
        try:
            await self._executions.approve(pending.id)
            execution, outcome = await self._executions.run_returning_outcome(
                pending.id
            )
        except ExecutionError as refusal:
            logger.info("Mail read refused", extra={"reason": refusal.reason})
            return self._failure(refusal.reason, intent=self._intent_of(arguments))

        if execution.state is not ExecutionState.SUCCEEDED or outcome is None:
            return self._failure(
                execution.error_code or "gmail_failed",
                intent=self._intent_of(arguments),
            )

        block, count = self._rendered(outcome)
        bodies = int(arguments.get("body_count") or 0)
        intent = self._intent_of(arguments)
        return MailResult(
            outcome=MailOutcome.COMPLETED,
            # From the approved arguments, like the intent it is derived
            # from. A summary read that the user asked a priority question
            # about keeps its bodies and its own instruction.
            priority=(
                intent == mail_language.MailIntent.ATTENTION.value
            ),
            # Derived from the *approved* arguments rather than remembered
            # across the turn. The approval is the authoritative record of
            # what was agreed to, so reporting from it cannot describe a read
            # other than the one that happened.
            intent=intent,
            messages_block=block,
            message_count=count,
            body_count=bodies,
        )

    @staticmethod
    def _intent_of(arguments) -> str:
        """Which kind of read the approved arguments describe.

        Read off the *approved* arguments, never remembered across the two
        turns, so what is reported cannot describe a read other than the one
        the user agreed to.

        An attention read is a bodiless listing with its own result bound --
        the shapes are distinguishable because `MAX_ATTENTION_RESULTS` and
        `MAX_LIST_RESULTS` differ, and a test pins that they do. The flag is
        not stored in the arguments because the arguments are the tool's
        payload: `GmailListMessagesArguments` is `extra="forbid"`, and a
        field that is not part of the query has no business travelling with
        one.
        """
        bodies = int(arguments.get("body_count") or 0)
        if not bodies:
            if int(arguments.get("max_results") or 0) == (
                mail_language.MAX_ATTENTION_RESULTS
            ):
                return mail_language.MailIntent.ATTENTION.value
            return mail_language.MailIntent.LIST.value
        if bodies == 1:
            return mail_language.MailIntent.READ.value
        return mail_language.MailIntent.SUMMARISE.value

    # --- Internals ----------------------------------------------------------

    async def _discard(self, pending: Execution, reason: str) -> None:
        """Withdraw a proposal so it cannot be confirmed later.

        The Stage 4E revoke path rather than a delete: the journal should
        record that a mail read was proposed and not run, which is exactly the
        kind of thing an audit reader wants to see.
        """
        try:
            await self._executions.revoke(pending.id)
        except ExecutionError:
            logger.debug(
                "Pending mail proposal was already resolved",
                extra={"execution_id": str(pending.id), "reason": reason},
            )

    async def _pending_for(
        self, conversation_id: uuid.UUID
    ) -> Optional[Execution]:
        """The mail proposal this conversation is waiting on, if any.

        Only `PROPOSED`, only this conversation, only the mail tool, and only
        the most recent. An execution created through the API carries no
        conversation, so it can never be confirmed by a chat message.
        """
        result = await self._session.execute(
            select(Execution)
            .where(
                Execution.conversation_id == conversation_id,
                Execution.state == ExecutionState.PROPOSED,
                Execution.tool_name == MAIL_TOOL,
            )
            .order_by(Execution.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    def _unavailable_reply(self, request) -> Optional[MailResult]:
        """Why Gmail cannot be used, if it cannot.

        Four distinct answers. "Not configured" is an operator problem, "not
        connected" is a one-click user action, and "reauthorisation" is a
        different user action -- collapsing them would send someone to the
        wrong place. None of them is "you have no email", which is the lie
        this method exists to avoid.
        """
        intent = request.intent.value if request.intent else None
        integration = self._integration()
        if integration is None:
            return MailResult(
                outcome=MailOutcome.NOT_CONFIGURED,
                intent=intent,
                reply=(
                    "I don't have a Gmail integration configured for this "
                    "instance."
                ),
            )

        state = integration.state()
        if state is IntegrationState.NOT_CONFIGURED:
            return MailResult(
                outcome=MailOutcome.NOT_CONFIGURED,
                intent=intent,
                reply=(
                    "Gmail isn't configured for this instance, so I can't "
                    "read your mail."
                ),
            )
        if state is IntegrationState.AUTHENTICATION_REQUIRED:
            return MailResult(
                outcome=MailOutcome.NOT_CONNECTED,
                intent=intent,
                reason="gmail_not_connected",
                reply=_REPLIES["gmail_not_connected"],
            )
        if state is not IntegrationState.AVAILABLE:
            return MailResult(
                outcome=MailOutcome.FAILED,
                intent=intent,
                reason=state.value,
                reply=_DEFAULT_FAILURE,
            )
        return None

    def _integration(self):
        registry = self._integrations
        if registry is None:
            from app.integrations.registry import get_integration_registry

            registry = get_integration_registry()
        return registry.get("google_gmail")

    def _failure(self, reason: str, request=None, intent=None) -> MailResult:
        """A failure, reported as what it was.

        Never "I found no emails". A provider refusal and an empty mailbox are
        different facts, and reporting the first as the second would be the
        most useful lie Mai could tell.
        """
        outcome = (
            MailOutcome.REAUTHORISATION_REQUIRED
            if reason in ("gmail_reauthorisation_required", "gmail_insufficient_scope")
            else MailOutcome.FAILED
        )
        return MailResult(
            outcome=outcome,
            intent=(
                intent
                if intent
                else (request.intent.value if request and request.intent else None)
            ),
            reason=reason,
            reply=_REPLIES.get(reason, _DEFAULT_FAILURE),
        )

    @staticmethod
    def _rendered(outcome):
        """Pull the rendered messages out of the tool's outcome.

        The content arrives as the `ExternalData` the integration built,
        already minimised, already flattened, already classified. Nothing is
        re-rendered -- a second rendering is a second place for the labelling
        to be forgotten.
        """
        if outcome is None:
            return "", 0
        external = (outcome.data or {}).get("external")
        if not isinstance(external, dict):
            return "", 0
        content = external.get("content")
        if not isinstance(content, str) or not content.strip():
            return "", 0
        count = sum(1 for line in content.split("\n") if line.startswith("["))
        return content, count


__all__ = ["MAIL_TOOL", "MailService"]
