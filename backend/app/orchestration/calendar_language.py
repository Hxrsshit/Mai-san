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

Two question shapes
-------------------

Stage 4F-G modelled one shape: *object-centric* questions, which name the
calendar and ask what is on it. "What's on my calendar tomorrow?" So every
family required a calendar noun, and the module said why -- requiring the noun
is what stops "I'm free tomorrow" from reading someone's schedule.

That was right about the danger and wrong about the coverage. The other half of
ordinary scheduling language is *subject-centric*: it asks about the person,
not the object. "Am I free tomorrow afternoon?" names no calendar, so no family
head could match it, and the question fell through to an ordinary turn where
the model answered that it could not see the calendar -- untrue, and exactly
the class of failure Stage 4E.1 exists to prevent.

Both shapes are now modelled, and the guard that the noun used to provide is
provided instead by **form**: a subject-centric family must be a question about
the user's own schedule, anchored at the start of the message, and must carry
an explicit time expression. "I wish I were free tomorrow" is a first-person
wish, not a question, and it matches nothing.
"""

import enum
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: How far ahead "what's next?" looks before giving up.
NEXT_HORIZON_DAYS = 14

#: Most events any recognised question may ask for.
MAX_RESULTS = 10

#: How far "the next few hours" reaches.
NEXT_HOURS = 4


class CalendarIntent(str, enum.Enum):
    """What the user wants done with the window, once it is read.

    Three, because they call for three different answers from three different
    amounts of data -- which is the point. An availability question is
    answered from *intervals*; it needs no event titles, so under this intent
    no title, location or organiser is sent to the model at all.
    """

    #: "What's on my calendar tomorrow?" -- wants the events.
    SCHEDULE = "calendar_schedule"
    #: "Am I free tomorrow afternoon?" -- wants free and busy periods.
    AVAILABILITY = "calendar_availability"
    #: "When is my next meeting?" -- wants the first one.
    NEXT_EVENT = "calendar_next_event"


# --- Grammar ----------------------------------------------------------------

#: The nouns that make a question about the calendar rather than about time.
_CALENDAR_NOUN = (
    r"(?:calendar|schedule|agenda|diary|meetings?|appointments?|events?|"
    r"bookings?)"
)

#: Words for an unoccupied period, for the availability families.
_FREE_WORD = r"(?:free|open|spare|available)"
_GAP_NOUN = r"(?:slots?|hours?|times?|gaps?|windows?|spaces?|moments?)"

#: Openers that carry no meaning of their own.
#:
#: The comma matters: "Hey, am I free tomorrow?" is the same question as "Hey
#: am I free tomorrow?", and the subject-centric families are anchored at the
#: start of the message, so an unconsumed opener stops them matching at all.
#: Openers that carry no meaning of their own.
#:
#: The comma matters: "Hey, am I free tomorrow?" is the same question as "Hey
#: am I free tomorrow?", and the subject-centric families are anchored at the
#: start of the message, so an unconsumed opener stops them matching at all.
#:
#: **At most one, never a repetition.** Written `*` this group made every
#: pattern here catastrophically backtrack: "so so so so ... x" took longer
#: than a test run to fail, because a repeated alternation over a
#: non-matching tail explores exponentially many splits. A user's message is
#: attacker-controlled text on a path with no timeout, so that is a denial of
#: service, not a performance note. One optional opener covers every real
#: phrasing and cannot backtrack.
#: Qualifiers people put between the possessive and the noun.
#:
#: "my google calendar", "the work calendar", "my personal diary". Without
#: this, "what's on my google calendar tomorrow?" matched no family at all --
#: the pattern wanted the possessive adjacent to the noun. Stage 5A found it
#: while chasing a typo report; the qualifier, not the spelling, was what the
#: grammar could not see.
#:
#: A closed list. It admits an adjective, never an arbitrary word, so
#: "what's on my urgent-request calendar" does not quietly become a request
#: about something else.
_CALENDAR_QUALIFIER = (
    r"(?:google|gmail|outlook|apple|icloud|office|work|personal|team|"
    r"shared|main|primary)"
)

#: The noun, with an optional qualifier in front of it.
_CALENDAR_PHRASE = rf"(?:{_CALENDAR_QUALIFIER}\s+)?{_CALENDAR_NOUN}"

_LEAD = (
    r"(?:(?:can|could|would|will)\s+you\s*,?\s*|"
    r"(?:please|hey|hi|hello|ok|okay|so|and|also)\s*,?\s*)?"
)


class _Family(NamedTuple):
    """One request shape."""

    name: str
    intent: CalendarIntent
    pattern: "re.Pattern"
    #: Whether the message must carry an explicit time expression.
    #:
    #: Object-centric questions may omit one -- "what's on my calendar?" means
    #: today, which is what anyone means. Subject-centric ones may not: "am I
    #: free?" is genuinely ambiguous between "right now" and "at all today",
    #: and guessing reads a window the user did not ask for. It asks instead.
    time_required: bool = False


#: Request families, tried in order.
#:
#: Availability comes first so that a question which is both -- "do I have a
#: free slot tomorrow?" -- is answered as the availability question it is.
_FAMILIES: Tuple[_Family, ...] = (
    # --- Subject-centric: about the person -------------------------------
    #
    # Every one of these is anchored at the start and requires an explicit
    # time. Anchoring is what separates a question from a remark: "am I free
    # tomorrow?" opens the sentence, "I wish I were free tomorrow" does not.
    _Family(
        "am_i_free",
        CalendarIntent.AVAILABILITY,
        re.compile(
            rf"^{_LEAD}(?:am|are)\s+i\s+"
            rf"(?:{_FREE_WORD}|busy|booked|occupied|around|tied\s+up)\b"
            rf"(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    _Family(
        "when_am_i_free",
        CalendarIntent.AVAILABILITY,
        re.compile(
            rf"^{_LEAD}when\s+(?:am|are)\s+i\s+(?:{_FREE_WORD}|around)\b"
            rf"(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    _Family(
        "free_slot",
        CalendarIntent.AVAILABILITY,
        re.compile(
            rf"^{_LEAD}(?:do\s+i\s+have|have\s+i\s+got|is\s+there|are\s+there)\s+"
            rf"(?:an?y?\s+)?{_FREE_WORD}\s+{_GAP_NOUN}\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    _Family(
        "find_free",
        CalendarIntent.AVAILABILITY,
        re.compile(
            rf"^{_LEAD}(?:find|get|suggest)\s+(?:me\s+)?(?:an?y?\s+)?"
            rf"{_FREE_WORD}\s+{_GAP_NOUN}\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    _Family(
        "how_busy",
        CalendarIntent.AVAILABILITY,
        re.compile(
            rf"^{_LEAD}how\s+(?:busy|packed|full|booked|free)\s+"
            rf"(?:is|does)\s+(?:my|the)\s+{_CALENDAR_PHRASE}\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    _Family(
        "what_am_i_doing",
        CalendarIntent.SCHEDULE,
        # Subject-centric in form, but it asks *what*, so it wants the events.
        re.compile(
            rf"^{_LEAD}what\s+(?:am\s+i|have\s+i\s+got)\s+"
            rf"(?:doing|up\s+to|on)\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    # --- Object-centric: about the calendar -------------------------------
    #
    # "What's on my calendar tomorrow?", "What's my schedule this afternoon?",
    # "What's happening on my calendar this week?"
    #
    # The possessive carries the weight when there is no preposition: "what's
    # *my* schedule" is a request, "what is *the* calendar" is a question
    # about the word.
    _Family(
        "whats_on",
        CalendarIntent.SCHEDULE,
        re.compile(
            rf"{_LEAD}what(?:'|’)?s?\s+(?:is\s+)?"
            rf"(?:(?:happening|going\s+on|planned|scheduled|booked|"
            rf"coming\s+up|left)\s+)?"
            rf"(?:(?:on|in|for)\s+(?:my|the)|my)\s+{_CALENDAR_PHRASE}\b"
            rf"(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "What meetings do I have today?", "Do I have any events tomorrow?",
    # "What does my calendar look like tomorrow?"
    _Family(
        "do_i_have",
        CalendarIntent.SCHEDULE,
        re.compile(
            rf"{_LEAD}(?:what|which|any|do\s+i\s+have|have\s+i\s+got)\s*"
            rf"(?:\w+\s+){{0,2}}?{_CALENDAR_PHRASE}\s*"
            rf"(?:do\s+i\s+have|have\s+i\s+got|are\s+there|do\s+i\s+got|"
            rf"look\s+like)?"
            rf"(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Do I have anything scheduled Friday afternoon?"
    _Family(
        "anything_scheduled",
        CalendarIntent.SCHEDULE,
        re.compile(
            rf"{_LEAD}(?:do\s+i\s+have|is\s+there)\s+(?:anything|something)\s+"
            rf"(?:scheduled|booked|planned|on|going\s+on)\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Do I have anything tomorrow afternoon?" -- the same question with the
    # qualifier left out, which is why the time expression becomes required.
    # Without that, "do I have anything to declare?" would read a calendar.
    _Family(
        "anything_when",
        CalendarIntent.SCHEDULE,
        re.compile(
            rf"^{_LEAD}(?:do\s+i\s+have|have\s+i\s+got|is\s+there)\s+"
            rf"(?:anything|something)\b(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
        time_required=True,
    ),
    # "When is my next meeting?", "What's my next appointment?"
    _Family(
        "next_event",
        CalendarIntent.NEXT_EVENT,
        re.compile(
            rf"{_LEAD}(?:when\s+is|what(?:'|’)?s?\s+(?:is\s+)?|when(?:'|’)?s)\s+"
            rf"(?:my|the)\s+next\s+{_CALENDAR_PHRASE}(?P<when>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Show me my calendar for tomorrow."
    _Family(
        "show_calendar",
        CalendarIntent.SCHEDULE,
        re.compile(
            rf"{_LEAD}(?:(?:show|tell|give)\s+me\s+(?:my|the)|"
            rf"(?:check|see|open|pull\s+up|look\s+at)\s+(?:my|the))\s+"
            rf"{_CALENDAR_PHRASE}(?P<when>.*)",
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
#: Definitional questions: about the word, not about the schedule.
#:
#: "What is a free/busy calendar?" and "What does free time mean?" both name
#: calendars and both are about vocabulary. The article is the tell -- nobody
#: says "what is *a* calendar" about their own.
_DEFINITIONAL = re.compile(
    r"\b(?:what\s+(?:does|do)\b[^?]{0,40}\bmean\b|"
    r"what\s+(?:is|are)\s+(?:a|an)\b|"
    r"what(?:'|’)?s\s+(?:a|an)\b|"
    r"the\s+(?:meaning|definition)\s+of)\b",
    re.IGNORECASE,
)
#: A remark about the past, or a wish. Neither is a request.
_FIRST_PERSON_PAST = re.compile(
    r"\bi\s+(?:already\s+)?(?:checked|looked|saw|cancelled|deleted|had|"
    r"attended|missed|went)\b",
    re.IGNORECASE,
)
_WISH = re.compile(
    r"\b(?:i\s+wish|i'?d\s+love|if\s+only|i\s+hope|wish\s+i)\b",
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

    Four outcomes: not a calendar question, a *write* request that must be
    refused, a readable window, or a request whose time could not be resolved
    and which must be asked about rather than guessed at.
    """

    family: str = ""
    intent: Optional[CalendarIntent] = None
    starts_at: str = ""
    ends_at: str = ""
    #: A human description of the window, for the reply. Never a date format
    #: the user did not use.
    window_label: str = ""
    max_results: int = MAX_RESULTS
    is_write_request: bool = False
    #: Recognised as a calendar question whose time is missing or unresolved.
    needs_clarification: bool = False

    @property
    def is_request(self) -> bool:
        return bool(self.family) or self.is_write_request

    @property
    def is_readable(self) -> bool:
        return bool(self.family and self.starts_at and self.ends_at)

    @property
    def is_availability(self) -> bool:
        return self.intent is CalendarIntent.AVAILABILITY


def recognise(
    message: str,
    now: Optional[datetime] = None,
    tz=None,
) -> CalendarRequest:
    """Read one message. Never raises; not-a-request is the default.

    `tz` is the zone the window is built in. Passed by the caller rather than
    read here, so this module keeps no dependency on settings and a test can
    state the zone it means.
    """
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

    if (
        _NEGATION.search(text)
        or _EXPLANATORY.search(text)
        or _DEFINITIONAL.search(text)
        or _FIRST_PERSON_PAST.search(text)
        or _WISH.search(text)
    ):
        return CalendarRequest()

    moment = now or datetime.now(timezone.utc)
    zone = tz or moment.tzinfo or timezone.utc
    local = moment.astimezone(zone)

    for family in _FAMILIES:
        match = family.pattern.search(text)
        if match is None:
            continue

        when = (match.group("when") or "").strip()

        if family.intent is CalendarIntent.NEXT_EVENT:
            end = local + timedelta(days=NEXT_HORIZON_DAYS)
            return CalendarRequest(
                family=family.name,
                intent=family.intent,
                starts_at=local.isoformat(),
                ends_at=end.isoformat(),
                window_label="the next couple of weeks",
                max_results=MAX_RESULTS,
            )

        window = _resolve_when(when, local, zone)
        if window is None:
            if family.time_required and not _looks_temporal(when):
                # The head matched but the sentence is not about time --
                # "am I free to speak my mind?". Not this family, and not a
                # calendar question.
                continue
            if family.time_required:
                # Recognised, but the window is not knowable. Asking is the
                # only safe answer: defaulting would read a period the user
                # never named, and a calendar read is private data.
                return CalendarRequest(
                    family=family.name,
                    intent=family.intent,
                    needs_clarification=True,
                )
            window = _day_window(local.date(), zone, _part_of_day(""), "today")

        starts_at, ends_at, label = window
        return CalendarRequest(
            family=family.name,
            intent=family.intent,
            starts_at=starts_at,
            ends_at=ends_at,
            window_label=label,
            max_results=MAX_RESULTS,
        )

    return CalendarRequest()


# --- Temporal resolution ----------------------------------------------------

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
#:
#: Morning starts at 06:00 rather than midnight. Stage 4F-G used 00:00, which
#: is defensible for "what's on tomorrow morning" and absurd for "am I free
#: tomorrow morning" -- the honest answer to which is not "yes, from midnight".
_DAY_PARTS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (17, 24),
    "night": (17, 24),
    "tonight": (17, 24),
}


#: Vocabulary that makes a trailing phrase a *time* phrase.
#:
#: Used to tell three cases apart, which matters because two of them look
#: identical to a pattern match:
#:
#:     "am I free tomorrow?"       resolves            -> read the calendar
#:     "am I free?"                nothing to resolve  -> ask which day
#:     "am I free to speak?"       not about time      -> not a calendar question
#:
#: Without this, the third became the second: "what am I doing wrong?" and "do
#: I have anything to declare?" were answered with "which day do you mean?".
#: The head matched, so only the *complement* can say the sentence is about
#: something else.
_TEMPORAL_HINT = re.compile(
    r"\b(?:today|tomorrow|tonight|yesterday|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|night|noon|midnight|"
    r"week|weekend|month|weekday|"
    r"later|soon|now|currently|upcoming|"
    r"hours?|days?|minutes?|"
    r"next|coming|rest\s+of|"
    r"\d{1,2}\s*(?:am|pm)|\d{1,2}:\d{2}|o'?clock)\b",
    re.IGNORECASE,
)


def _looks_temporal(when: str) -> bool:
    """Whether a trailing phrase is about time at all.

    An empty remainder counts: "am I free?" is a calendar question with the
    day left out, which is worth asking about. A non-empty remainder with no
    temporal vocabulary does not -- that is a different sentence that happens
    to start the same way.
    """
    stripped = re.sub(r"[^\w\s]", "", when or "").strip()
    if not stripped:
        return True
    return bool(_TEMPORAL_HINT.search(when))


def _resolve_when(when: str, local: datetime, zone):
    """Resolve a time expression, or None when there is not one.

    Returning None rather than a default is what lets the caller distinguish
    "they said today" from "they said nothing" -- which is the difference
    between answering and asking.

    A model is never consulted. A window is the whole of what the read will
    cover, so choosing it is choosing how much of the user's calendar is read.
    """
    lowered = (when or "").lower()

    # Bounded, and first: "the next few hours" contains no day word.
    if re.search(r"\b(?:next|coming)\s+(?:few\s+|couple\s+of\s+)?hours?\b", lowered):
        return (
            local.isoformat(),
            (local + timedelta(hours=NEXT_HOURS)).isoformat(),
            "the next few hours",
        )

    if re.search(r"\b(?:right\s+now|at\s+the\s+moment|currently)\b", lowered):
        return (
            local.isoformat(),
            (local + timedelta(hours=1)).isoformat(),
            "right now",
        )

    if re.search(r"\blater\s+(?:today|on)\b", lowered):
        end_of_day = datetime.combine(
            local.date(), time(0, 0), tzinfo=zone
        ) + timedelta(days=1)
        return local.isoformat(), end_of_day.isoformat(), "later today"

    # "next week" before the weekday scan, so "next week" is not read as a
    # bare weekday, and before "this week" so the longer phrase wins.
    if re.search(r"\bnext\s+week\b", lowered):
        # The seven days beginning the coming Monday.
        ahead = (7 - local.weekday()) or 7
        start_day = (local + timedelta(days=ahead)).date()
        start = datetime.combine(start_day, time(0, 0), tzinfo=zone)
        return start.isoformat(), (start + timedelta(days=7)).isoformat(), "next week"

    for phrase, offset in sorted(
        _RELATIVE_DAYS.items(), key=lambda item: -len(item[0])
    ):
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            day = (local + timedelta(days=offset)).date()
            part = _part_of_day(lowered)
            return _day_window(day, zone, part, phrase)

    for name, index in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            ahead = (index - local.weekday()) % 7
            # "on Friday" said on a Friday means the coming one, not today.
            ahead = ahead or 7
            day = (local + timedelta(days=ahead)).date()
            part = _part_of_day(lowered)
            return _day_window(day, zone, part, name.capitalize())

    if re.search(r"\b(?:this\s+week|the\s+week)\b", lowered):
        return (
            local.isoformat(),
            (local + timedelta(days=7)).isoformat(),
            "this week",
        )

    # "this afternoon", "this evening" -- a part of today, with no day word.
    part = _part_of_day(lowered)
    if part is not None:
        name, _ = part
        return _day_window(local.date(), zone, part, "this")

    return None


def _part_of_day(lowered: str):
    for name, bounds in _DAY_PARTS.items():
        if re.search(rf"\b{name}\b", lowered):
            return name, bounds
    return None


def _day_window(day: date, zone, part, label: str):
    """A whole day, or a named part of one."""
    zone = zone or timezone.utc
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


def resolve_window(text: str, now: Optional[datetime] = None, tz=None):
    """Resolve a time expression anywhere in `text`, or None.

    The public face of this module's temporal engine, so Stage 4H composes a
    briefing window with the same code that resolves "tomorrow afternoon" for
    a bare calendar question. Two date engines would drift, and the drift
    would show up as a briefing covering a different day from the one the same
    words produce elsewhere -- which nobody would notice until it mattered.

    Returns `(starts_at, ends_at, label)` or None. Never raises.
    """
    if not text or not text.strip():
        return None
    trimmed = " ".join(text.split())[:1000]
    moment = now or datetime.now(timezone.utc)
    zone = tz or moment.tzinfo or timezone.utc
    try:
        return _resolve_when(trimmed, moment.astimezone(zone), zone)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve a time expression")
        return None


def known_families() -> Tuple[str, ...]:
    return tuple(family.name for family in _FAMILIES)


def known_intents() -> Tuple[str, ...]:
    return tuple(intent.value for intent in CalendarIntent)


__all__ = [
    "MAX_RESULTS",
    "NEXT_HORIZON_DAYS",
    "NEXT_HOURS",
    "CalendarIntent",
    "CalendarRequest",
    "known_families",
    "known_intents",
    "resolve_window",
    "recognise",
]
