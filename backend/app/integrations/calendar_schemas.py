"""What Mai keeps from a Google Calendar response, and nothing else.

Data minimisation is the whole of this module. A Calendar event carries far
more than a title and a time: attendee lists with email addresses, conference
join links with embedded credentials, private extended properties, attachment
URLs, recurrence rules, the organiser's account. None of that is needed to
answer "what's on my calendar tomorrow?", and all of it would otherwise reach
an external LLM.

So nothing is passed through. Each field is read by name, bounded, and
flattened, and the raw payload is discarded at the end of the call -- it is
never stored, never logged, and never handed to a caller.

Two axes, not one
-----------------

The stage brief asks that Calendar events not be marked as untrusted *web*
data, and that is right -- but it needs unpicking, because "trust" here means
two different things and Stage 4F-A already separated them.

    DataClassification   how private is this?      -> PRIVATE
    TrustLevel           may this direct Mai?      -> UNTRUSTED

On the first axis Calendar data is emphatically not web data: it is the
user's own schedule, `classification=PRIVATE`, from a service they explicitly
connected, and it carries a `source` of `google_calendar` rather than
`web_search`.

On the second axis it is **exactly** as untrusted as a web page, and for a
reason that is easy to miss: *anyone can put text into your calendar by
sending you an invitation.* An event title is authored by whoever created the
event, who may be a stranger. Calendar content is therefore attacker-
influenced by design, not despite it.

Stage 4F-A gave `TrustLevel` two values and refused a third, writing that a
middle category "would become the place where 'well, this source is fairly
reliable' gets written down, and that is the argument that ends with a web
page being obeyed". Adding `TRUSTED` for calendar data would be that argument,
made about the one content type an outsider can write into directly.

So: private on the sensitivity axis, untrusted on the instruction axis, and
flattened and fenced exactly as retrieved knowledge is. The brief's
requirement -- that Calendar data be distinguishable from web data and still
unable to become an instruction -- is met by both fields, rather than by
moving one of them.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.integrations.result import DataClassification, ExternalData

logger = get_logger(__name__)

#: Bounds on what one response may contribute. Each is a ceiling on prompt
#: budget as much as on memory.
MAX_EVENTS = 25
MAX_TITLE_CHARS = 200
MAX_LOCATION_CHARS = 120
MAX_ORGANISER_CHARS = 120

#: Fields Mai reads from a Google event. Everything else is dropped.
#:
#: Written out so the omissions are visible: `attendees`, `hangoutLink`,
#: `conferenceData`, `attachments`, `extendedProperties`, `recurrence`,
#: `iCalUID`, `htmlLink` and `creator` are all deliberately absent.
KEPT_FIELDS = ("summary", "start", "end", "location", "organizer", "status")


class CalendarEvent(BaseModel):
    """One event, reduced to what answering a question needs.

    Frozen and `extra="forbid"`: a field that is not named here cannot be
    added by a provider response, so a future Google field cannot arrive in
    Mai's state without someone deciding it should.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    #: ISO-8601 as Google returned it, or a date for an all-day event.
    starts_at: str = Field(default="", max_length=40)
    ends_at: str = Field(default="", max_length=40)
    all_day: bool = False
    location: str = Field(default="", max_length=MAX_LOCATION_CHARS)
    #: A display name where Google gave one, otherwise empty.
    #:
    #: **Never an email address.** An organiser's address is personal data
    #: about a third party who did not consent to Mai's LLM provider seeing
    #: it, and it is not needed to answer a scheduling question.
    organiser: str = Field(default="", max_length=MAX_ORGANISER_CHARS)

    @property
    def cancelled(self) -> bool:
        return False


class CalendarWindow(BaseModel):
    """The events found in one time window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: Tuple[CalendarEvent, ...] = ()
    #: What was asked for, echoed back so a caller can see the window that
    #: produced these results rather than inferring it.
    starts_at: str = ""
    ends_at: str = ""
    #: How many Google offered before bounding, so a truncated set is
    #: recognisable as truncated.
    total_available: int = 0

    def as_external_data(self) -> ExternalData:
        """Render as labelled personal data for the prompt.

        `PRIVATE` because it is the user's own schedule, and `UNTRUSTED`
        because an event title is written by whoever sent the invitation --
        see the module docstring. Each line is attributed and flattened so a
        title cannot forge structure.
        """
        lines: List[str] = []
        for index, event in enumerate(self.events, start=1):
            when = (
                f"{_flatten(event.starts_at)}"
                if event.all_day
                else f"{_flatten(event.starts_at)} to {_flatten(event.ends_at)}"
            )
            lines.append(f"[{index}] {_flatten(event.title) or '(no title)'}")
            lines.append(f"    When: {when}{' (all day)' if event.all_day else ''}")
            if event.location:
                lines.append(f"    Location: {_flatten(event.location)}")
            if event.organiser:
                lines.append(f"    Organiser: {_flatten(event.organiser)}")

        if not lines:
            lines.append("(no events in this window)")

        return ExternalData(
            source="google_calendar",
            content="\n".join(lines),
            # The user's own schedule. Private, and never PUBLIC.
            #
            # `trust_level` is not passed: `ExternalData` freezes it at
            # UNTRUSTED and excludes it from being set, which is the Stage
            # 4F-A guarantee this module deliberately does not try to relax.
            classification=DataClassification.PRIVATE,
        )


def parse_events(
    payload: Dict[str, Any], max_events: int = MAX_EVENTS
) -> Tuple[Tuple[CalendarEvent, ...], int]:
    """Extract events from a Google response, field by field.

    Never `CalendarEvent(**item)`. Reading by name is what keeps an unexpected
    or renamed Google field out of Mai's state, and what keeps the omissions
    above actually omitted.

    A cancelled event is dropped rather than reported: it is not on the
    calendar, and listing it would answer the user's question wrongly.
    """
    raw = payload.get("items")
    if not isinstance(raw, list):
        raw = []

    events: List[CalendarEvent] = []
    for item in raw:
        if len(events) >= min(max_events, MAX_EVENTS):
            break
        if not isinstance(item, dict):
            continue
        if item.get("status") == "cancelled":
            continue

        start, start_all_day = _time_of(item.get("start"))
        end, _ = _time_of(item.get("end"))
        if not start:
            # An event with no start cannot be placed in a day.
            continue

        events.append(
            CalendarEvent(
                title=_bounded(item.get("summary"), MAX_TITLE_CHARS),
                starts_at=start,
                ends_at=end,
                all_day=start_all_day,
                location=_bounded(item.get("location"), MAX_LOCATION_CHARS),
                organiser=_organiser_of(item.get("organizer")),
            )
        )

    dropped = len(raw) - len(events)
    if dropped > 0:
        logger.info(
            "Calendar events were bounded or dropped",
            # Counts only. Never a title, a location or an attendee.
            extra={"dropped": dropped, "kept": len(events)},
        )

    return tuple(events), len(raw)


def _time_of(value: Any) -> Tuple[str, bool]:
    """`{"dateTime": ...}` or `{"date": ...}` -> (value, is_all_day)."""
    if not isinstance(value, dict):
        return "", False
    moment = value.get("dateTime")
    if isinstance(moment, str) and moment:
        return moment[:40], False
    day = value.get("date")
    if isinstance(day, str) and day:
        return day[:40], True
    return "", False


def _organiser_of(value: Any) -> str:
    """A display name, never an address.

    Google returns `{"email": ..., "displayName": ...}`. Only the second is
    read: an organiser's email is personal data about someone who did not
    agree to Mai's LLM provider seeing it, and no scheduling question needs it.
    """
    if not isinstance(value, dict):
        return ""
    name = value.get("displayName")
    if isinstance(name, str) and name.strip():
        return _flatten(name)[:MAX_ORGANISER_CHARS]
    return ""


def _bounded(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _flatten(value)[:limit]


def _flatten(text: str) -> str:
    """Collapse to one line.

    A meeting title is written by whoever sent the invitation. Without this a
    title containing newlines could forge the structure of the block it is
    rendered into -- the same reason Stage 3B flattens a retrieved memory.
    """
    return " ".join((text or "").split())


__all__ = [
    "KEPT_FIELDS",
    "MAX_EVENTS",
    "CalendarEvent",
    "CalendarWindow",
    "parse_events",
]
