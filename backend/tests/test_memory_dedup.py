"""Deduplication.

The hard cases are pairs that are lexically *more* similar than genuine
rewordings but describe different facts. Those are handled by the
discriminative-token guard, not by the threshold.
"""

import pytest

from app.memory.deduplication import (
    is_restatement,
    find_duplicate,
    has_conflicting_details,
    normalize,
    similarity,
)
from app.memory.models import Memory, MemoryType

THRESHOLD = 0.82


def memory(content: str, memory_type=MemoryType.PREFERENCE) -> Memory:
    return Memory(
        content=content,
        normalized_content=normalize(content),
        memory_type=memory_type,
        importance_score=7,
        confidence_score=0.9,
        source_conversation_id=None,
    )


# --- Normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("User prefers X.", "user prefers x"),
        ("  User   prefers   X!  ", "user prefers x"),
        ("USER PREFERS X", "user prefers x"),
        ("User, prefers: X;", "user prefers x"),
    ],
)
def test_normalization(raw, expected) -> None:
    assert normalize(raw) == expected


# --- Genuine duplicates -----------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        # The case named in the Stage 2A specification.
        ("User prefers practical explanations.", "The user likes practical explanations."),
        (
            "User prefers concise answers with practical examples.",
            "The user prefers concise answers and practical examples.",
        ),
        (
            "User wants to transition their career toward AI product development.",
            "User wants to move their career towards AI product development.",
        ),
        (
            "User decided to use PostgreSQL for Mai.",
            "User has decided to use PostgreSQL for the Mai project.",
        ),
        # Exact match, differing only in case and punctuation.
        ("User prefers dark mode.", "user prefers dark mode"),
    ],
)
def test_rewordings_are_detected_as_duplicates(existing, candidate) -> None:
    match = find_duplicate(candidate, [memory(existing)], THRESHOLD)
    assert match is not None, f"missed duplicate: {candidate!r}"


def test_exact_match_is_reported_as_exact() -> None:
    match = find_duplicate(
        "User prefers dark mode.", [memory("User prefers dark mode.")], THRESHOLD
    )
    assert match is not None
    assert match.reason == "exact"
    assert match.score == 1.0


# --- Distinct facts must survive --------------------------------------------


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        ("User prefers practical explanations.", "User prefers concise answers."),
        ("User works on AI projects.", "User is interested in startup investing."),
        (
            "User wants to transition into AI product development.",
            "User wants to explore VC roles.",
        ),
        # Near-identical wording, opposite meaning.
        ("User prefers dark mode.", "User prefers light mode."),
        # These score 0.97 lexically -- higher than any real duplicate above.
        ("User completed Stage 1 of Mai.", "User completed Stage 2 of Mai."),
        ("User lives in Bangalore.", "User lives in Berlin."),
        ("User decided to use PostgreSQL for Mai.", "User decided to use Groq for Mai."),
    ],
)
def test_different_facts_are_not_merged(existing, candidate) -> None:
    """Wrongly merging destroys a fact; the guard must prevent it."""
    match = find_duplicate(candidate, [memory(existing)], THRESHOLD)
    assert match is None, f"wrongly merged {candidate!r} into {existing!r}"


@pytest.mark.parametrize(
    ("left", "right", "conflicts"),
    [
        ("User completed Stage 1.", "User completed Stage 2.", True),
        ("User lives in Bangalore.", "User lives in Berlin.", True),
        ("User uses PostgreSQL.", "User uses Postgres.", True),
        ("User prefers practical explanations.", "User likes practical explanations.", False),
        ("User has 3 projects.", "User has 3 projects ongoing.", False),
    ],
)
def test_discriminative_token_guard(left, right, conflicts) -> None:
    assert has_conflicting_details(left, right) is conflicts


def test_similarity_is_bounded_and_symmetric() -> None:
    a, b = "User prefers practical explanations.", "The user likes practical explanations."
    assert 0.0 <= similarity(a, b) <= 1.0
    assert similarity(a, b) == similarity(b, a)
    assert similarity(a, a) == 1.0
    assert similarity("", "anything") == 0.0


# --- Scoping ----------------------------------------------------------------


def test_duplicates_are_only_sought_within_the_same_type() -> None:
    """Callers pass same-type memories; a goal is not a preference."""
    goals = [memory("User wants practical explanations.", MemoryType.GOAL)]
    # Same wording, but the caller supplied no preference memories to compare.
    assert find_duplicate("User wants practical explanations.", [], THRESHOLD) is None
    # Within the same type it is still found.
    assert find_duplicate("User wants practical explanations.", goals, THRESHOLD)


def test_best_match_wins_among_several() -> None:
    existing = [
        memory("User prefers tea."),
        memory("User prefers practical explanations."),
    ]
    match = find_duplicate(
        "The user likes practical explanations.", existing, THRESHOLD
    )
    assert match is not None
    assert match.existing.content == "User prefers practical explanations."


def test_empty_corpus_has_no_duplicates() -> None:
    assert find_duplicate("User prefers anything.", [], THRESHOLD) is None


# --- Restatements -----------------------------------------------------------
# Character-level similarity misses reordered clauses with synonym swaps. This
# case was found by running extraction against the real model: it scores 0.67,
# well under the threshold, but is plainly the same fact.


def test_reordered_restatement_is_detected() -> None:
    existing = memory("User prefers concise answers with practical examples.")
    match = find_duplicate(
        "User prefers practical examples and short answers.", [existing], THRESHOLD
    )
    assert match is not None
    assert match.reason == "restatement"


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        # A short statement inside a longer, more specific one is NOT a
        # duplicate -- the longer one carries information the shorter lacks.
        ("User likes coffee.", "User likes coffee in the morning before work."),
        ("User works on AI.", "User works on AI research and product strategy."),
        ("User uses Python.", "User uses Python for data pipelines and scripting."),
    ],
)
def test_a_more_specific_statement_is_not_a_restatement(existing, candidate) -> None:
    """Full containment alone must not merge; length has to be comparable."""
    assert find_duplicate(candidate, [memory(existing)], THRESHOLD) is None
    assert find_duplicate(existing, [memory(candidate)], THRESHOLD) is None


def test_restatement_still_respects_the_conflict_guard() -> None:
    """Reordering does not override a differing proper noun."""
    existing = memory("User decided to use PostgreSQL for the Mai project.")
    assert (
        find_duplicate(
            "User decided, for Mai, to use MySQL.", [existing], THRESHOLD
        )
        is None
    )


def test_restatement_is_symmetric() -> None:
    a = "User prefers concise answers with practical examples."
    b = "User prefers practical examples and short answers."
    assert is_restatement(a, b) is is_restatement(b, a) is True


# --- Synonym verbs ----------------------------------------------------------
# The specification's own near-duplicate example differs in BOTH the verb and
# the word order, which collapses character similarity to 0.52. Collapsing a
# small closed set of high-frequency verbs fixes it without loosening the
# threshold.


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        # The specification's example.
        ("User prefers practical explanations.", "User likes explanations that are practical."),
        ("User prefers detailed writeups.", "User enjoys detailed writeups."),
        ("User wants to learn Rust this year.", "User wishes to learn Rust this year."),
        ("User decided to use Postgres.", "User chose to use Postgres."),
    ],
)
def test_synonym_verbs_are_treated_as_the_same_fact(existing, candidate) -> None:
    assert find_duplicate(candidate, [memory(existing)], THRESHOLD) is not None


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        # Different strength of intent is a different fact, not a synonym.
        ("User wants to learn Rust.", "User decided to learn Rust."),
        ("User wants to use Postgres.", "User prefers to use Postgres."),
    ],
)
def test_verbs_of_different_strength_stay_distinct(existing, candidate) -> None:
    assert find_duplicate(candidate, [memory(existing)], THRESHOLD) is None


def test_synonyms_do_not_override_the_conflict_guard() -> None:
    """A synonym swap must not merge statements about different things."""
    assert (
        find_duplicate(
            "User likes Berlin.", [memory("User prefers Bangalore.")], THRESHOLD
        )
        is None
    )


def test_exact_matching_is_unaffected_by_synonyms() -> None:
    """normalize() stays faithful -- synonyms only affect token comparison."""
    assert normalize("User likes tea.") == "user likes tea"
    assert normalize("User prefers tea.") == "user prefers tea"
