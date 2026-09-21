"""Stage 5D.1: execution truth — the unit behaviour of claim adjudication.

Two halves under test: reading the authoritative record out of the existing
outcome enums, and deciding whether a piece of prose claims something that
record does not support.

The hardest requirement is **not** detecting fabrication. It is not detecting
it where there is none: a crude keyword blocker would refuse "I can search the
web for you", which is the single most useful sentence Mai says on a research
turn. Roughly half the cases below are legitimate prose that must pass.
"""

import pytest

from app.synthesis.execution_truth import (
    CLAIMABLE_STATES,
    Channel,
    ExecutionRecord,
    ExecutionState,
    claims,
    known_claim_channels,
    record_for_turn,
    render_note,
    truthful_reply,
    validate,
)


class FakeResult:
    """Minimal stand-in carrying an outcome and its evidence attribute."""

    def __init__(self, outcome, **blocks):
        self.outcome = outcome
        for name, value in blocks.items():
            setattr(self, name, value)


class Outcome:
    def __init__(self, value):
        self.value = value


# --- The record is read from the layers, never from prose ----------------------


@pytest.mark.parametrize(
    "outcome, block, expected",
    [
        ("completed", "results", ExecutionState.EXECUTED_SUCCESSFULLY),
        ("failed", "", ExecutionState.EXECUTED_FAILED),
        ("awaiting_confirmation", "", ExecutionState.PROPOSED_NOT_EXECUTED),
        ("declined", "", ExecutionState.PROPOSED_NOT_EXECUTED),
        ("abandoned", "", ExecutionState.PROPOSED_NOT_EXECUTED),
        ("disabled", "", ExecutionState.PROPOSED_NOT_EXECUTED),
        ("not_configured", "", ExecutionState.PROPOSED_NOT_EXECUTED),
        ("needs_clarification", "", ExecutionState.PROPOSED_NOT_EXECUTED),
        ("not_research", "", ExecutionState.NOT_REQUESTED),
    ],
)
def test_every_research_outcome_maps_to_a_state(outcome, block, expected) -> None:
    record = record_for_turn(
        research=FakeResult(Outcome(outcome), results_block=block)
    )
    assert record.web is expected


def test_an_unrecognised_outcome_is_unknown_not_success() -> None:
    """A state added to the enum later must license nothing until classified."""
    record = record_for_turn(
        research=FakeResult(Outcome("some_future_state"), results_block="x")
    )
    assert record.web is ExecutionState.UNKNOWN
    assert not record.may_claim(Channel.WEB)


def test_completed_without_results_is_unknown_not_success() -> None:
    """An outcome its own layer does not corroborate resolves downwards.

    "It completed" and "there is nothing to show for it" cannot both be true;
    treating the contradiction as success is how a claim gets licensed by a
    bug rather than by an execution.
    """
    record = record_for_turn(
        research=FakeResult(Outcome("completed"), results_block="")
    )
    assert record.web is ExecutionState.UNKNOWN


def test_unknown_never_becomes_claimable() -> None:
    """§6: "Unknown" MUST NOT become "successful"."""
    assert ExecutionState.UNKNOWN not in CLAIMABLE_STATES
    assert ExecutionState.EXECUTED_FAILED not in CLAIMABLE_STATES
    assert ExecutionState.PROPOSED_NOT_EXECUTED not in CLAIMABLE_STATES
    assert ExecutionState.NOT_REQUESTED not in CLAIMABLE_STATES
    assert CLAIMABLE_STATES == frozenset({ExecutionState.EXECUTED_SUCCESSFULLY})


def test_a_workflow_can_supply_the_evidence_for_a_channel() -> None:
    """One truth per channel, not one per code path."""
    record = record_for_turn(
        workflow=FakeResult(
            Outcome("completed"), researched=True, research_block="results",
            calendar_block="events",
        )
    )
    assert record.web is ExecutionState.EXECUTED_SUCCESSFULLY
    assert record.calendar is ExecutionState.EXECUTED_SUCCESSFULLY


def test_absent_layers_are_not_requested() -> None:
    record = record_for_turn()
    assert record == ExecutionRecord()
    assert not record.anything_executed


# --- Claims that must be detected -------------------------------------------------


#: The exact shape observed live during the Stage 5D.0 audit.
OBSERVED_FABRICATION = (
    '**Web Search Results for "Fable"**\n\n'
    "| # | Title | Snippet | Source |\n"
    "|---|-------|---------|--------|\n"
    "| 1 | **Fable (video game series)** | An action RPG | "
    "https://en.wikipedia.org/wiki/Fable |\n"
    "| 2 | **Fable** | A short story | https://example.com/fable |"
)

OBSERVED_FABRICATION_LIST = (
    'Here are the top results for "Fable" from a web search:\n\n'
    "1. **Fable (video game series)** – Wikipedia\n"
    "   https://en.wikipedia.org/wiki/Fable\n"
    "2. **Fable** – Britannica\n   https://www.britannica.com/art/fable"
)


@pytest.mark.parametrize(
    "text, channel",
    [
        (OBSERVED_FABRICATION, Channel.WEB),
        (OBSERVED_FABRICATION_LIST, Channel.WEB),
        ("I searched the web and found three articles.", Channel.WEB),
        ("I've just googled it for you.", Channel.WEB),
        ("I looked it up online.", Channel.WEB),
        ("I ran a web search on that.", Channel.WEB),
        ("According to the search, the answer is 42.", Channel.WEB),
        ("Based on my search, prices have risen.", Channel.WEB),
        ("The search returned four relevant pages.", Channel.WEB),
        ("I found these sources for you.", Channel.WEB),
        ("I checked your Gmail.", Channel.MAIL),
        ("I've read your inbox.", Channel.MAIL),
        ("You have 4 new messages.", Channel.MAIL),
        ("I retrieved your messages.", Channel.MAIL),
        ("I checked your calendar.", Channel.CALENDAR),
        ("On your calendar, you have a dentist appointment.", Channel.CALENDAR),
        ("Your schedule for tomorrow is busy.", Channel.CALENDAR),
    ],
)
def test_an_assertion_of_execution_is_a_claim(text, channel) -> None:
    assert channel in claims(text), text


# --- Prose that must NOT be flagged -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # §18's own examples.
        "I can help you understand how web search works.",
        "You asked me to search Fable, but I haven't actually searched it yet.",
        # Offers -- the most important false positive to avoid.
        "I can search the web for **Fable** and use the results to answer you.",
        "Would you like me to search the web for that?",
        "Shall I check your calendar?",
        "I could look it up if you want me to.",
        "I'll search the web once you confirm.",
        "Let me know if you want me to check your email.",
        # Negations.
        "I haven't searched the web for this.",
        "I did not check your calendar.",
        "I cannot read your Gmail until you connect it.",
        "I'm not able to search right now.",
        "Instead of searching, I can tell you what I already know.",
        # Conditionals and questions.
        "If I search the web, I can give you current information.",
        "Do you want me to check your inbox?",
        # Ordinary knowledge, no claim at all.
        "Fable is a video game series developed by Lionhead Studios.",
        "A fable is a short story that conveys a moral lesson.",
        "Search engines index pages using automated crawlers.",
        "Calendars are usually synchronised over CalDAV.",
        "Email uses SMTP for sending and IMAP for reading.",
    ],
)
def test_legitimate_prose_is_not_a_claim(text) -> None:
    assert claims(text) == frozenset(), f"false positive: {text!r}"


def test_a_mixed_reply_flags_only_the_channel_it_asserts() -> None:
    """Per clause, not per response."""
    text = "I searched the web and found three articles. I haven't checked your email."
    found = claims(text)
    assert Channel.WEB in found
    assert Channel.MAIL not in found


def test_empty_and_whitespace_are_not_claims() -> None:
    assert claims("") == frozenset()
    assert claims("   \n  ") == frozenset()


def test_an_enormous_response_is_not_scanned() -> None:
    assert claims("I searched the web. " * 40_000) == frozenset()


# --- The guards, tested on the cases that actually reach them --------------------
#
# Mutation testing found the first version of this file had no case that
# exercised the negation, modality or question guards at all: the verb-tense
# patterns already exclude "I can search" and "I haven't searched", so removing
# the guards changed nothing the suite looked at. The sentences below are the
# ones that *do* reach them -- hedged prose containing a result-presentation
# phrase, which matches on wording alone.


@pytest.mark.parametrize(
    "text",
    [
        "I will not present the search results for Fable.",
        "I cannot show you search results for Fable.",
        "Here are the results I would find if I searched.",
        "I am not going to invent search results for you.",
        "Without a search I have no results for Fable.",
    ],
)
def test_a_hedged_sentence_containing_a_claim_phrase_is_not_a_claim(text) -> None:
    """The negation and modality guard, on the only cases that reach it."""
    assert claims(text) == frozenset(), f"false positive: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Would you like to see the search results for Fable?",
        "Shall I tell you what the search returned?",
        "Should I check your calendar for tomorrow?",
        # No modal word at all, so only the "?" guard can stop these. Without
        # them the modality guard masks the question guard completely and
        # removing it changes nothing the suite looks at.
        "Which of the search results for Fable interests you?",
        "The search results for Fable, or the ones for Asta?",
    ],
)
def test_a_question_containing_a_claim_phrase_is_not_a_claim(text) -> None:
    """A question is not a report. The `?` guard, on cases that reach it."""
    assert claims(text) == frozenset(), f"false positive: {text!r}"


def test_a_bare_results_phrase_is_a_claim_on_its_own() -> None:
    """Without any of the other wordings, so the phrase family is pinned."""
    assert Channel.WEB in claims("Here are the top 3 results:\n\n1. Fable\n2. Asta")


def test_a_source_table_with_links_is_a_claim_without_any_claim_phrase() -> None:
    """The fabricated-citation signal, isolated.

    The observed fabrication carried both a results heading *and* a table, so
    a test using it verbatim cannot tell which signal fired. This one has no
    claim wording at all -- only a rendered table of links presented as
    sources, which is what a fabricated citation looks like when the prose
    around it is careful.
    """
    table = (
        "Some notes on Fable:\n\n"
        "| # | Title | Source |\n|---|---|---|\n"
        "| 1 | Fable | https://en.wikipedia.org/wiki/Fable |\n"
        "| 2 | Asta | https://example.com/asta |"
    )
    assert Channel.WEB in claims(table)


def test_a_source_table_without_links_is_not_a_claim() -> None:
    """The bar is deliberately high: a table alone is ordinary writing."""
    table = "| Name | Source |\n|---|---|\n| Fable | folklore |"
    assert claims(table) == frozenset()


def test_the_note_reports_a_failed_execution_as_failed() -> None:
    """Per-state wording, pinned.

    A note that described a failed search as having happened would tell the
    model the opposite of the truth -- and the note is the preventive half of
    the whole stage.
    """
    note = render_note(ExecutionRecord(web=ExecutionState.EXECUTED_FAILED))
    assert "FAILED" in note
    assert "DID happen" not in note


def test_the_note_forbids_inventing_sources() -> None:
    """The one instruction that names the observed failure mode.

    Asserts the *prohibition*, not just the vocabulary: a note that listed
    "sources, URLs, titles, snippets" and then permitted them would contain
    every word this test used to look for while saying the opposite. Mutation
    testing found exactly that hole.
    """
    note = render_note(ExecutionRecord())
    for word in ("sources", "URLs", "titles", "snippets"):
        assert word in note
    assert "Do not present results" in note
    assert "did not run" in note


# --- The verdict ---------------------------------------------------------------------


def test_a_supported_claim_is_accepted() -> None:
    record = ExecutionRecord(web=ExecutionState.EXECUTED_SUCCESSFULLY)
    verdict = validate("I searched the web and found three articles.", record)
    assert verdict.ok
    assert verdict.violations == ()


@pytest.mark.parametrize(
    "state",
    [
        ExecutionState.NOT_REQUESTED,
        ExecutionState.PROPOSED_NOT_EXECUTED,
        ExecutionState.EXECUTED_FAILED,
        ExecutionState.UNKNOWN,
    ],
)
def test_an_unsupported_claim_is_refused(state) -> None:
    record = ExecutionRecord(web=state)
    verdict = validate(OBSERVED_FABRICATION, record)
    assert not verdict.ok
    assert verdict.violations == (Channel.WEB,)
    assert verdict.reason == "unsupported_execution_claim"


def test_prose_with_no_claim_is_always_accepted() -> None:
    verdict = validate("Fable is a video game series.", ExecutionRecord())
    assert verdict.ok
    assert verdict.reason == "no_execution_claim"


def test_a_claim_about_one_channel_does_not_license_another() -> None:
    record = ExecutionRecord(web=ExecutionState.EXECUTED_SUCCESSFULLY)
    verdict = validate("I checked your calendar and you are free.", record)
    assert not verdict.ok
    assert verdict.violations == (Channel.CALENDAR,)


# --- Application text ---------------------------------------------------------------


def test_the_note_always_states_every_channel_including_the_negative() -> None:
    """The silence is what the model filled; there must be no silence."""
    note = render_note(ExecutionRecord())
    assert "web search" in note
    assert "email read" in note
    assert "calendar read" in note
    assert note.count("did NOT happen") == 3


def test_the_note_marks_a_real_execution_as_having_happened() -> None:
    note = render_note(ExecutionRecord(web=ExecutionState.EXECUTED_SUCCESSFULLY))
    assert "DID happen" in note


def test_the_note_does_not_forbid_answering() -> None:
    """A model that refuses to answer once told it has not searched is a
    regression, not a fix."""
    note = render_note(ExecutionRecord())
    assert "your own knowledge" in note


def test_the_truthful_reply_claims_nothing() -> None:
    for state in ExecutionState:
        record = ExecutionRecord(web=state)
        reply = truthful_reply(record, (Channel.WEB,))
        assert claims(reply) == frozenset(), f"{state}: {reply!r}"


def test_the_truthful_reply_distinguishes_failure_from_never_ran() -> None:
    failed = truthful_reply(
        ExecutionRecord(web=ExecutionState.EXECUTED_FAILED), (Channel.WEB,)
    )
    never = truthful_reply(
        ExecutionRecord(web=ExecutionState.PROPOSED_NOT_EXECUTED), (Channel.WEB,)
    )
    assert "failed" in failed
    assert "not actually done" in never
    assert failed != never


def test_the_adjudicated_channels_are_pinned() -> None:
    """A capability added later is not adjudicated until someone adds it."""
    assert known_claim_channels() == (Channel.WEB, Channel.MAIL, Channel.CALENDAR)
    assert len(list(Channel)) == 3
