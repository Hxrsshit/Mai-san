"""Stage 4H -- personal assistant composition.

Recognition, planning, bounds and the composition's behaviour end to end.
The security matrix -- escalation, injection, replay, privacy, network,
failure and truthfulness -- lives in
`tests/security/test_composition_security.py`.
"""

from datetime import datetime, timezone

import pytest

from app.workflows import briefing
from app.workflows.limits import (
    MAX_ARTIFACT_OPERATIONS,
    MAX_CALENDAR_LOOKUPS,
    MAX_EXTERNAL_OPERATIONS,
    MAX_MODEL_CALLS,
    MAX_RESEARCH_QUERIES,
)
from app.workflows.schemas import (
    StepKind,
    StepStatus,
    TOOL_FOR_KIND,
    WorkflowOutcome,
    WorkflowPlan,
    WorkflowStep,
)

UTC = timezone.utc

#: Thursday 10 September 2026, 14:00 UTC.
THURSDAY = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)


def recognise(message, now=THURSDAY, tz=None):
    return briefing.recognise(message, now=now, tz=tz)


# --- §4/§5 The supported requests -------------------------------------------


#: (message, subject, wants_research, wants_artifact)
SUPPORTED = [
    ("I have a meeting with Acme tomorrow. Give me a briefing before the meeting.",
     "Acme", True, False),
    ("Prepare me for my meeting with Acme tomorrow.", "Acme", True, False),
    ("I have a client meeting tomorrow. Give me some background.", "", False, False),
    ("What should I know before tomorrow's Acme meeting?", "Acme", True, False),
    ("Give me a quick briefing for my meeting tomorrow.", "", False, False),
    ("Brief me on my Acme call tomorrow.", "Acme", True, False),
    ("I have a meeting with the Acme Corp team tomorrow, brief me.",
     "Acme Corp", True, False),
    ("Prepare me for my 1:1 tomorrow.", "", False, False),
    ("Catch me up before my Acme sync on Friday.", "Acme", True, False),
    ("Fill me in on my meeting with Globex tomorrow morning.", "Globex", True, False),
    ("What do I need to know before my Initech review tomorrow?",
     "Initech", True, False),
    ("Brief me on my Acme call tomorrow and write it up as a document.",
     "Acme", True, True),
    ("Give me a briefing for my meeting with Acme tomorrow and save it to a file.",
     "Acme", True, True),
]


@pytest.mark.parametrize(
    ("message", "subject", "wants_research", "wants_artifact"), SUPPORTED
)
def test_the_supported_requests_are_recognised(
    message, subject, wants_research, wants_artifact
) -> None:
    request = recognise(message)

    assert request is not None, message
    assert request.subject == subject, message
    assert request.wants_research is wants_research, message
    assert request.wants_artifact is wants_artifact, message
    assert request.starts_at < request.ends_at


def test_the_primary_use_case_plans_the_full_composition() -> None:
    """§4: calendar + bounded research + synthesis."""
    message = "I have a meeting with Acme tomorrow. Give me a briefing before the meeting."
    request = recognise(message)
    plan = briefing.build_plan(message, request)

    assert [step.kind for step in plan.steps] == [
        StepKind.CALENDAR, StepKind.RESEARCH, StepKind.SYNTHESISE
    ]
    # The window is concrete, not a phrase.
    assert plan.step(0).arguments["starts_at"].startswith("2026-09-11T00:00")
    assert plan.step(0).arguments["ends_at"].startswith("2026-09-12T00:00")
    assert plan.step(1).arguments["query"] == "Acme"
    # And synthesis depends on both, so it cannot run before either.
    assert plan.step(2).depends_on == (0, 1)


def test_a_briefing_without_a_named_subject_plans_no_research() -> None:
    """§4: not every meeting requires research.

    "I have a client meeting tomorrow" names no company. Searching for the
    word "client" would be worse than not searching.
    """
    message = "I have a client meeting tomorrow. Give me some background."
    plan = briefing.build_plan(message, recognise(message))

    assert [step.kind for step in plan.steps] == [
        StepKind.CALENDAR, StepKind.SYNTHESISE
    ]
    assert plan.external_operations == 1


def test_an_artifact_is_planned_only_when_asked_for() -> None:
    """§19: not merely because creating one would be convenient."""
    without = "Brief me on my Acme call tomorrow."
    with_doc = "Brief me on my Acme call tomorrow and write it up as a document."

    plain = briefing.build_plan(without, recognise(without))
    documented = briefing.build_plan(with_doc, recognise(with_doc))

    assert StepKind.ARTIFACT not in [s.kind for s in plain.steps]
    assert StepKind.ARTIFACT in [s.kind for s in documented.steps]
    assert documented.steps[-1].arguments["path"] == "acme-briefing.txt"


# --- §5/§6 Not a briefing ---------------------------------------------------


NOT_BRIEFINGS = [
    # Discussion about briefings and meetings.
    "What is a briefing?",
    "Explain how briefings work.",
    "Briefings are useful before meetings.",
    "The briefing yesterday was good.",
    "Meetings are a waste of time.",
    "How do I prepare for a meeting?",
    "Can you explain what a stand-up meeting is?",
    # A meeting mentioned, but nothing asked.
    "I have a meeting tomorrow.",
    "My meeting was cancelled.",
    "I have a meeting with Acme tomorrow.",
    # A briefing asked for, but not about a meeting.
    #
    # The first four have no time either, so they were refused before the
    # meeting requirement was ever consulted -- mutation testing found the
    # requirement itself untested. The last four carry a resolvable time, so
    # only the absence of a meeting stops them.
    "Brief me on the history of Rome.",
    "Give me some background on quantum computing.",
    "What should I know about Python decorators?",
    "Catch me up on the news.",
    "Brief me on quantum computing tomorrow.",
    "Give me some background on the Roman empire tomorrow.",
    "Catch me up on the news from tomorrow.",
    "What should I know about Rust tomorrow?",
    # Refusals, wishes and the past.
    "Don't brief me on my meeting tomorrow.",
    "I wish someone would prepare me for meetings.",
    "I already prepared for tomorrow.",
    "I attended a briefing for the Acme meeting yesterday.",
    # No resolvable time.
    "Prepare me for my meeting.",
    "Give me a briefing.",
    "Brief me on the Acme meeting.",
    # Adjacent requests that belong to other layers.
    "Prepare a report about meetings.",
    "Research Acme and write a summary.",
]


@pytest.mark.parametrize("message", NOT_BRIEFINGS)
def test_ordinary_conversation_is_not_a_briefing(message) -> None:
    """§5: no fuzzy match may cause private calendar access."""
    assert recognise(message) is None, message


def test_there_are_at_least_twenty_negative_cases() -> None:
    assert len(NOT_BRIEFINGS) >= 20


def test_a_briefing_request_naming_no_subject_asks_rather_than_guessing() -> None:
    """"research the company" -- which company?

    The event title would answer it, and that is exactly why the answer is
    not taken from there. See the module docstring in app/workflows/briefing.py.
    """
    request = recognise("Before my meeting tomorrow, research the company and brief me.")

    assert request is not None
    assert request.needs_subject is True
    assert request.wants_research is False


# --- §8 Bounds ---------------------------------------------------------------


def test_the_composition_bounds_are_all_one_or_two() -> None:
    """Pinned as literals: a raised ceiling has to be argued for here."""
    assert MAX_CALENDAR_LOOKUPS == 1
    assert MAX_RESEARCH_QUERIES == 1
    assert MAX_MODEL_CALLS == 1
    assert MAX_ARTIFACT_OPERATIONS == 1
    assert MAX_EXTERNAL_OPERATIONS == 2


@pytest.mark.parametrize(
    ("kinds", "why"),
    [
        ([StepKind.CALENDAR, StepKind.CALENDAR], "two calendar lookups"),
        ([StepKind.RESEARCH, StepKind.RESEARCH], "two research queries"),
        ([StepKind.SYNTHESISE, StepKind.SYNTHESISE], "two model calls"),
        ([StepKind.ARTIFACT, StepKind.ARTIFACT], "two artifacts"),
    ],
)
def test_a_plan_exceeding_a_bound_cannot_be_constructed(kinds, why) -> None:
    """Refused at construction, so an over-large plan is unrepresentable."""
    with pytest.raises(Exception):
        WorkflowPlan(steps=tuple(
            WorkflowStep(index=index, kind=kind)
            for index, kind in enumerate(kinds)
        ))


def test_the_largest_valid_composition_is_within_every_bound() -> None:
    plan = WorkflowPlan(steps=(
        WorkflowStep(index=0, kind=StepKind.CALENDAR),
        WorkflowStep(index=1, kind=StepKind.RESEARCH, depends_on=(0,)),
        WorkflowStep(index=2, kind=StepKind.SYNTHESISE, depends_on=(0, 1)),
        WorkflowStep(index=3, kind=StepKind.ARTIFACT, depends_on=(2,)),
    ))

    assert plan.external_operations == MAX_EXTERNAL_OPERATIONS


def test_a_very_long_message_plans_nothing() -> None:
    assert recognise("Brief me on my Acme meeting tomorrow. " * 200) is None


def test_the_subject_is_bounded() -> None:
    """A query is not a place to put a paragraph."""
    from app.workflows.limits import MAX_SUBJECT_CHARS

    request = recognise(
        "I have a meeting with " + ("Averylongcompanyname " * 20) + " tomorrow, brief me."
    )
    if request is not None:
        assert len(request.subject) <= MAX_SUBJECT_CHARS


# --- §7 The composition model ------------------------------------------------


def test_the_step_kinds_are_closed_and_every_tool_is_declared() -> None:
    """§2: the model may not create capabilities."""
    from app.tools.registry import get_registry

    assert set(TOOL_FOR_KIND.values()) == {
        "calendar_list_events", "web_search", "create_text_file",
    }
    registry = get_registry()
    for tool in TOOL_FOR_KIND.values():
        assert registry.get(tool) is not None, tool

    # The synthesis step has no tool, so it cannot be dispatched at all.
    assert StepKind.SYNTHESISE not in TOOL_FOR_KIND


def test_every_step_status_the_brief_names_exists() -> None:
    """§16: explicit outcome semantics."""
    required = {
        "not_started", "pending_authorization", "approved", "executing",
        "succeeded", "failed", "refused", "unavailable", "cancelled",
    }
    assert required <= {status.value for status in StepStatus}


def test_only_one_status_means_success() -> None:
    """A status added later is unsuccessful by default."""
    from app.workflows.schemas import SUCCESS_STATUS

    assert SUCCESS_STATUS is StepStatus.SUCCEEDED
    succeeded = [s for s in StepStatus if s is SUCCESS_STATUS]
    assert len(succeeded) == 1


def test_a_dependency_must_point_backwards_in_a_briefing_plan() -> None:
    """Inherited from Stage 4F-E, restated for the new shape."""
    with pytest.raises(Exception):
        WorkflowPlan(steps=(
            WorkflowStep(index=0, kind=StepKind.CALENDAR, depends_on=(1,)),
            WorkflowStep(index=1, kind=StepKind.RESEARCH),
        ))


# --- §9 Calendar arguments ---------------------------------------------------


def test_the_calendar_step_carries_only_the_typed_arguments() -> None:
    """§9: no URL, no endpoint, no token, no header, no method."""
    message = "Brief me on my Acme meeting tomorrow."
    plan = briefing.build_plan(message, recognise(message))
    arguments = plan.step(0).arguments

    assert set(arguments) == {
        "starts_at", "ends_at", "max_results", "intent", "window_label",
    }
    assert arguments["intent"] == "calendar_schedule"
    assert arguments["max_results"] <= briefing.BRIEFING_MAX_EVENTS

    # And the schema validates them, so nothing else could be added.
    from app.tools.catalog import CalendarListEventsArguments

    CalendarListEventsArguments.model_validate(arguments)


def test_the_calendar_window_is_computed_in_the_configured_zone() -> None:
    """§9: application-generated time ranges."""
    from zoneinfo import ZoneInfo

    message = "Brief me on my Acme meeting tomorrow."
    kolkata = briefing.recognise(message, now=THURSDAY, tz=ZoneInfo("Asia/Kolkata"))

    start = datetime.fromisoformat(kolkata.starts_at)
    assert start.utcoffset().total_seconds() == 5.5 * 3600
    assert start.hour == 0



# --- Guards that another guard was hiding ------------------------------------
#
# Mutation testing found four requirements in this module that no test could
# reach, each because a different requirement refused the input first. A
# guard nothing exercises is a guard nobody knows is working.


@pytest.mark.parametrize(
    "message",
    [
        "Brief me on quantum computing tomorrow.",
        "Give me some background on the Roman empire tomorrow.",
        "What should I know about Rust tomorrow?",
        "Catch me up on what happened tomorrow.",
    ],
)
def test_a_briefing_needs_a_meeting_even_when_it_names_a_time(message) -> None:
    """The meeting requirement, reached.

    Every other negative case is refused earlier -- for having no time at all
    -- so deleting the meeting check broke nothing. These all resolve a time
    and name no appointment, which leaves the meeting requirement as the only
    thing standing between them and a calendar read.
    """
    assert recognise(message) is None, message


@pytest.mark.parametrize(
    ("message", "why"),
    [
        ("I have a strategy meeting tomorrow, brief me.", "strategy"),
        ("I have a planning call tomorrow, brief me.", "planning"),
        ("Brief me on my budget review tomorrow.", "budget"),
        ("Prepare me for my design sync tomorrow.", "design"),
    ],
)
def test_a_lowercase_word_before_the_meeting_noun_is_not_a_search_subject(
    message, why
) -> None:
    """Capitalisation is load-bearing, and nothing was checking it.

    "strategy meeting" describes the meeting; "Acme meeting" names who it is
    with. Allowing a lowercase word to become the subject sends a fragment of
    the user's own sentence to a search provider -- and every existing
    negative case happened to use a word that was already a stopword, so the
    capitalisation requirement itself was untested.
    """
    request = recognise(message)

    assert request is not None, message
    assert request.subject == "", (message, request.subject)
    assert request.wants_research is False
    assert why not in request.subject


def test_the_subject_length_bound_is_a_literal_not_the_constant() -> None:
    """An earlier version read `MAX_SUBJECT_CHARS` and was conditional.

    Both faults at once: raising the constant moved the assertion with it, and
    `if request is not None` meant the test passed when nothing was recognised
    at all.
    """
    # Through the proper-noun path, which is the one with no per-word cap.
    # The "meeting with X" pattern bounds its own capture at 80 characters,
    # so it can never reach `MAX_SUBJECT_CHARS` and a test using it was
    # measuring the regex rather than the constant.
    message = "The " + ("A" * 400) + " meeting tomorrow, brief me."
    request = recognise(message)

    assert request is not None, "the premise failed; this test proves nothing"
    # A literal, deliberately not `MAX_SUBJECT_CHARS`: reading the constant
    # means the assertion moves with it.
    assert len(request.subject) <= 200
    assert len(request.subject) == 120

    plan = briefing.build_plan(message, request)
    assert plan is not None
    assert len(plan.step(1).arguments["query"]) <= 240


def test_a_capitalised_subject_is_still_recognised() -> None:
    """The complement, so the test above cannot pass by recognising nothing."""
    request = recognise("I have a Strategy Consulting meeting tomorrow, brief me.")

    assert request is not None
    assert request.subject == "Strategy Consulting"
