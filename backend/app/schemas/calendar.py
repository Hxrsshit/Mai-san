"""What a client is told about a calendar turn.

Deliberately small, and deliberately carrying **no event content**. A client
learns that a read happened and how many events were found; the events
themselves are private personal data that reached the model to answer the
question, and handing them to a client as structured data would invite a UI to
render them as though Mai had said them.

Absent by design: the access token, the window timestamps, the scope, the
execution id, and the events.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.calendar.schemas import CalendarOutcome, CalendarResult


class CalendarRead(BaseModel):
    """One turn's calendar state, as the wire sees it."""

    model_config = ConfigDict(frozen=True)

    outcome: CalendarOutcome
    event_count: int = 0
    #: The user's own words for the window: "tomorrow", "Friday afternoon".
    window_label: str = Field(default="", max_length=60)
    reason: Optional[str] = None

    @classmethod
    def from_result(cls, result: CalendarResult) -> Optional["CalendarRead"]:
        """None when the turn had nothing to do with the calendar."""
        if result is None or result.outcome is CalendarOutcome.NOT_CALENDAR:
            return None
        return cls(
            outcome=result.outcome,
            event_count=result.event_count,
            window_label=result.window_label,
            reason=result.reason,
        )


__all__ = ["CalendarRead"]
