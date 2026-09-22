"""Stage 5D.2: resolving what the user is talking about.

Unit behaviour of the resolver. Integration and security live in
`tests/test_context_continuity.py` and
`tests/security/test_context_resolution_security.py`.

The canonical case is the one the Stage 5D.0 audit reproduced live:

    "Is Fable better or Asta?"  →  "Search up the net and let me know."

which previously searched for **"let me know"**.
"""

import pytest

from app.orchestration.resolution import (
    MAX_SUBJECT_CHARS,
    MAX_USER_TURNS,
    Ambiguity,
    ResolutionSource,
    UserTurn,
    is_substantive,
    resolve,
    subject_of,
)


def turns(*messages):
    return [UserTurn(content=message) for message in messages]


# --- A. The exact failure ------------------------------------------------------


def test_the_fable_asta_follow_up_inherits_the_comparison() -> None:
    """§A: the canonical case. Previously resolved to "let me know"."""
    result = resolve(
        "search up the net and let me know", turns("Is Fable better or Asta?")
    )
    assert result.is_resolved
    assert result.subject == "Fable vs Asta"
    assert result.source is ResolutionSource.RECENT_USER_CONTEXT
    assert "let me know" not in result.subject


# --- B–F. References ------------------------------------------------------------


@pytest.mark.parametrize(
    "follow_up",
    [
        "search it", "look it up", "check that", "search this",
        "tell me more", "what about it", "how much is it",
    ],
)
def test_a_plain_reference_inherits_the_topic(follow_up) -> None:
    result = resolve(follow_up, turns("Tell me about Fable."))
    assert result.is_resolved
    assert result.subject.startswith("Fable")


def test_which_one_resolves_to_the_comparison(_=None) -> None:
    """§D: the whole set, not one arbitrary member."""
    result = resolve("which one is better?", turns("Compare Fable and Asta."))
    assert result.is_resolved
    assert "Fable" in result.subject and "Asta" in result.subject
    assert result.source is ResolutionSource.PENDING_USER_QUESTION


@pytest.mark.parametrize(
    "follow_up, expected",
    [
        ("what about the second one?", "Asta"),
        ("what about the first one?", "Fable"),
        ("what about the third one?", "Claude"),
        ("what about the last one?", "Claude"),
    ],
)
def test_an_ordinal_picks_one_member(follow_up, expected) -> None:
    """§E: "the second one" of a list the user themselves gave."""
    result = resolve(follow_up, turns("Compare Fable, Asta and Claude."))
    assert result.is_resolved
    assert result.subject == expected
    assert result.source is ResolutionSource.EXPLICIT_USER_REFERENCE


def test_multiple_ordinals_pick_several(_=None) -> None:
    """§F."""
    result = resolve(
        "what about the first and third?", turns("Compare Fable, Asta and Claude.")
    )
    assert result.is_resolved
    assert result.subject == "Fable and Claude"


# --- G–I. Ellipsis and non-research follow-ups -----------------------------------


def test_an_ellipsis_qualifies_the_topic_rather_than_replacing_it() -> None:
    """§G: "what about pricing?" is asking about *Fable's* pricing."""
    result = resolve("what about pricing?", turns("Tell me about Fable."))
    assert result.is_resolved
    assert result.subject == "Fable pricing"


def test_a_research_follow_up_inherits_the_topic() -> None:
    """§H."""
    result = resolve(
        "search the web and tell me more", turns("Tell me about Fable.")
    )
    assert result.is_resolved
    assert result.subject == "Fable"


def test_a_non_research_follow_up_resolves_the_same_way() -> None:
    """§I: resolution is about meaning, not about routing."""
    result = resolve("tell me more", turns("Tell me about Fable."))
    assert result.is_resolved
    assert result.subject == "Fable"


# --- J–K. Topic switching ---------------------------------------------------------


def test_a_topic_switch_prevents_stale_leakage() -> None:
    """§J: the critical anti-staleness case.

    Resolution takes the most recent user turn that *states a subject*, which
    is not the same as "the last entity mentioned". The switching turn is
    itself the most recent statement, so it becomes the topic -- and Fable,
    which the user has moved on from, cannot come back.
    """
    result = resolve(
        "what about pricing?",
        turns("Tell me about Fable.", "What is the weather in Bangalore?"),
    )
    assert result.is_resolved
    assert "Fable" not in result.subject
    assert "Bangalore" in result.subject


def test_an_explicit_return_outranks_the_recent_topic() -> None:
    """§K: the user named it themselves, so nothing is inferred."""
    result = resolve(
        "going back to Fable, what about pricing?",
        turns("Tell me about Fable.", "What is the weather in Bangalore?"),
    )
    assert result.is_resolved
    assert result.subject == "Fable"
    assert result.source is ResolutionSource.EXPLICIT_USER_REFERENCE


def test_what_about_is_not_read_as_an_explicit_return() -> None:
    """An ellipsis is not a topic switch.

    An earlier version matched a bare "about", so "what about the second one?"
    was read as an explicit reference to the topic "the second one" -- and
    skipped ordinal handling entirely.
    """
    result = resolve(
        "what about the second one?", turns("Compare Fable, Asta and Claude.")
    )
    assert result.subject == "Asta"


# --- L–N. Refusing to guess --------------------------------------------------------


def test_no_context_resolves_to_nothing() -> None:
    """§M: "search it" with nothing before it must not invent a subject."""
    result = resolve("search it", [])
    assert not result.is_resolved
    assert result.subject == ""
    assert result.ambiguity is Ambiguity.NO_ANTECEDENT


def test_an_ordinal_outside_the_list_is_ambiguous_not_a_guess() -> None:
    """§L: "the fourth one" of three things is a misunderstanding."""
    result = resolve(
        "what about the fourth one?", turns("Compare Fable, Asta and Claude.")
    )
    assert not result.is_resolved
    assert result.ambiguity is Ambiguity.AMBIGUOUS


def test_which_one_with_no_comparison_does_not_resolve_to_a_single_topic() -> None:
    """§N: there is no "one" to pick when the user named a single thing."""
    result = resolve("which one is better?", turns("Tell me about Fable."))
    # It may inherit Fable as the topic, but it must not claim a comparison.
    assert result.source is not ResolutionSource.PENDING_USER_QUESTION


def test_turns_with_no_subject_are_skipped() -> None:
    """A conversation of pure pleasantries offers nothing to inherit."""
    result = resolve("search it", turns("hello", "thanks!", "ok"))
    assert not result.is_resolved


# --- Bounds -------------------------------------------------------------------------


def test_the_bounds_are_pinned_to_literals() -> None:
    """A bound is not bounded if the test reads it from the thing it guards.

    `test_a_topic_beyond_the_window_is_not_reachable` computed its filler from
    `MAX_USER_TURNS`, so raising the constant raised the filler too and the
    test still passed -- mutation testing caught exactly that. The numbers are
    literals here, so widening either bound means editing this test and saying
    why.

    Six turns is roughly three exchanges: far enough back to catch a genuine
    follow-up, near enough that a topic the user left behind is out of reach.
    Two hundred characters is below the search layer's own query bound.
    """
    assert MAX_USER_TURNS == 6
    assert MAX_SUBJECT_CHARS == 200


def test_only_a_bounded_window_is_inspected() -> None:
    """§19: a long conversation must cost what a short one does."""
    history = turns(*[f"Tell me about Topic{i}." for i in range(40)])
    result = resolve("search it", history)
    assert result.is_resolved
    # The most recent, not the oldest, and nothing beyond the window.
    assert result.subject == "Topic39"


def test_a_topic_beyond_the_window_is_not_reachable() -> None:
    """A literal filler count, so widening the bound fails this test."""
    older = turns("Tell me about Fable.")
    filler = turns(*["ok" for _ in range(8)])
    result = resolve("search it", older + filler)
    assert not result.is_resolved, "reached past the window"


def test_the_subject_is_length_bounded() -> None:
    """A literal cap, for the same reason as the window bound above."""
    long_topic = "Tell me about " + ("x" * 900)
    result = resolve("search it", turns(long_topic))
    assert len(result.subject) <= 200


def test_an_empty_message_resolves_to_nothing() -> None:
    assert not resolve("", turns("Tell me about Fable.")).is_resolved
    assert not resolve("   ", turns("Tell me about Fable.")).is_resolved


# --- The substantive vocabulary -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["let me know", "it", "this", "tell me more", "search it", "look it up",
     "and tell me", "please", "more info", "the same",
     # Bare comparatives and ordinals: they point at a topic rather than
     # being one. Live verification found "which one is better" accepted as a
     # literal search query because "better" was not in the vocabulary.
     "which one is better", "the second one", "the first and third",
     "which is faster", "yes", "ok thanks"],
)
def test_a_phrase_that_names_nothing_is_not_substantive(text) -> None:
    assert not is_substantive(text)


@pytest.mark.parametrize(
    "text",
    ["Fable", "the latest news about OpenAI", "weather in Bangalore",
     "quantum computing", "Fable vs Asta", "pricing"],
)
def test_a_phrase_that_names_something_is_substantive(text) -> None:
    assert is_substantive(text)


# --- Subject extraction ----------------------------------------------------------------


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Tell me about Fable.", "Fable"),
        ("tell me more about Fable", "Fable"),
        ("What is Fable?", "Fable"),
        ("Compare Fable and Asta.", "Fable and Asta"),
        ("Is Fable better or Asta?", "Fable vs Asta"),
        ("Fable vs Asta", "Fable vs Asta"),
        ("What is the weather in Bangalore?", "weather in Bangalore"),
    ],
)
def test_a_subject_is_read_from_a_user_turn(message, expected) -> None:
    subject = subject_of(message)
    assert subject is not None
    assert subject.text == expected


@pytest.mark.parametrize(
    "message", ["hello", "thanks!", "ok", "yes", "let me know", "search it"]
)
def test_a_turn_that_states_no_topic_yields_no_subject(message) -> None:
    assert subject_of(message) is None


def test_a_list_is_split_into_its_members() -> None:
    subject = subject_of("Compare Fable, Asta and Claude.")
    assert subject is not None
    assert subject.entities == ("Fable", "Asta", "Claude")
