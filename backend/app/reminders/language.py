"""Deterministic schedule parsing for reminders.

The model may help Mai understand that a message *is* a reminder request. It
does not get to say when the reminder fires. This module turns the user's own
words into a typed `ParsedSchedule`, or refuses -- and refusing is a first-class
outcome, because a reminder set for the wrong time is worse than no reminder.

Two rules shape it:

**No guessing.** "Remind me later", "remind me sometime next week", "remind me
at 5" with no day context are all refused rather than resolved to a plausible
instant. Mai asks. A misparsed schedule is silent until the moment it is too
late to matter.

**A closed recurrence vocabulary.** `once`, `daily`, `weekly`. Anything a
person can say that is not one of those -- "every other Tuesday", "on the last
working day of the month" -- is refused. Approximating it would set a
different reminder from the one asked for.

Time arithmetic is done in the user's zone and stored in UTC. That split
matters for recurrence: "every day at 9am" means 9am on the clock, so the next
occurrence is computed by advancing the *local* date and re-attaching the
zone, not by adding 24 hours to a UTC instant.
"""

import enum
import re
from datetime import date, datetime, time, timedelta
from typing import NamedTuple, Optional

from app.reminders.models import Recurrence

#: Longest reminder text kept. Matches the column, so nothing is silently cut
#: at the database instead of here where it can be reported.
MAX_TEXT_CHARS = 500

#: Furthest ahead a relative reminder may be set. A year is past any real
#: "remind me in N", and an unbounded multiplier is a way to push a row's
#: timestamp somewhere arithmetic breaks.
MAX_RELATIVE_DAYS = 366

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: Units a relative reminder may use, in seconds.
_UNITS = {
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
}

#: Named times of day, for "tomorrow morning". Chosen once, here, so the same
#: word never means two different hours in two different places.
_DAYPARTS = {"morning": time(9, 0), "afternoon": time(14, 0),
             "evening": time(18, 0), "night": time(20, 0),
             "noon": time(12, 0), "midday": time(12, 0),
             "midnight": time(0, 0)}


class ScheduleProblem(str, enum.Enum):
    """Why a schedule could not be read. Each gets a different sentence."""

    NOT_A_REMINDER = "not_a_reminder"
    #: Recognised as a reminder, but no time could be read at all.
    NO_TIME = "no_time"
    #: A time was named but is not one this stage can represent.
    UNSUPPORTED_SCHEDULE = "unsupported_schedule"
    #: The time is in the past.
    IN_THE_PAST = "in_the_past"
    #: Too far ahead to be a reminder anyone meant.
    TOO_FAR_AHEAD = "too_far_ahead"
    #: Recognised, but there is nothing to be reminded *of*.
    NO_TEXT = "no_text"


class ParsedSchedule(NamedTuple):
    """A reminder request, resolved. The only thing the scheduler accepts.

    `problem` and `run_at` are mutually exclusive: a schedule either resolved
    to an instant or it did not, and a caller reading `run_at` without
    checking `ok` gets `None` rather than a plausible guess.
    """

    ok: bool = False
    text: str = ""
    run_at: Optional[datetime] = None
    recurrence: Recurrence = Recurrence.ONCE
    timezone_name: str = "UTC"
    problem: Optional[ScheduleProblem] = None
    #: What the user will be shown, in their own zone. Built here so the
    #: confirmation and the stored record cannot describe different times.
    human_time: str = ""


# --- Recognition --------------------------------------------------------------

#: A reminder request. The verb must be an imperative directed at Mai --
#: "remind me", "set a reminder" -- so "I need a reminder about X" (a
#: statement) and "what reminders do I have" (a question) do not match.
_REQUEST = re.compile(
    r"^\s*(?:hey\s+|ok\s+|please\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:please\s+)?"
    r"(?:remind\s+me|set\s+(?:a\s+|an\s+)?reminder|create\s+(?:a\s+|an\s+)?reminder"
    r"|add\s+(?:a\s+|an\s+)?reminder|ping\s+me)\b",
    re.IGNORECASE,
)

#: Listing and cancelling, which are questions about reminders rather than
#: requests for one. Matched before `_REQUEST` would ever see them.
_LIST = re.compile(
    r"^\s*(?:hey\s+|ok\s+)*"
    r"(?:what|which|show|list|do\s+i\s+have|tell\s+me)\b[^?]*\breminders?\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^\s*(?:hey\s+|ok\s+|please\s+)*"
    r"(?:cancel|delete|remove|drop|forget|clear)\b[^.?!]*\breminder",
    re.IGNORECASE,
)

#: "every day", "every Sunday", "daily", "each Monday".
_RECURRING = re.compile(
    r"\b(?:every|each)\s+(?P<unit>day|morning|evening|week|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b|\b(?P<daily>daily|weekly)\b",
    re.IGNORECASE,
)

#: "in 3 hours", "in 20 minutes".
_RELATIVE = re.compile(
    r"\bin\s+(?P<count>\d{1,4})\s*(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
    re.IGNORECASE,
)

#: "at 10", "at 10am", "at 10:30 pm", "at 22:15".
_CLOCK = re.compile(
    r"\bat\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?\b",
    re.IGNORECASE,
)

#: A named day. "tomorrow", "today", "tonight", or a weekday.
_DAY = re.compile(
    r"\b(?P<day>today|tonight|tomorrow|tmrw|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
    re.IGNORECASE,
)

_DAYPART = re.compile(
    r"\b(?P<part>morning|afternoon|evening|night|noon|midday|midnight)\b",
    re.IGNORECASE,
)

#: Vague time words this stage deliberately refuses rather than resolves.
_VAGUE = re.compile(
    r"\b(?:later|sometime|soon|eventually|some\s+point|whenever|"
    r"next\s+(?:week|month|year)|in\s+a\s+(?:bit|while|few)|"
    r"end\s+of\s+(?:the\s+)?(?:day|week|month))\b",
    re.IGNORECASE,
)

#: Recurrence shapes that exist in English but not in `Recurrence`.
_UNSUPPORTED_RECURRENCE = re.compile(
    r"\b(?:every\s+other|every\s+\d+|bi-?weekly|fortnightly|monthly|yearly|"
    r"annually|every\s+month|every\s+year|last\s+\w+day\s+of|"
    r"weekdays?|weekends?)\b",
    re.IGNORECASE,
)

#: Strips the request frame and any schedule wording off the reminder text.
_TEXT_LEAD = re.compile(
    r"^\s*(?:hey\s+|ok\s+|please\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:remind\s+me|set\s+(?:a\s+|an\s+)?reminder|create\s+(?:a\s+|an\s+)?reminder"
    r"|add\s+(?:a\s+|an\s+)?reminder|ping\s+me)\s*",
    re.IGNORECASE,
)
_TEXT_JOINER = re.compile(r"^\s*(?:to|that|about|for|:|-)\s+", re.IGNORECASE)


def is_list_request(message: str) -> bool:
    """Whether this asks what reminders exist."""
    return bool(message and _LIST.search(message))


def is_cancel_request(message: str) -> bool:
    """Whether this asks to cancel one."""
    return bool(message and _CANCEL.search(message))


def is_management_request(message: str) -> bool:
    """Whether this asks about reminders that already exist.

    True only for listing and cancelling, both of whose patterns require the
    literal word "reminder" -- which makes such a message unambiguously this
    subsystem's, and is why the router may take it before the calendar looks
    at it. "Cancel my reminder about the plants" otherwise reaches the
    calendar's cancel grammar first and is refused as a calendar mutation,
    which is what live verification found.
    """
    return is_list_request(message) or is_cancel_request(message)


def is_reminder_request(message: str) -> bool:
    """Whether this asks Mai to set one."""
    if not message:
        return False
    if is_list_request(message) or is_cancel_request(message):
        return False
    return bool(_REQUEST.search(message))


def cancel_subject(message: str) -> str:
    """What the user wants cancelled, in their words.

    Returns "" for a bare "cancel my reminder", which is the ambiguous case --
    the caller asks rather than choosing one.
    """
    if not message:
        return ""
    trimmed = re.sub(
        r"^\s*(?:hey\s+|ok\s+|please\s+)*"
        r"(?:cancel|delete|remove|drop|forget|clear)\s+"
        r"(?:the\s+|my\s+|that\s+|a\s+)?reminder\s*",
        "",
        message,
        count=1,
        flags=re.IGNORECASE,
    )
    trimmed = _TEXT_JOINER.sub("", trimmed).strip(" .?!,")
    return trimmed[:MAX_TEXT_CHARS]


# --- Parsing --------------------------------------------------------------------


def parse(message: str, now_local: datetime, timezone_name: str) -> ParsedSchedule:
    """Read a reminder request. Never raises; refuses instead.

    `now_local` is the current time *in the user's zone*, supplied by the
    caller so this function is pure and testable at any instant. The result's
    `run_at` is timezone-aware and carries that zone; the caller converts to
    UTC for storage.
    """
    if not message or not message.strip():
        return ParsedSchedule(problem=ScheduleProblem.NOT_A_REMINDER)
    if not is_reminder_request(message):
        return ParsedSchedule(problem=ScheduleProblem.NOT_A_REMINDER)

    text = " ".join(message.split())

    if _UNSUPPORTED_RECURRENCE.search(text):
        return ParsedSchedule(problem=ScheduleProblem.UNSUPPORTED_SCHEDULE)

    recurrence, run_at = _schedule_of(text, now_local)

    if run_at is None:
        # A vague time word is a different failure from no time word at all:
        # one means "you said when but not usefully", the other "you did not
        # say when". Both refuse, and the caller says something different.
        if _VAGUE.search(text):
            return ParsedSchedule(problem=ScheduleProblem.UNSUPPORTED_SCHEDULE)
        return ParsedSchedule(problem=ScheduleProblem.NO_TIME)

    if run_at <= now_local:
        return ParsedSchedule(problem=ScheduleProblem.IN_THE_PAST)
    if run_at - now_local > timedelta(days=MAX_RELATIVE_DAYS):
        return ParsedSchedule(problem=ScheduleProblem.TOO_FAR_AHEAD)

    body = _reminder_text(text)
    if not body:
        return ParsedSchedule(problem=ScheduleProblem.NO_TEXT)

    return ParsedSchedule(
        ok=True,
        text=body,
        run_at=run_at,
        recurrence=recurrence,
        timezone_name=timezone_name,
        human_time=describe(run_at, recurrence),
    )


def _schedule_of(text: str, now_local: datetime):
    """The recurrence and the first occurrence, or `(ONCE, None)`."""
    recurring = _RECURRING.search(text)
    if recurring:
        return _recurring_schedule(recurring, text, now_local)

    relative = _RELATIVE.search(text)
    if relative:
        seconds = _UNITS.get(relative.group("unit").lower())
        if seconds is None:  # pragma: no cover - the alternation bounds it
            return Recurrence.ONCE, None
        count = int(relative.group("count"))
        if count == 0:
            return Recurrence.ONCE, None
        return Recurrence.ONCE, now_local + timedelta(seconds=count * seconds)

    clock = _clock_time(text)
    day = _DAY.search(text)
    part = _DAYPART.search(text)

    if day:
        word = day.group("day").lower()
        target = _day_of(word, now_local)
        at = clock or (_DAYPARTS[part.group("part").lower()] if part else None)
        if at is None and word == "tonight":
            # "Tonight" names a part of the day as well as a day, which
            # "tomorrow" does not. Defaulting it is reading the word, not
            # guessing at an unstated time.
            at = _DAYPARTS["evening"]
        if at is None:
            # A named day with no time. "tomorrow" alone is a day, not an
            # instant, and picking one would be inventing the schedule.
            return Recurrence.ONCE, None
        return Recurrence.ONCE, _combine(target, at, now_local)

    if clock is not None:
        # A bare time: the next time that clock reading occurs.
        candidate = _combine(now_local.date(), clock, now_local)
        if candidate <= now_local:
            candidate = _combine(now_local.date() + timedelta(days=1), clock, now_local)
        return Recurrence.ONCE, candidate

    if part:
        candidate = _combine(
            now_local.date(), _DAYPARTS[part.group("part").lower()], now_local
        )
        if candidate <= now_local:
            candidate = _combine(
                now_local.date() + timedelta(days=1),
                _DAYPARTS[part.group("part").lower()],
                now_local,
            )
        return Recurrence.ONCE, candidate

    return Recurrence.ONCE, None


def _recurring_schedule(match, text: str, now_local: datetime):
    """A `daily` or `weekly` reminder and its first occurrence."""
    unit = (match.group("unit") or match.group("daily") or "").lower()
    clock = _clock_time(text)
    part = _DAYPART.search(text)
    at = clock or (_DAYPARTS[part.group("part").lower()] if part else None)

    if unit in ("day", "daily"):
        at = at or _DAYPARTS["morning"]
        candidate = _combine(now_local.date(), at, now_local)
        if candidate <= now_local:
            candidate = _combine(now_local.date() + timedelta(days=1), at, now_local)
        return Recurrence.DAILY, candidate

    if unit in ("morning", "evening"):
        at = at or _DAYPARTS[unit]
        candidate = _combine(now_local.date(), at, now_local)
        if candidate <= now_local:
            candidate = _combine(now_local.date() + timedelta(days=1), at, now_local)
        return Recurrence.DAILY, candidate

    if unit in ("week", "weekly"):
        at = at or _DAYPARTS["morning"]
        return Recurrence.WEEKLY, _combine(
            now_local.date() + timedelta(days=7), at, now_local
        )

    weekday = _WEEKDAYS.get(unit)
    if weekday is None:  # pragma: no cover - the alternation bounds it
        return Recurrence.ONCE, None

    at = at or _DAYPARTS["morning"]
    target = _next_weekday(now_local.date(), weekday)
    candidate = _combine(target, at, now_local)
    if candidate <= now_local:
        candidate = _combine(target + timedelta(days=7), at, now_local)
    return Recurrence.WEEKLY, candidate


def _clock_time(text: str) -> Optional[time]:
    """"at 10:30 pm" -> 22:30. None when no clock time is present."""
    match = _CLOCK.search(text)
    if match is None:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").lower()

    if minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return time(hour, minute)


def _day_of(word: str, now_local: datetime) -> date:
    if word in ("today", "tonight"):
        return now_local.date()
    if word in ("tomorrow", "tmrw"):
        return now_local.date() + timedelta(days=1)
    weekday = _WEEKDAYS.get(word)
    if weekday is None:  # pragma: no cover - the alternation bounds it
        return now_local.date()
    return _next_weekday(now_local.date(), weekday)


def _next_weekday(today: date, weekday: int) -> date:
    """The next occurrence of a weekday, today included."""
    ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=ahead)


def _combine(day: date, at: time, now_local: datetime) -> datetime:
    """A local instant, carrying the caller's zone.

    `tzinfo` comes from `now_local` rather than being looked up again, so the
    schedule is always expressed in the same zone the question was asked in.
    """
    return datetime.combine(day, at, tzinfo=now_local.tzinfo)


def _reminder_text(text: str) -> str:
    """What the user wants to be reminded of, with the scaffolding removed."""
    body = _TEXT_LEAD.sub("", text, count=1)
    body = _RECURRING.sub(" ", body)
    body = _RELATIVE.sub(" ", body)
    body = _CLOCK.sub(" ", body)
    body = _DAY.sub(" ", body)
    body = _DAYPART.sub(" ", body)
    body = _TEXT_JOINER.sub("", " ".join(body.split()))
    body = " ".join(body.split()).strip(" .,;:!?-")
    return body[:MAX_TEXT_CHARS]


def describe(run_at: datetime, recurrence: Recurrence) -> str:
    """How the schedule is shown back to the user, in their own zone.

    One implementation, used by both the confirmation and the listing, so the
    time a user agrees to and the time they are later shown cannot differ.
    """
    clock = run_at.strftime("%H:%M")
    if recurrence is Recurrence.DAILY:
        return f"every day at {clock}"
    if recurrence is Recurrence.WEEKLY:
        return f"every {run_at.strftime('%A')} at {clock}"
    return run_at.strftime(f"%A %d %B at {clock}")


def next_occurrence(previous: datetime, recurrence: Recurrence) -> Optional[datetime]:
    """The occurrence after `previous`, in the same zone. None for one-shots.

    Advances the **local date**, not the UTC instant. "Every day at 9am" means
    nine o'clock on the clock; adding 24 hours across a DST boundary would
    drift it by an hour and keep drifting.
    """
    if recurrence is Recurrence.ONCE:
        return None
    step = timedelta(days=1 if recurrence is Recurrence.DAILY else 7)
    return datetime.combine(
        previous.date() + step, previous.timetz()
    )


__all__ = [
    "MAX_RELATIVE_DAYS",
    "MAX_TEXT_CHARS",
    "ParsedSchedule",
    "ScheduleProblem",
    "cancel_subject",
    "describe",
    "is_cancel_request",
    "is_list_request",
    "is_management_request",
    "is_reminder_request",
    "next_occurrence",
    "parse",
]
