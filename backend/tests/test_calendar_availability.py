"""Stage 4G.1 -- availability language, temporal resolution, and free/busy.

Three subjects, kept apart because they fail for different reasons:

    recognition   does the grammar route this sentence, and only this sentence?
    resolution    does "tomorrow afternoon" become the right two timestamps?
    arithmetic    do those events, over that window, leave those gaps?

The security-shaped cases -- injection, replay, minimisation, memory -- live
in `tests/security/test_calendar_security.py`.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.calendar import availability
from app.orchestration import calendar_language
from app.orchestration.calendar_language import CalendarIntent

UTC = timezone.utc

#: Thursday 10 September 2026, 14:00 UTC. Every relative expression below is
#: relative to this, so the expectations can be written as literal dates.
THURSDAY = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)


def recognise(message, now=THURSDAY, tz=None):
    return calendar_language.recognise(message, now=now, tz=tz)


# --- §15 Positive availability requests -------------------------------------


#: The nineteen phrasings the brief lists, with the intent each must reach.
#:
#: The intents matter as much as the recognition: an availability question
#: answered as a schedule read sends event titles to the model that the
#: question never needed.
SUPPORTED = [
    ("Am I free tomorrow afternoon?", CalendarIntent.AVAILABILITY, "tomorrow afternoon"),
    ("Am I free tomorrow?", CalendarIntent.AVAILABILITY, "tomorrow"),
    ("Do I have anything tomorrow afternoon?", CalendarIntent.SCHEDULE, "tomorrow afternoon"),
    ("Do I have any meetings tomorrow?", CalendarIntent.SCHEDULE, "tomorrow"),
    ("What does my calendar look like tomorrow?", CalendarIntent.SCHEDULE, "tomorrow"),
    ("What's on my calendar tomorrow?", CalendarIntent.SCHEDULE, "tomorrow"),
    ("What meetings do I have today?", CalendarIntent.SCHEDULE, "today"),
    ("Do I have anything scheduled Friday?", CalendarIntent.SCHEDULE, "Friday"),
    ("What's my schedule this afternoon?", CalendarIntent.SCHEDULE, "this afternoon"),
    ("When is my next meeting?", CalendarIntent.NEXT_EVENT, "the next couple of weeks"),
    ("What am I doing tomorrow morning?", CalendarIntent.SCHEDULE, "tomorrow morning"),
    ("Do I have a free slot tomorrow?", CalendarIntent.AVAILABILITY, "tomorrow"),
    ("When am I free tomorrow?", CalendarIntent.AVAILABILITY, "tomorrow"),
    ("Find me a free hour tomorrow afternoon.", CalendarIntent.AVAILABILITY, "tomorrow afternoon"),
    ("Do I have any appointments tomorrow?", CalendarIntent.SCHEDULE, "tomorrow"),
    ("Show me my schedule for Friday.", CalendarIntent.SCHEDULE, "Friday"),
    ("What's happening on my calendar this week?", CalendarIntent.SCHEDULE, "this week"),
    ("Am I busy Friday afternoon?", CalendarIntent.AVAILABILITY, "Friday afternoon"),
    ("How packed is my calendar tomorrow?", CalendarIntent.AVAILABILITY, "tomorrow"),
]


@pytest.mark.parametrize(("message", "intent", "label"), SUPPORTED)
def test_the_brief_s_nineteen_phrasings_are_recognised(message, intent, label) -> None:
    request = recognise(message)

    assert request.is_readable, message
    assert request.intent is intent, message
    assert request.window_label == label, message
    assert request.starts_at < request.ends_at


def test_the_reported_failure_is_fixed() -> None:
    """The sentence this stage exists for."""
    request = recognise("Am I free tomorrow afternoon?")

    assert request.is_readable
    assert request.intent is CalendarIntent.AVAILABILITY
    # Friday the 11th, 12:00 to 18:00.
    assert request.starts_at.startswith("2026-09-11T12:00")
    assert request.ends_at.startswith("2026-09-11T18:00")


@pytest.mark.parametrize(
    "message",
    [
        "am i free tomorrow",
        "AM I FREE TOMORROW AFTERNOON?",
        "Hey, am I free tomorrow?",
        "So am I busy tomorrow morning?",
        "Can you tell me my schedule for tomorrow?",
        "Are I free tomorrow" .replace("Are I", "Am I"),
        "  Am   I   free   tomorrow  ?  ",
        "When am I available tomorrow?",
        "Am I around Friday afternoon?",
        "Is there a free slot tomorrow?",
        "Have I got any free time tomorrow?",
        "Suggest a free hour tomorrow.",
        "How busy is my schedule tomorrow?",
        "What am I up to tomorrow evening?",
    ],
)
def test_equivalent_phrasings_are_supported(message) -> None:
    """§1: variations, not only the listed strings."""
    request = recognise(message)

    assert request.is_readable, message


# --- §15 Negative cases -----------------------------------------------------


NOT_REQUESTS = [
    # The brief's own list.
    "I wish I were free tomorrow.",
    "Why do people have such busy calendars?",
    "What does free time mean?",
    "Calendar apps are useful.",
    "I had a meeting yesterday.",
    "My calendar is a mess.",
    "Can you explain how Google Calendar works?",
    "What is a free/busy calendar?",
    "I need to buy a calendar.",
    # Discussion, not a request.
    "Tell me about calendar software.",
    "The meeting was productive.",
    "Calendars were invented long ago.",
    "Free time is important for wellbeing.",
    "Busy people use calendars.",
    "Scheduling is a hard problem in computer science.",
    "What is an agenda?",
    "Explain what a diary is.",
    "How does scheduling work?",
    # Refusals and past tense.
    "I do not want you to check my calendar.",
    "Never look at my calendar.",
    "I already checked my calendar.",
    "I saw my schedule earlier.",
    "I attended a meeting on Friday.",
    # The head matches but the sentence is about something else entirely.
    "Do I have anything to declare?",
    "Am I free to speak my mind?",
    "What am I doing wrong?",
    "I'd love a free hour someday.",
]


@pytest.mark.parametrize("message", NOT_REQUESTS)
def test_talking_about_calendars_is_not_asking_about_one(message) -> None:
    request = recognise(message)

    assert not request.is_readable, message
    assert not request.needs_clarification, message
    assert not request.is_write_request, message


def test_there_are_at_least_twenty_negative_cases() -> None:
    """§15 asks for twenty; a list that quietly shrinks is worth catching."""
    assert len(NOT_REQUESTS) >= 20


# --- Ambiguity: asking rather than guessing ---------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Am I free?",
        "When am I free?",
        "Do I have a free slot?",
        "What am I doing?",
        "Am I busy?",
    ],
)
def test_a_calendar_question_with_no_time_asks_rather_than_guesses(message) -> None:
    """§3: do not invent dates, and do not query an arbitrary range.

    Defaulting would read a period of the user's private calendar that they
    never named. Asking costs one turn.
    """
    request = recognise(message)

    assert request.needs_clarification, message
    assert not request.is_readable, message
    # And nothing to execute: no window means no arguments.
    assert request.starts_at == ""
    assert request.ends_at == ""


def test_an_object_centric_question_with_no_time_means_today() -> None:
    """The asymmetry is deliberate.

    "What's on my calendar?" has an obvious answer -- today -- and everyone
    means it. "Am I free?" does not: it is equally "right now" and "at all
    today", and those read different windows.
    """
    request = recognise("What's on my calendar?")

    assert request.is_readable
    assert request.window_label == "today"


# --- §15 Temporal extraction ------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "starts", "ends", "label"),
    [
        ("today", "2026-09-10T00:00", "2026-09-11T00:00", "today"),
        ("tomorrow", "2026-09-11T00:00", "2026-09-12T00:00", "tomorrow"),
        ("tonight", "2026-09-10T17:00", "2026-09-11T00:00", "tonight"),
        ("this afternoon", "2026-09-10T12:00", "2026-09-10T18:00", "this afternoon"),
        ("tomorrow morning", "2026-09-11T06:00", "2026-09-11T12:00", "tomorrow morning"),
        ("tomorrow afternoon", "2026-09-11T12:00", "2026-09-11T18:00", "tomorrow afternoon"),
        ("tomorrow evening", "2026-09-11T17:00", "2026-09-12T00:00", "tomorrow evening"),
        ("Friday", "2026-09-11T00:00", "2026-09-12T00:00", "Friday"),
        ("Friday afternoon", "2026-09-11T12:00", "2026-09-11T18:00", "Friday afternoon"),
        ("next Monday", "2026-09-14T00:00", "2026-09-15T00:00", "Monday"),
    ],
)
def test_temporal_expressions_resolve_to_the_right_window(
    phrase, starts, ends, label
) -> None:
    request = recognise(f"What's on my calendar {phrase}?")

    assert request.is_readable, phrase
    assert request.starts_at.startswith(starts), phrase
    assert request.ends_at.startswith(ends), phrase
    assert request.window_label == label, phrase


def test_this_week_runs_seven_days_from_now() -> None:
    request = recognise("What's on my calendar this week?")

    assert request.starts_at.startswith("2026-09-10T14:00")
    assert request.ends_at.startswith("2026-09-17T14:00")


def test_next_week_begins_on_the_coming_monday() -> None:
    request = recognise("What's on my calendar next week?")

    # Thursday the 10th -> Monday the 14th, for seven days.
    assert request.starts_at.startswith("2026-09-14T00:00")
    assert request.ends_at.startswith("2026-09-21T00:00")
    assert request.window_label == "next week"


def test_later_today_starts_now_and_ends_at_midnight() -> None:
    request = recognise("Am I free later today?")

    assert request.starts_at.startswith("2026-09-10T14:00")
    assert request.ends_at.startswith("2026-09-11T00:00")


def test_the_next_few_hours_is_bounded() -> None:
    """§21: a time expression must not be able to create an unbounded query."""
    request = recognise("Am I free in the next few hours?")

    start = datetime.fromisoformat(request.starts_at)
    end = datetime.fromisoformat(request.ends_at)
    # A literal ceiling, deliberately not `NEXT_HOURS`. Asserting against the
    # constant means the assertion moves with it, so widening the horizon to a
    # year would still pass -- which is exactly the mutation this must catch.
    assert end - start <= timedelta(hours=12)
    assert end - start == timedelta(hours=calendar_language.NEXT_HOURS)


def test_right_now_is_an_hour_not_a_day() -> None:
    request = recognise("Am I free right now?")

    start = datetime.fromisoformat(request.starts_at)
    end = datetime.fromisoformat(request.ends_at)
    assert end - start == timedelta(hours=1)


def test_a_named_weekday_means_the_coming_one_not_today() -> None:
    """Asked on a Thursday, "Thursday" is next week's."""
    request = recognise("What's on my calendar Thursday?", now=THURSDAY)

    assert request.starts_at.startswith("2026-09-17T00:00")


# --- Timezone (§3) ----------------------------------------------------------


def test_the_window_is_built_in_the_configured_zone_not_utc() -> None:
    """Stage 4F-G resolved "tomorrow" in UTC, which is wrong everywhere else.

    At 14:00 UTC on the 10th it is 19:30 on the 10th in Kolkata, so "tomorrow"
    is the 11th local -- which begins at 18:30 UTC on the 10th, not at
    midnight UTC.
    """
    kolkata = ZoneInfo("Asia/Kolkata")
    request = recognise("What's on my calendar tomorrow?", tz=kolkata)

    start = datetime.fromisoformat(request.starts_at)
    assert start.year, start.month == (2026, 9)
    assert start.day == 11
    assert start.hour == 0 and start.minute == 0
    # Local midnight, which is not UTC midnight.
    assert start.utcoffset() == timedelta(hours=5, minutes=30)
    assert start.astimezone(UTC).day == 10


def test_a_zone_behind_utc_can_put_tomorrow_on_a_different_date() -> None:
    """The same instant, a different calendar day.

    At 02:00 UTC on the 11th it is still the 10th in Los Angeles, so "tomorrow"
    there is the 11th while "tomorrow" in UTC is the 12th. Getting this wrong
    reads an entire day of the wrong schedule.
    """
    early = datetime(2026, 9, 11, 2, 0, tzinfo=UTC)

    in_utc = recognise("What's on my calendar tomorrow?", now=early, tz=UTC)
    in_la = recognise(
        "What's on my calendar tomorrow?", now=early, tz=ZoneInfo("America/Los_Angeles")
    )

    assert in_utc.starts_at.startswith("2026-09-12")
    assert in_la.starts_at.startswith("2026-09-11")


def test_a_window_across_a_daylight_saving_transition_is_still_a_whole_day() -> None:
    """The clocks go back in New York on 1 November 2026, making a 25-hour day.

    A day is "midnight to midnight", not "start plus 24 hours". Computing it
    the second way would leave an hour of the user's calendar unread.
    """
    new_york = ZoneInfo("America/New_York")
    saturday = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)

    request = recognise(
        "What's on my calendar tomorrow?", now=saturday, tz=new_york
    )

    start = datetime.fromisoformat(request.starts_at)
    end = datetime.fromisoformat(request.ends_at)
    assert start.hour == 0 and end.hour == 0
    assert (end - start) == timedelta(hours=25)


def test_midnight_boundaries_are_exact() -> None:
    request = recognise("What's on my calendar today?")

    assert request.starts_at.startswith("2026-09-10T00:00:00")
    assert request.ends_at.startswith("2026-09-11T00:00:00")


# --- §15 Availability calculation -------------------------------------------


def at(hour, minute=0, day=11):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


WINDOW = (at(12), at(18))


def periods(result_periods):
    return [(p.start.hour, p.start.minute, p.end.hour, p.end.minute) for p in result_periods]


def test_a_completely_free_window() -> None:
    result = availability.compute(*WINDOW, [])

    assert result.fully_free
    assert periods(result.free) == [(12, 0, 18, 0)]
    assert result.busy == ()


def test_a_single_event_leaves_two_gaps() -> None:
    """The brief's own example: busy 2-3, free 12-2 and 3-6."""
    result = availability.compute(*WINDOW, [(at(14), at(15), False)])

    assert periods(result.busy) == [(14, 0, 15, 0)]
    assert periods(result.free) == [(12, 0, 14, 0), (15, 0, 18, 0)]


def test_a_completely_occupied_window() -> None:
    result = availability.compute(*WINDOW, [(at(11), at(19), False)])

    assert result.fully_busy
    assert periods(result.busy) == [(12, 0, 18, 0)]
    assert result.free == ()


def test_overlapping_events_merge() -> None:
    result = availability.compute(
        *WINDOW, [(at(13), at(15, 30), False), (at(15), at(16), False)]
    )

    assert periods(result.busy) == [(13, 0, 16, 0)]


def test_adjacent_events_merge_and_leave_no_zero_length_gap() -> None:
    """`[12:00, 14:00)` and `[14:00, 16:00)` touch. There is no gap at 14:00."""
    result = availability.compute(
        *WINDOW, [(at(12), at(14), False), (at(14), at(16), False)]
    )

    assert periods(result.busy) == [(12, 0, 16, 0)]
    assert periods(result.free) == [(16, 0, 18, 0)]


def test_multiple_gaps() -> None:
    result = availability.compute(
        *WINDOW,
        [(at(12, 30), at(13), False), (at(14), at(15), False), (at(16), at(16, 30), False)],
    )

    assert periods(result.free) == [
        (12, 0, 12, 30), (13, 0, 14, 0), (15, 0, 16, 0), (16, 30, 18, 0)
    ]


def test_an_all_day_event_occupies_the_whole_window() -> None:
    """Treating an all-day event as free is what books a meeting into a holiday."""
    result = availability.compute(*WINDOW, [(None, None, True)])

    assert result.all_day_blocked
    assert result.fully_busy
    assert result.free == ()


def test_an_event_with_no_end_runs_to_the_end_of_the_window() -> None:
    """Over-reporting busy is the safe direction; inventing free time is not."""
    result = availability.compute(*WINDOW, [(at(15), None, False)])

    assert periods(result.busy) == [(15, 0, 18, 0)]
    assert periods(result.free) == [(12, 0, 15, 0)]


def test_an_event_entirely_outside_the_window_is_ignored() -> None:
    result = availability.compute(*WINDOW, [(at(8), at(9), False)])

    assert result.fully_free


def test_an_event_ending_exactly_when_the_window_opens_does_not_occupy_it() -> None:
    """Half-open intervals, stated as a test so the choice cannot drift."""
    result = availability.compute(*WINDOW, [(at(9), at(12), False)])

    assert result.fully_free


def test_an_event_crossing_midnight_is_clipped_to_the_window() -> None:
    window = (at(22), at(23, 59, ))
    result = availability.compute(at(22), at(23, 59), [(at(23), at(2, day=12), False)])

    assert periods(result.busy) == [(23, 0, 23, 59)]


def test_an_event_straddling_the_window_start_is_clipped() -> None:
    result = availability.compute(*WINDOW, [(at(11), at(13), False)])

    assert periods(result.busy) == [(12, 0, 13, 0)]


def test_a_gap_shorter_than_the_minimum_is_not_reported_as_free() -> None:
    """Four minutes between meetings is not availability."""
    result = availability.compute(
        *WINDOW, [(at(12), at(14), False), (at(14, 4), at(18), False)]
    )

    assert result.free == ()


def test_an_inverted_window_yields_nothing_rather_than_raising() -> None:
    result = availability.compute(at(18), at(12), [(at(14), at(15), False)])

    assert result.busy == ()
    assert result.free == ()


def test_an_inverted_window_with_an_all_day_event_yields_no_backwards_period() -> None:
    """The case that makes the inverted-window guard load-bearing.

    An ordinary event is clipped out of a backwards window by the
    `finish <= begin` check, so it hides the missing guard. An all-day event
    is not clipped -- it is appended as the window itself -- so without the
    guard the result contains a busy period running from 18:00 back to 12:00.
    A negative-length interval would then be rendered into the prompt.
    """
    result = availability.compute(at(18), at(12), [(None, None, True)])

    assert result.busy == ()
    for period in result.busy + result.free:
        assert period.start <= period.end


def test_the_number_of_reported_periods_is_bounded() -> None:
    """§21: result sizes are bounded.

    Many short events must not produce an unbounded block of prompt text.
    """
    many = [
        (at(12) + timedelta(minutes=i * 4), at(12) + timedelta(minutes=i * 4 + 1), False)
        for i in range(80)
    ]
    result = availability.compute(at(12), at(18), many, min_gap_minutes=0)

    # Literal ceilings. `MAX_PERIODS` is what is being pinned, so reading it
    # here would let it be raised to a billion without a test noticing.
    # Tight literals, just above `MAX_PERIODS`. A loose ceiling made this
    # test useless: at 100 it still passed with the bound removed entirely,
    # because the fixture only produces eighty periods.
    assert len(result.busy) <= 45
    assert len(result.free) <= 45
    assert len(result.busy) <= availability.MAX_PERIODS
    assert len(result.free) <= availability.MAX_PERIODS


# --- The intent enums cannot drift ------------------------------------------


def test_the_recogniser_and_the_tool_agree_on_the_intent_names() -> None:
    """Two enums, one vocabulary.

    The recogniser produces one and the tool schema validates the other. If
    they drifted, a perfectly valid recognised intent would be rejected at the
    tool boundary -- or worse, silently fall back to the wider rendering.
    """
    from app.tools.catalog import CalendarReadIntent

    assert {i.value for i in CalendarIntent} == {i.value for i in CalendarReadIntent}


def test_an_unknown_intent_falls_back_to_the_narrower_path_not_a_wider_one() -> None:
    """An unrecognised intent must never be a way to reach *more* data."""
    from app.tools.catalog import CalendarListEventsArguments

    with pytest.raises(Exception):
        CalendarListEventsArguments(
            starts_at="2026-09-11T12:00:00+00:00",
            ends_at="2026-09-11T18:00:00+00:00",
            intent="calendar_write_everything",
        )


# --- Availability of the recogniser itself (§21) ----------------------------


def test_recognition_does_not_backtrack_catastrophically() -> None:
    """A real defect, found while writing this stage and fixed here.

    Writing the optional opener as a repetition -- `(?:hey|so|please...)*`
    rather than `...?` -- made every pattern in the module explode on input
    that starts like an opener and then does not match: "so so so so ... x"
    ran for over four hundred seconds before the test run was killed.

    The message is attacker-controlled text on a path with no timeout, so the
    failure is a denial of service rather than a slow parse. The bound below
    is loose by three orders of magnitude against the observed 2ms, so it
    will not flake, and the exponential version could not come close to it.
    """
    import time

    hostile = [
        "so " * 300 + "x",
        "can you " * 120 + "x",
        "hey, " * 200 + "x",
        "please " * 140 + "am i free",
        "so and also ok okay hi hello " * 33,
        "am i free " * 100,
        "do i have anything " * 55,
        "what is a " * 100,
        "a" * 999,
        "?" * 999,
    ]

    started = time.perf_counter()
    for message in hostile:
        calendar_language.recognise(message[:1000], now=THURSDAY)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"recognition took {elapsed:.1f}s on hostile input"


def test_an_over_long_message_is_refused_before_matching() -> None:
    """The length cap is the outer bound on any pattern's work."""
    request = calendar_language.recognise("Am I free tomorrow? " * 200, now=THURSDAY)

    assert not request.is_request


# --- Guards that a head actually reaches ------------------------------------
#
# Mutation testing found the negation, past-tense and wish guards all
# survived deletion. Not because they are redundant -- because every negative
# case above is rejected *earlier*, by no family head matching at all. A guard
# that only ever runs after something else has already refused is a guard no
# test is exercising.
#
# Each sentence below matches a family head. Only the named guard stops it.


@pytest.mark.parametrize(
    ("message", "guard"),
    [
        ("Don't tell me my schedule for tomorrow.", "negation"),
        ("Can't you show me my calendar tomorrow?", "negation"),
        ("I never want to know what is on my calendar tomorrow.", "negation"),
        ("I already checked what is on my calendar tomorrow.", "first-person past"),
        ("I had anything scheduled tomorrow.", "first-person past"),
        ("I wish I knew what is on my calendar tomorrow.", "wish"),
        ("I wish you would show me my calendar tomorrow.", "wish"),
        ("If only I knew what is on my calendar tomorrow.", "wish"),
    ],
)
def test_a_guard_refuses_a_sentence_whose_head_matches(message, guard) -> None:
    text = " ".join(message.split())

    # The premise: a family head really does match, so the guard is the only
    # thing standing between this sentence and a calendar read.
    heads = [f.name for f in calendar_language._FAMILIES if f.pattern.search(text)]
    assert heads, f"{message!r} no longer reaches a head; this test has stopped testing {guard}"

    assert not recognise(message).is_readable, message


# --- Timezone, through the service (§3) -------------------------------------


def test_the_service_passes_the_configured_zone_to_the_recogniser() -> None:
    """Mutation testing found the recogniser zone-aware and the caller not.

    `recognise` honoured a `tz` argument correctly and every test proved it --
    by passing `tz` itself. Nothing checked that the *service* supplied one,
    so dropping it and letting every window fall back to UTC broke no test.
    """
    import inspect

    from app.calendar.service import CalendarService

    source = inspect.getsource(CalendarService)
    assert "tz=self._timezone()" in source


@pytest.mark.parametrize(
    ("zone", "expected_offset_hours"),
    [("UTC", 0), ("Asia/Kolkata", 5.5), ("America/Los_Angeles", -7)],
)
def test_the_service_resolves_its_zone_from_settings(zone, expected_offset_hours) -> None:
    from types import SimpleNamespace

    from app.calendar.service import CalendarService

    service = CalendarService.__new__(CalendarService)
    service._settings = SimpleNamespace(MAI_TIMEZONE=zone)

    resolved = service._timezone()
    offset = datetime(2026, 9, 10, 12, 0, tzinfo=resolved).utcoffset()
    assert offset.total_seconds() / 3600 == expected_offset_hours


def test_an_unusable_zone_falls_back_to_utc_rather_than_failing_the_turn() -> None:
    """`Settings` refuses an unknown zone at startup, so this cannot normally
    happen. If it somehow does, answering in UTC beats failing the question."""
    from types import SimpleNamespace

    from app.calendar.service import CalendarService

    service = CalendarService.__new__(CalendarService)
    service._settings = SimpleNamespace(MAI_TIMEZONE="Mars/Olympus")

    resolved = service._timezone()
    assert datetime(2026, 9, 10, tzinfo=resolved).utcoffset() == timedelta(0)


def test_settings_refuse_an_unknown_timezone_at_startup() -> None:
    """A silent fallback would leave every window quietly wrong.

    The deployment would look configured, the answers would be plausible, and
    nobody would check. A typo should stop the process.
    """
    from app.core.config import Settings

    assert Settings(MAI_TIMEZONE="Asia/Kolkata").MAI_TIMEZONE == "Asia/Kolkata"

    for bad in ("Mars/Olympus", "", "   ", "Not/A/Zone"):
        with pytest.raises(Exception):
            Settings(MAI_TIMEZONE=bad)


# --- Classification of the availability block -------------------------------


def test_the_availability_block_is_private_and_untrusted() -> None:
    """Mutation testing found the availability rendering's classification
    untested -- the existing test pinned the *event* block's.

    When someone is busy is personal data even with the reason removed.
    """
    from app.integrations.calendar_schemas import CalendarEvent, CalendarWindow
    from app.integrations.result import DataClassification, TrustLevel

    window = CalendarWindow(
        events=(
            CalendarEvent(
                title="x",
                starts_at="2026-09-11T14:00:00+00:00",
                ends_at="2026-09-11T15:00:00+00:00",
            ),
        ),
        starts_at="2026-09-11T12:00:00+00:00",
        ends_at="2026-09-11T18:00:00+00:00",
        total_available=1,
    )

    data = window.as_availability_data("tomorrow afternoon")

    assert data.classification is DataClassification.PRIVATE
    assert data.trust_level is TrustLevel.UNTRUSTED
    assert data.source == "google_calendar"


def test_the_availability_block_carries_no_event_text_at_all() -> None:
    """Not redacted -- never assembled."""
    from app.integrations.calendar_schemas import CalendarEvent, CalendarWindow

    window = CalendarWindow(
        events=(
            CalendarEvent(
                title="TITLESENTINEL",
                location="LOCSENTINEL",
                organiser="ORGSENTINEL",
                starts_at="2026-09-11T14:00:00+00:00",
                ends_at="2026-09-11T15:00:00+00:00",
            ),
        ),
        starts_at="2026-09-11T12:00:00+00:00",
        ends_at="2026-09-11T18:00:00+00:00",
        total_available=1,
    )

    content = window.as_availability_data("tomorrow afternoon").content

    for sentinel in ("TITLESENTINEL", "LOCSENTINEL", "ORGSENTINEL"):
        assert sentinel not in content
    # And it did render the interval, so this is not passing on an empty block.
    assert "14:00" in content and "15:00" in content


def test_intervals_expose_times_and_nothing_else() -> None:
    """The availability path receives triples, not events."""
    from app.integrations.calendar_schemas import CalendarEvent, CalendarWindow

    window = CalendarWindow(
        events=(
            CalendarEvent(
                title="TITLESENTINEL",
                starts_at="2026-09-11T14:00:00+00:00",
                ends_at="2026-09-11T15:00:00+00:00",
            ),
        ),
    )

    for start, end, all_day in window.intervals():
        assert isinstance(all_day, bool)
        assert "TITLESENTINEL" not in repr(start) + repr(end)



# --- The memory rule, as a rule ---------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "suppressed"),
    [
        ("not_calendar", False),
        ("completed", True),
        ("clarification_needed", True),
        ("write_not_supported", True),
        ("disabled", True),
        ("not_configured", True),
        ("not_connected", True),
        ("reauthorisation_required", True),
        ("failed", True),
    ],
)
def test_every_calendar_outcome_except_not_calendar_suppresses_extraction(
    outcome, suppressed
) -> None:
    """Mutation testing found this rule untestable where it lived.

    As an inline boolean inside the route, narrowing it from "any calendar
    outcome" to "only completed" broke no test: the only turns that reached it
    end-to-end were completed ones. Named and enumerated, every state is
    checked -- including the ones that are easy to forget, like the turn that
    merely asked which day the user meant.
    """
    from app.api.routes.conversations import touches_personal_data
    from app.calendar.schemas import CalendarOutcome, CalendarResult

    result = CalendarResult(outcome=CalendarOutcome(outcome))

    assert touches_personal_data(result) is suppressed
    assert touches_personal_data(None) is False


def test_the_outcome_list_above_covers_every_outcome() -> None:
    """So a new outcome cannot default into being extracted."""
    from app.calendar.schemas import CalendarOutcome

    covered = {
        "not_calendar", "completed", "clarification_needed",
        "write_not_supported", "disabled", "not_configured",
        "not_connected", "reauthorisation_required", "failed",
    }
    assert {o.value for o in CalendarOutcome} == covered


def test_a_failed_read_still_reports_which_question_it_was() -> None:
    """`intent` says what was asked, not whether it worked.

    Found in live verification: a reauthorisation failure reported
    `intent: null` for a question the recogniser had classified perfectly
    well, which made the wire field mean "the intent of successful reads".
    """
    from types import SimpleNamespace

    from app.calendar.service import CalendarService
    from app.calendar.schemas import CalendarOutcome

    service = CalendarService.__new__(CalendarService)
    result = service._failure(
        "calendar_reauthorisation_required", CalendarIntent.AVAILABILITY
    )

    assert result.outcome is CalendarOutcome.REAUTHORISATION_REQUIRED
    assert result.intent == "calendar_availability"
    # And it still carries no event content.
    assert result.events_block == ""
