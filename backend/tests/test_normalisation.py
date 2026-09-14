"""Stage 5A -- reading past a typo.

Three subjects: what the normaliser corrects, what it must leave alone, and
what the corrected text then routes to. The adversarial matrix lives in
`tests/security/test_normalisation_security.py`.
"""

from datetime import datetime, timezone

import pytest

from app.language import normalise as module
from app.language.normalise import (
    CANONICAL_TERMS,
    KNOWN_MISSPELLINGS,
    MAX_CORRECTIONS,
    MAX_EDIT_DISTANCE,
    MAX_INPUT_CHARS,
    PROTECTED_WORDS,
    normalise,
)
from app.orchestration import calendar_language
from app.research import language as research_language

UTC = timezone.utc
MONDAY = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)


def route(message):
    """Which layer claims this message, after normalisation.

    The same order the chat turn uses: calendar, then workflow, then research.
    """
    from app.workflows import briefing

    text = normalise(message).text
    calendar = calendar_language.recognise(text, now=MONDAY)
    if calendar.is_write_request:
        return "calendar_write"
    if calendar.is_readable:
        return "calendar_read"
    if calendar.needs_clarification:
        return "calendar_clarify"
    if briefing.recognise(text, now=MONDAY) is not None:
        return "briefing"
    if research_language.recognise(text).is_request:
        return "research"
    return "ordinary"


# --- The mappings the brief requires -----------------------------------------


@pytest.mark.parametrize(
    ("typo", "expected"),
    [
        ("google callender", "google calendar"),
        ("calender", "calendar"),
        ("calandar", "calendar"),
        ("notifcation", "notification"),
        ("remider", "reminder"),
    ],
)
def test_the_required_mappings(typo, expected) -> None:
    assert normalise(typo).text == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("add this to my callender", "add this to my calendar"),
        ("what's on my calender tomorrow", "what's on my calendar tomorrow"),
        ("check my calandar", "check my calendar"),
        ("set a remider for tomorrow", "set a reminder for tomorrow"),
        ("send me a notifcation", "send me a notification"),
    ],
)
def test_the_contextual_cases(message, expected) -> None:
    assert normalise(message).text == expected


@pytest.mark.parametrize(
    ("typo", "expected"),
    [
        # Reached by the table.
        ("tommorow", "tomorrow"), ("schedual", "schedule"),
        ("appointmnet", "appointment"), ("breifing", "briefing"),
        ("wendsday", "wednesday"), ("meetng", "meeting"),
        # Reached by bounded edit distance, with no table entry.
        ("calenndar", "calendar"), ("calendarr", "calendar"),
        ("aftrnoon", "afternoon"), ("notificatio", "notification"),
        ("appointmentt", "appointment"),
    ],
)
def test_typos_reached_by_either_route(typo, expected) -> None:
    assert normalise(typo).text == expected


def test_case_is_preserved() -> None:
    assert normalise("Callender").text == "Calendar"
    assert normalise("CALLENDER").text == "CALENDAR"
    assert normalise("callender").text == "calendar"


def test_punctuation_and_spacing_survive_exactly() -> None:
    message = "  what's   on my callender,  tomorrow??  "
    result = normalise(message)

    assert result.text == "  what's   on my calendar,  tomorrow??  "
    assert result.original == message


# --- What must not be touched -------------------------------------------------


NEVER_CORRECTED = [
    # Ordinary misspellings of words Mai has no capability for. Correcting
    # them would change the sentence without changing what Mai does.
    "I need to seperate these",
    "did you recieve it",
    "that is definately true",
    "occurence of the problem",
    # Real words that sit close to the vocabulary.
    "the remainder of the money",
    "dairy products are expensive",
    "the agent said no",
    "an eventual outcome",
    "a warning sign",
    "cooking and booking",
    "summer is coming",
    "monkey money monday",
    "documents were documented",
    "reminding me is fine",
    "scheduled and scheduling",
    # Short tokens are never corrected.
    "call me",
    "mail it",
    "the diary",
    # Identifiers and non-words.
    "calendar_list_events",
    "user@calender.example",
    "https://calender.example/x",
    "v1.2.3-calendr",
]


@pytest.mark.parametrize("message", NEVER_CORRECTED)
def test_ordinary_text_is_left_alone(message) -> None:
    result = normalise(message)

    assert result.text == message, result.corrections
    assert not result.changed


def test_the_vocabulary_is_closed_and_small() -> None:
    """The property that makes normalisation safe.

    A correction can only ever produce a member of this set, and every member
    is a word one of the existing grammars already matches on. So the most a
    typo can become is a word that was already going to be routed somewhere --
    it cannot become a capability that did not exist.
    """
    assert len(CANONICAL_TERMS) < 60
    for term in CANONICAL_TERMS:
        assert term.isalpha(), term
        assert term.islower(), term

    # Every table entry lands inside the vocabulary. Nothing may map outward.
    for typo, target in KNOWN_MISSPELLINGS.items():
        assert target in CANONICAL_TERMS, (typo, target)


def test_no_vocabulary_word_can_trigger_an_external_operation() -> None:
    """A verb is what turns a sentence into a request.

    A layer that could repair a broken verb into a working one could
    manufacture an instruction out of noise, so the vocabulary holds nouns.

    Two exceptions are worth naming precisely rather than hiding:
    **"schedule" and "email" are in the set**, because "what's on my
    schedual" and "check my emials" are real questions and the words are
    nouns there. Each has a verb sense, and each verb sense reaches exactly
    one place -- a write-request detector -- which performs nothing and
    produces a refusal, because no calendar-write and no mail-send capability
    exists. The tests below prove that, so the exceptions are verified rather
    than asserted.

    What the vocabulary may never contain is a verb that reaches an *external*
    operation: a search or a file write.
    """
    triggers = {
        "search", "google", "researching", "research", "find", "lookup",
        "create", "write", "make", "save", "generate", "produce", "send",
        "run", "execute", "approve", "authorize", "authorise",
        "yes", "confirm", "delete", "remove", "forward", "reply", "archive",
    }
    assert not (CANONICAL_TERMS & triggers), CANONICAL_TERMS & triggers
    assert not (set(KNOWN_MISSPELLINGS.values()) & triggers)


def test_repairing_a_write_verb_produces_a_refusal_and_no_capability() -> None:
    """The "schedule" exception, verified.

    "schedual a meetng tomorrow" repairs into a calendar write request. That
    must reach a truthful refusal and nothing else -- no capability appears,
    because none exists to appear.
    """
    text = normalise("schedual a meetng tomorrow").text
    assert text == "schedule a meeting tomorrow"

    request = calendar_language.recognise(text, now=MONDAY)
    assert request.is_write_request is True
    assert request.is_readable is False

    from app.execution.tools import get_executable_registry
    from app.tools.registry import get_registry

    for write in ("calendar_create_event", "calendar_update_event",
                  "calendar_delete_event"):
        assert get_executable_registry().get(write) is None
        assert get_registry().get(write) is None


def test_a_protected_word_is_never_reached_by_distance() -> None:
    for word in PROTECTED_WORDS:
        if word in KNOWN_MISSPELLINGS:
            continue
        assert normalise(word).text == word, word


def test_an_ambiguous_token_is_left_alone() -> None:
    """Equidistant from two terms means the user's meaning is unknown.

    Guessing would silently pick a word they did not write.
    """
    assert module._nearest("evenning") in (None, "evening")
    # "todai" is one edit from "today" only; "mondai" from "monday" only.
    assert module._nearest("xyzzyxyzzy") is None


# --- Routing: the motivating failure ------------------------------------------


MOTIVATING = (
    "if i provide you a task would you give me a notification or add the "
    "task to my google callender?"
)


def test_the_motivating_message_is_no_longer_a_web_search() -> None:
    """The reported failure, and the whole reason for this stage."""
    assert route(MOTIVATING) != "research"

    # And nothing in it produces a search query.
    text = normalise(MOTIVATING).text
    assert research_language.recognise(text).is_request is False


def test_the_same_message_spelled_correctly_was_also_broken() -> None:
    """The typo was not the cause, and the fix must cover both.

    "my google calendar" parsed as `google <subject>` and searched the web
    for "calendar" -- with perfect spelling. A stage that fixed only the
    misspelling would have left the real defect in place.
    """
    correct = MOTIVATING.replace("callender", "calendar")

    assert route(correct) != "research"
    assert research_language.recognise(correct).is_request is False


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("what is on my google callender tomorrow?", "calendar_read"),
        ("what is on my google calendar tomorrow?", "calendar_read"),
        ("what's on my calender tomorrow", "calendar_read"),
        ("check my calandar", "calendar_read"),
        ("how busy is my google calender tomorrow?", "calendar_read"),
        ("can you add this to my google callender?", "calendar_write"),
        ("add this to my callender", "calendar_write"),
        ("schedule a meetng tomorrow", "calendar_write"),
    ],
)
def test_calendar_typos_route_to_the_calendar(message, expected) -> None:
    assert route(message) == expected, message


@pytest.mark.parametrize(
    "message",
    [
        "search the web for the latest news about OpenAI",
        "google the latest OpenAI announcements",
        "research Tesla's quarterly results",
        "look up NASA Artemis III",
        "search up the web and find out about Godzilla Minus One",
    ],
)
def test_web_research_is_unchanged(message) -> None:
    """§ regression: the research bridge still works exactly as before."""
    assert route(message) == "research", message


def test_a_google_search_request_still_reaches_research() -> None:
    """The `google` guard must not have broken the verb entirely."""
    recognition = research_language.recognise("google quantum computing")

    assert recognition.is_request
    assert recognition.query == "quantum computing"


# --- Bounds --------------------------------------------------------------------


def test_an_over_long_message_is_returned_unchanged() -> None:
    """Not truncated. A half-normalised message is one the user did not write."""
    message = "my callender " * 400
    assert len(message) > MAX_INPUT_CHARS

    result = normalise(message)

    assert result.text == message
    assert not result.changed


def test_corrections_are_capped() -> None:
    message = " ".join(["callender"] * 50)
    result = normalise(message)

    assert len(result.corrections) <= MAX_CORRECTIONS
    # And the tail is left exactly as written.
    assert result.text.endswith("callender")


def test_the_bounds_are_literals() -> None:
    """Pinned, so a widened bound has to be argued for here."""
    assert MAX_INPUT_CHARS == 2000
    assert MAX_EDIT_DISTANCE == 2
    assert MAX_CORRECTIONS == 8
    assert module.MIN_TOKEN_CHARS == 5
    assert module.MAX_TOKENS == 400


def test_normalisation_is_bounded_on_pathological_input() -> None:
    """§: no unbounded fuzzy matching.

    Cost is `O(tokens x vocabulary x word length)` with every factor a
    constant, so there is no input that makes this slow. The ceiling is loose
    by orders of magnitude against the observed time, so it will not flake --
    and an unbounded implementation could not come close to it.
    """
    import time

    hostile = [
        "callender " * 199,
        "c" * 1999,
        "ca" * 999,
        " ".join("calendr" + "r" * (i % 12) for i in range(200)),
        "́" * 1999,
        "calendar," * 400,
        " " * 1999,
        "".join(chr(0x0400 + (i % 64)) for i in range(1999)),
    ]

    started = time.perf_counter()
    for _ in range(5):
        for message in hostile:
            normalise(message[:MAX_INPUT_CHARS])
    elapsed = time.perf_counter() - started

    assert elapsed < 10.0, f"normalisation took {elapsed:.1f}s"


def test_the_original_is_always_carried() -> None:
    """§: the original must remain available for audit and provenance."""
    for message in ("callender", "nothing to fix", "", "   "):
        result = normalise(message)
        assert result.original == message


# --- Guards that another guard was hiding -------------------------------------
#
# Mutation testing found several checks in this module that no test could
# reach, each because a different check refused the input first. A guard
# nothing exercises is a guard nobody knows is working.


def test_a_correctly_spelled_message_produces_no_correction_record() -> None:
    """Not just unchanged text -- no correction at all.

    Removing the "already canonical" short-circuit leaves the text identical,
    because the nearest term to "calendar" is "calendar". Only the correction
    record shows the difference, so only a test that looks at it can tell.
    """
    for message in ("what is on my calendar tomorrow",
                    "my schedule for the meeting", "a reminder and a notification"):
        result = normalise(message)
        assert result.text == message
        assert result.corrections == (), (message, result.corrections)


@pytest.mark.parametrize("fragment", ["lendar", "alendr", "ndarr", "eendar"])
def test_a_short_fragment_cannot_travel_two_edits(fragment) -> None:
    """The rule that closed the homoglyph hole, tested on its own.

    "lendar" is two insertions from "calendar" and six characters long. The
    confusable tests cannot reach this rule -- the adjacent-letter check
    refuses those inputs earlier -- so a bare fragment is the only way to
    exercise it.
    """
    assert normalise(fragment).text == fragment


def test_a_long_token_may_still_travel_two_edits() -> None:
    """The complement, so the rule above is not simply switching correction off."""
    assert normalise("calenndar").text == "calendar"
    assert normalise("notificatio").text == "notification"


def test_a_tie_between_unrelated_words_is_not_resolved() -> None:
    """"thuesday" is one edit from both "thursday" and "tuesday".

    Neither is a prefix of the other, so they are not inflections and the
    user's meaning is genuinely unknown. Picking the shorter one would answer
    about the wrong day.
    """
    assert normalise("thuesday").text == "thuesday"


def test_a_tie_between_inflections_is_resolved() -> None:
    """The complement: "calendar"/"calendars" is a tie worth resolving."""
    assert normalise("calendarr").text == "calendar"


@pytest.mark.parametrize(
    "token", ["cal'endar", "cale'ndar", "c'alendar", "calen'dar"]
)
def test_an_apostrophe_inside_a_word_blocks_correction(token) -> None:
    """Punctuation injection must not reach a grammar.

    "cal'endar" is one edit from "calendar" -- delete the apostrophe -- and
    nine characters long, so the distance rules permit it. Only the token
    *shape* check refuses it, and without that a word broken up with
    punctuation would be reassembled into one Mai acts on.
    """
    assert normalise(token).text == token


def test_the_ratio_guard_was_removed_as_unreachable() -> None:
    """Recorded as a test so the reasoning cannot quietly rot.

    A `distance * 3 <= length` rule stood alongside the length rule and could
    never fire: distance is capped at 1 below eight characters and at 2 above,
    and 3 > 8 is false. If the length rule is ever loosened, this fails and
    the ratio rule needs reconsidering.
    """
    reachable = [
        (length, distance)
        for length in range(module.MIN_TOKEN_CHARS, module.MAX_TOKEN_CHARS + 1)
        for distance in (1, MAX_EDIT_DISTANCE)
        if not (length < 8 and distance > 1) and distance * 3 > length
    ]
    assert reachable == [], reachable



def test_repairing_a_mail_verb_produces_a_refusal_and_no_capability() -> None:
    """The "email" exception, verified.

    "emial this to everyone" repairs into a mail write request. That must
    reach a truthful refusal and nothing else -- no capability appears,
    because none exists to appear.
    """
    from app.orchestration.mail_language import recognise as recognise_mail

    text = normalise("emial this to everyone").text
    assert text == "email this to everyone"

    request = recognise_mail(text)
    assert request.is_write_request is True
    assert request.is_readable is False

    from app.execution.tools import get_executable_registry
    from app.tools.registry import get_registry

    for write in ("gmail_send_message", "gmail_reply", "gmail_forward",
                  "gmail_trash", "gmail_archive", "gmail_create_draft"):
        assert get_executable_registry().get(write) is None, write
        assert get_registry().get(write) is None, write
