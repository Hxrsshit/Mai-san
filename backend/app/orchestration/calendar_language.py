"""Recognising a calendar question, and turning it into a time window.

The same shape as Stage 4F-F.1's research grammar, and for the same reasons: a
small set of request families with guards against mentions, no model call, and
a deterministic result the application can build authorization on.

What is different is the output. A research request yields a *subject*; a
calendar question yields a **time window** -- two RFC-3339 timestamps computed
here, from the application's clock, in the user's configured timezone. A model
is never asked what "tomorrow" means, because a model that could choose the
window could choose to read a different part of the calendar than the one the
question asked about.

Recognition is not permission. What comes out is a candidate; Stage 4C
authorization, the integration's connection state and the Stage 4E dispatcher
all still stand between it and a request to Google.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: How far ahead "what's next?" looks before giving up.
NEXT_HORIZON_DAYS = 14

#: Most events any recognised question may ask for.
MAX_RESULTS = 10

# --- Grammar ----------------------------------------------------------------

#: The nouns that make a question about the calendar rather than about time.
_CALENDAR_NOUN = (
    r"(?:calendar|schedule|agenda|diary|meetings?|appointments?|events?|"
    r"bookings?)"
)

_LEAD = r"(?:(?:can|could|would|will)\s+you\s+|please\s+|hey\s+)?"

#: Day words, mapped to an offset from today.
_RELATIVE_DAYS = {
    "today": 0,
    "tonight": 0,
    "tomorrow": 1,
    "the day after tomorrow": 2,
    "yesterday": -1,
}

#: Weekday names, for "on Friday".
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

#: Parts of a day, as (start hour, end hour).
_DAY_PARTS = {
    "morning": (0, 12),
    "afternoon": (12, 18),
    "evening": (17, 24),
    "tonight": (17, 24),
}

#: Request families. Each must name the calendar *and* ask something of it.
#:
#: Requiring the noun is what keeps "I'm free tomorrow" and "tomorrow is
#: busy" from reading the user's calendar: a statement about a day is not a
#: question about a schedule.
_FAMILIES = (
    # "What's on my calendar tomorrow?", "What's on my schedule for Friday?"
    (
        "whats_on",
        re.compile(
            rf"{_LEAD}what(?:'|’)?s?\s+(?:is\s+)?(?:on|in)\s+(?:my|the)\s+"
            rf"{_CALENDAR_NOUN}\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "What meetings do I have today?", "Do I have any events tomorrow?"
    (
        "do_i_have",
        re.compile(
            rf"{_LEAD}(?:what|which|any|do\s+i\s+have|have\s+i\s+got)\s*"
            rf"(?:\w+\s+){{0,2}}?{_CALENDAR_NOUN}\s*"
            rf"(?:do\s+i\s+have|have\s+i\s+got|are\s+there|do\s+i\s+got)?"
            rf"(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Do I have anything scheduled Friday afternoon?"
    (
        "anything_scheduled",
        re.compile(
            rf"{_LEAD}(?:do\s+i\s+have|is\s+there)\s+(?:anything|something)\s+"
            rf"(?:scheduled|booked|planned|on)\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "When is my next meeting?", "What's my next appointment?"
    (
        "next_event",
        re.compile(
            rf"{_LEAD}(?:when\s+is|what(?:'|’)?s?\s+(?:is\s+)?|when(?:'|’)?s)\s+"
            rf"(?:my|the)\s+next\s+{_CALENDAR_NOUN}(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Show me my calendar for tomorrow."
    (
        "show_calendar",
        re.compile(
            rf"{_LEAD}(?:show|tell)\s+me\s+(?:my|the)\s+{_CALENDAR_NOUN}"
            rf"(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

#: Text that discusses calendars rather than asking about one.
_NEGATION = re.compile(
    r"\b(?:don'?t|do\s+not|cannot|can'?t|won'?t|never|without|no\s+need)\b",
    re.IGNORECASE,
)
_EXPLANATORY = re.compile(
    r"\b(?:how\s+(?:do|does|to|would|can)|explain|what\s+is\s+a\s+calendar|"
    r"is\s+an?\s+\w+\s+concept|why\s+(?:do|does|is|are|can))\b",
    re.IGNORECASE,
)
_FIRST_PERSON_PAST = re.compile(
    r"\bi\s+(?:already\s+)?(?:checked|looked|saw|cancelled|deleted)\b",
    re.IGNORECASE,
)

#: A request to *change* the calendar. Recognised so it can be refused
#: truthfully, never so it can be performed -- no write capability exists.
#: Nouns that make a write verb a *calendar* write verb.
#:
#: Wider than `_CALENDAR_NOUN`, which governs reads: "schedule a call" is a
#: calendar request and "what calls do I have" is not, so "call" belongs here
#: and not there.
_WRITE_OBJECT = (
    r"(?:meetings?|events?|appointments?|calls?|calendars?|schedules?|"
    r"invites?|bookings?|reminders?|slots?)"
)

#: Both halves required: an imperative verb *and* a calendar object.
#:
#: The verb must open the sentence, because unanchored "What's on my schedule
#: for the team meeting?" reads as a write -- "schedule" is also a noun.
#:
#: And the object must be a calendar one, because anchoring alone made
#: "Delete all my memories" and "Create a business plan" into calendar write
#: requests. Both are imperatives with a write verb and nothing to do with a
#: calendar; the existing intent and planning security suites caught it
#: immediately, which is exactly what those suites are for.
_WRITE_REQUEST = re.compile(
    rf"^{_LEAD}(?:create|add|schedule|book|set\s+up|make|move|reschedule|"
    rf"cancel|delete|remove|update|change|invite|put)\b[^.?!]{{0,60}}?"
    rf"\b{_WRITE_OBJECT}\b",
    re.IGNORECASE,
)


class CalendarRequest(NamedTuple):
    """A recognised calendar question, resolved to a window.

    Three outcomes, as in the research grammar: not a calendar question, a
    *write* request that must be refused, or a readable window.
    """

    family: str = ""
    starts_at: str = ""
    ends_at: str = ""
    #: A human description of the window, for the reply. Never a date format
    #: the user did not use.
    window_label: str = ""
    max_results: int = MAX_RESULTS
    is_write_request: bool = False

    @property
    def is_request(self) -> bool:
        return bool(self.family) or self.is_write_request

    @property
    def is_readable(self) -> bool:
        return bool(self.family and self.starts_at and self.ends_at)


def recognise(message: str, now: Optional[datetime] = None) -> CalendarRequest:
    """Read one message. Never raises; not-a-request is the default."""
    if not message or not message.strip():
        return CalendarRequest()

    text = " ".join(message.split())
    if len(text) > 1000:
        return CalendarRequest()

    if _WRITE_REQUEST.search(text):
        # Recognised so Mai can say plainly that it cannot do this, rather
        # than answering a create request with a list of events. No write
        # capability exists anywhere; this only shapes the reply.
        return CalendarRequest(is_write_request=True)

    if _NEGATION.search(text) or _EXPLANATORY.search(text) or _FIRST_PERSON_PAST.search(text):
        return CalendarRequest()

    for family, pattern in _FAMILIES:
        match = pattern.search(text)
        if match is None:
            continue

        when = (match.group("when") or "").strip()
        window = _window_for(when, family, now or datetime.now(timezone.utc))
        if window is None:
            continue

        starts_at, ends_at, label = window
        return CalendarRequest(
            family=family,
            starts_at=starts_at,
            ends_at=ends_at,
            window_label=label,
            max_results=MAX_RESULTS,
        )

    return CalendarRequest()


def _window_for(when: str, family: str, now: datetime):
    """Resolve the time expression to a window. Application clock only.

    A model is never asked what "tomorrow" means. A window is the whole of
    what the read will cover, so choosing it is choosing how much of the
    user's calendar is read -- not a decision to delegate.
    """
    lowered = (when or "").lower()

    if family == "next_event":
        # From now to the horizon; the caller takes the first result.
        end = now + timedelta(days=NEXT_HORIZON_DAYS)
        return now.isoformat(), end.isoformat(), "the next couple of weeks"

    for phrase, offset in sorted(
        _RELATIVE_DAYS.items(), key=lambda item: -len(item[0])
    ):
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            day = (now + timedelta(days=offset)).date()
            part = _part_of_day(lowered)
            return _day_window(day, now.tzinfo, part, phrase)

    for name, index in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            ahead = (index - now.weekday()) % 7
            # "on Friday" said on a Friday means the coming one, not today.
            ahead = ahead or 7
            day = (now + timedelta(days=ahead)).date()
            part = _part_of_day(lowered)
            return _day_window(day, now.tzinfo, part, name.capitalize())

    if re.search(r"\b(?:this\s+week|the\s+week)\b", lowered):
        start = now
        end = now + timedelta(days=7)
        return start.isoformat(), end.isoformat(), "this week"

    # No time expression at all. "What's on my calendar?" means today.
    return _day_window(now.date(), now.tzinfo, _part_of_day(lowered), "today")


def _part_of_day(lowered: str):
    for name, bounds in _DAY_PARTS.items():
        if re.search(rf"\b{name}\b", lowered):
            return name, bounds
    return None


def _day_window(day: date, tzinfo, part, label: str):
    """A whole day, or a named part of one."""
    zone = tzinfo or timezone.utc
    if part is None:
        start = datetime.combine(day, time(0, 0), tzinfo=zone)
        end = start + timedelta(days=1)
        return start.isoformat(), end.isoformat(), label

    name, (from_hour, to_hour) = part
    start = datetime.combine(day, time(from_hour, 0), tzinfo=zone)
    end = (
        datetime.combine(day, time(0, 0), tzinfo=zone) + timedelta(days=1)
        if to_hour >= 24
        else datetime.combine(day, time(to_hour, 0), tzinfo=zone)
    )
    # "tonight" is both a day and a part of one, so the two would otherwise
    # read as "tonight tonight".
    described = label if label.lower() == name else f"{label} {name}"
    return start.isoformat(), end.isoformat(), described.strip()


def known_families() -> Tuple[str, ...]:
    return tuple(name for name, _ in _FAMILIES)


__all__ = [
    "MAX_RESULTS",
    "NEXT_HORIZON_DAYS",
    "CalendarRequest",
    "known_families",
    "recognise",
]
