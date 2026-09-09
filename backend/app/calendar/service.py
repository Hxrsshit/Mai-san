"""Reading the calendar from a chat turn.

Shorter than the research service, and the difference is the whole of §8's
decision: there is no proposal and no confirmation turn. The user granted read
access in Google's own consent screen, the read changes nothing, and a prompt
after every "what's on my calendar?" would train someone to confirm without
reading.

What that does *not* mean is that the read is ungated. Four things must hold:
execution switched on, the integration connected, Stage 4C authorization
permitting the tool, and the deterministic recogniser actually identifying a
calendar question. The reasoning, and the argument against it, are recorded in
`app.tools.catalog._declare_calendar_read`.

No model is consulted here. Recognition is a grammar, the window is computed
from the application clock, and every refusal is application-written text.
"""

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.calendar.schemas import CalendarOutcome, CalendarResult
from app.execution.errors import ExecutionError
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.integrations.base import IntegrationState
from app.orchestration import calendar_language

logger = get_logger(__name__)

#: The tool this service may run. Exactly one, and read-only.
CALENDAR_TOOL = "calendar_list_events"

#: Reason codes that mean "reconnect", mapped to what to tell the user.
_REPLIES = {
    "calendar_not_connected": (
        "I can read your Google Calendar, but no account is connected yet. "
        "Connect one and I'll be able to answer that."
    ),
    "calendar_reauthorisation_required": (
        "My access to your Google Calendar has expired or been revoked. "
        "Reconnecting the account will restore it."
    ),
    "calendar_insufficient_scope": (
        "My access to your Google Calendar does not cover reading events. "
        "Reconnecting the account will fix that."
    ),
    "calendar_rate_limited": (
        "Google is rate limiting calendar requests at the moment, so I "
        "couldn't read your calendar. Nothing was retrieved."
    ),
}

_DEFAULT_FAILURE = (
    "I couldn't read your calendar just then. Nothing was retrieved."
)


class CalendarService:
    """Answers calendar questions by reading a bounded window."""

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

    async def handle(
        self, conversation_id: uuid.UUID, message: str
    ) -> CalendarResult:
        """Examine one turn. Never raises; degrades to NOT_CALENDAR."""
        try:
            return await self._handle(conversation_id, message)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Calendar handling failed; continuing as an ordinary turn",
                extra={"conversation_id": str(conversation_id)},
            )
            return CalendarResult(outcome=CalendarOutcome.NOT_CALENDAR)

    async def _handle(
        self, conversation_id: uuid.UUID, message: str
    ) -> CalendarResult:
        request = calendar_language.recognise(message)

        if request.is_write_request:
            # Recognised only so Mai can say plainly that it cannot. No write
            # capability exists in the catalogue, the executable registry or
            # the OAuth scope, so there is nothing here to refuse *access* to
            # -- this shapes the sentence, not the permission.
            return CalendarResult(
                outcome=CalendarOutcome.WRITE_NOT_SUPPORTED,
                reply=(
                    "I can only read your calendar — I can't create, change "
                    "or cancel events. My access to Google Calendar is "
                    "read-only, so nothing was changed."
                ),
            )

        if not request.is_readable:
            return CalendarResult(outcome=CalendarOutcome.NOT_CALENDAR)

        if not self._settings.EXECUTION_ENABLED:
            return CalendarResult(
                outcome=CalendarOutcome.DISABLED,
                reply=(
                    "I can't read your calendar right now: action execution "
                    "is switched off for this deployment."
                ),
            )

        unavailable = self._unavailable_reply()
        if unavailable is not None:
            return unavailable

        return await self._read(conversation_id, request)

    async def _read(self, conversation_id, request) -> CalendarResult:
        """Run the read through the ordinary execution path.

        No second client and no direct integration call: this creates an
        execution, approves it -- which is a formality for a tool that
        requires no approval, and still records the lifecycle -- and lets the
        Stage 4E dispatcher reach the integration.
        """
        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name=CALENDAR_TOOL,
                    arguments={
                        "starts_at": request.starts_at,
                        "ends_at": request.ends_at,
                        "max_results": request.max_results,
                    },
                    idempotency_key=(
                        f"cal:{conversation_id}:{request.starts_at}"
                        f":{request.ends_at}"
                    )[:128],
                ),
                conversation_id=conversation_id,
            )
            await self._executions.approve(execution.id)
            execution, outcome = await self._executions.run_returning_outcome(
                execution.id
            )
        except ExecutionError as refusal:
            logger.info(
                "Calendar read refused",
                extra={"reason": refusal.reason},
            )
            return self._failure(refusal.reason)

        if execution.state is not ExecutionState.SUCCEEDED or outcome is None:
            return self._failure(execution.error_code or "calendar_failed")

        block, count = self._rendered(outcome)
        return CalendarResult(
            outcome=CalendarOutcome.COMPLETED,
            events_block=block,
            event_count=count,
            window_label=request.window_label,
        )

    # --- Internals ----------------------------------------------------------

    def _unavailable_reply(self) -> Optional[CalendarResult]:
        """Why the integration cannot be used, if it cannot.

        Three distinct answers. "Not configured" is an operator problem,
        "not connected" is a one-click user action, and "reauthorisation" is a
        different user action -- collapsing them would send someone to the
        wrong place.
        """
        integration = self._integration()
        if integration is None:
            return CalendarResult(
                outcome=CalendarOutcome.NOT_CONFIGURED,
                reply=(
                    "I don't have a Google Calendar integration configured "
                    "for this instance."
                ),
            )

        state = integration.state()
        if state is IntegrationState.NOT_CONFIGURED:
            return CalendarResult(
                outcome=CalendarOutcome.NOT_CONFIGURED,
                reply=(
                    "Google Calendar isn't configured for this instance, so "
                    "I can't read your calendar."
                ),
            )
        if state is IntegrationState.AUTHENTICATION_REQUIRED:
            return CalendarResult(
                outcome=CalendarOutcome.NOT_CONNECTED,
                reply=_REPLIES["calendar_not_connected"],
            )
        if state is not IntegrationState.AVAILABLE:
            return CalendarResult(
                outcome=CalendarOutcome.FAILED,
                reason=state.value,
                reply=_DEFAULT_FAILURE,
            )
        return None

    def _integration(self):
        registry = self._integrations
        if registry is None:
            from app.integrations.registry import get_integration_registry

            registry = get_integration_registry()
        return registry.get("google_calendar")

    def _failure(self, reason: str) -> CalendarResult:
        outcome = (
            CalendarOutcome.REAUTHORISATION_REQUIRED
            if reason in (
                "calendar_reauthorisation_required",
                "calendar_insufficient_scope",
            )
            else CalendarOutcome.FAILED
        )
        return CalendarResult(
            outcome=outcome,
            reason=reason,
            reply=_REPLIES.get(reason, _DEFAULT_FAILURE),
        )

    @staticmethod
    def _rendered(outcome):
        """Pull the rendered window out of the tool's outcome.

        Reused shape from the research service: the content arrives as the
        `ExternalData` the integration built, already minimised, already
        flattened, already classified. Nothing is re-rendered -- a second
        rendering is a second place for the labelling to be forgotten.
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


__all__ = ["CALENDAR_TOOL", "CalendarService"]
