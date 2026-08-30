"""Query normalization, keyword extraction, and ranking arithmetic.

These components are fully deterministic -- no model, no database -- so they
carry the tightest guarantees in the retrieval system.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.retrieval.query_normalizer import (
    analyse,
    extract_keywords,
    extract_phrases,
    normalize_query,
)
from app.retrieval.ranker import recency_score


# --- Normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("What technology stack did we decide for Mai?",
         "what technology stack did we decide for mai"),
        ("   MULTIPLE   spaces   ", "multiple spaces"),
        ("Punctuation!!! Removed???", "punctuation removed"),
        ("MiXeD CaSe", "mixed case"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalization(raw, expected) -> None:
    assert normalize_query(raw) == expected


def test_the_original_message_is_never_modified() -> None:
    """The model must receive exactly what the user typed."""
    original = "  What about Mai???  "
    analysis = analyse(original)
    assert analysis.original == original
    assert analysis.normalized != original


def test_normalization_is_idempotent() -> None:
    once = normalize_query("What about Mai???")
    assert normalize_query(once) == once


# --- Keyword extraction -----------------------------------------------------


def test_the_specifications_example() -> None:
    keywords = extract_keywords(normalize_query(
        "What technology stack did we decide for Mai?"
    ))
    assert keywords == ["technology", "stack", "decide", "mai"]


@pytest.mark.parametrize(
    "stopword", ["what", "the", "for", "did", "we", "is", "a", "and"]
)
def test_stopwords_are_removed(stopword) -> None:
    assert stopword not in extract_keywords(normalize_query(f"{stopword} database"))


def test_very_short_tokens_are_dropped() -> None:
    assert extract_keywords(normalize_query("a an x to Mai")) == ["mai"]


def test_duplicate_keywords_are_removed() -> None:
    keywords = extract_keywords(normalize_query("Mai and Mai and Mai again"))
    assert keywords.count("mai") == 1


def test_keyword_order_is_stable() -> None:
    query = "What database does Mai use for storage?"
    assert extract_keywords(normalize_query(query)) == extract_keywords(
        normalize_query(query)
    )


def test_an_empty_query_produces_nothing() -> None:
    analysis = analyse("")
    assert analysis.is_empty
    assert analysis.keywords == ()


def test_a_query_of_only_stopwords_produces_nothing() -> None:
    assert extract_keywords(normalize_query("what is the of and to")) == []


# --- Phrases ----------------------------------------------------------------


def test_multi_word_phrases_are_generated_longest_first() -> None:
    """Multi-word entity names are matched by exact phrase lookup."""
    phrases = extract_phrases(normalize_query("Tell me about AI product development"))
    assert "ai product development" in phrases
    # Longest first, so the most specific entity match is found first.
    assert phrases.index("ai product development") < phrases.index("ai")


def test_single_word_entities_are_still_matchable() -> None:
    assert "mai" in extract_phrases(normalize_query("How is Mai progressing?"))


def test_bare_stopwords_are_not_offered_as_phrases() -> None:
    phrases = extract_phrases(normalize_query("what is the plan"))
    assert "the" not in phrases
    assert "is" not in phrases


def test_phrases_are_deduplicated() -> None:
    phrases = extract_phrases(normalize_query("Mai Mai Mai"))
    assert phrases.count("mai") == 1


# --- Recency ----------------------------------------------------------------


def test_recency_decays_gently() -> None:
    """Old knowledge must not disappear merely for being old."""
    now = datetime.now(timezone.utc)
    assert recency_score(now, now) == pytest.approx(1.0)
    assert recency_score(now - timedelta(days=180), now) == pytest.approx(0.5, abs=0.01)
    # A year-old memory keeps a quarter of its recency signal.
    assert recency_score(now - timedelta(days=365), now) > 0.2


def test_recency_is_monotonic() -> None:
    now = datetime.now(timezone.utc)
    scores = [recency_score(now - timedelta(days=d), now) for d in (0, 30, 90, 365)]
    assert scores == sorted(scores, reverse=True)


def test_recency_is_bounded() -> None:
    now = datetime.now(timezone.utc)
    for days in (0, 1, 1000, 10000):
        assert 0.0 <= recency_score(now - timedelta(days=days), now) <= 1.0


def test_naive_timestamps_are_handled() -> None:
    """Database rows can come back without tzinfo."""
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert 0.0 <= recency_score(naive) <= 1.0


# --- Weights ----------------------------------------------------------------


def test_weights_sum_to_one_and_favour_relevance() -> None:
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    relevance = (
        settings.RETRIEVAL_WEIGHT_TEXT
        + settings.RETRIEVAL_WEIGHT_ENTITY
        + settings.RETRIEVAL_WEIGHT_RELATIONSHIP
    )
    metadata = (
        settings.RETRIEVAL_WEIGHT_IMPORTANCE
        + settings.RETRIEVAL_WEIGHT_CONFIDENCE
        + settings.RETRIEVAL_WEIGHT_RECENCY
    )
    assert relevance + metadata == pytest.approx(1.0)
    # Relevance must dominate, or an important-but-irrelevant memory wins.
    assert relevance > metadata
