"""What a calendar turn produced. Application state, never model output."""

import enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CalendarOutcome(str, enum.Enum):
    """What the calendar layer did this turn.

    Explicit states, because "nothing happened" has several causes and each
    calls for a different sentence. Telling someone the integration is not
    connected when execution is switched off sends them to the wrong place.
    """

    NOT_CALENDAR = "not_calendar"
    #: Read successfully. `events_block` carries the rendered window.
    COMPLETED = "completed"
    #: The read was attempted and failed.
    FAILED = "failed"
    #: Execution is switched off for this deployment.
    DISABLED = "disabled"
    #: No Google OAuth client is configured.
    NOT_CONFIGURED = "not_configured"
    #: Configured, but no account has been connected yet.
    NOT_CONNECTED = "not_connected"
    #: The grant no longer covers what Mai needs, or was revoked.
    REAUTHORISATION_REQUIRED = "reauthorisation_required"
    #: A request to change the calendar. Mai has no such capability.
    WRITE_NOT_SUPPORTED = "write_not_supported"


class CalendarResult(BaseModel):
    """The calendar layer's report for one chat turn.

    Carries no token, no header, no endpoint and no scope -- a test pins the
    field set, so a future addition has to be a deliberate one.
    """

    model_config = ConfigDict(frozen=True)

    outcome: CalendarOutcome = CalendarOutcome.NOT_CALENDAR

    #: Application-written text to send instead of calling the model.
    reply: str = ""

    #: The rendered window, as labelled personal data. Only set on success.
    events_block: str = ""
    event_count: int = 0
    #: What the user asked about, in their own terms: "tomorrow", "Friday
    #: afternoon". Never a raw timestamp, which they did not type.
    window_label: str = Field(default="", max_length=60)

    #: An application reason code. Never a Google message, never an exception.
    reason: Optional[str] = Field(default=None, max_length=64)

    @property
    def has_reply(self) -> bool:
        return bool(self.reply)

    @property
    def needs_synthesis(self) -> bool:
        """Whether the model should answer this turn from the events."""
        return self.outcome is CalendarOutcome.COMPLETED


__all__ = ["CalendarOutcome", "CalendarResult"]
