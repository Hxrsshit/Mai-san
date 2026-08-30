"""First-level memory deduplication.

Stage 2A uses no embeddings, so similarity is lexical. Pure lexical scoring
has a failure mode worth stating plainly:

    "User completed Stage 1 of Mai."  vs  "User completed Stage 2 of Mai."

scores *higher* (0.97) than a genuine reworded duplicate such as

    "User prefers practical explanations."  vs  "The user likes practical
    explanations."                                                   (0.83)

No threshold can separate those, so a threshold alone is not enough. A guard
runs first: when two texts disagree on a *discriminative* token -- a number or
a proper noun present in one and not the other -- they are treated as
different facts no matter how similar the wording.

The bias is deliberate. Wrongly merging two memories destroys a fact;
wrongly keeping a near-duplicate only adds mild noise. When uncertain, we keep
both.
"""

import difflib
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Set

from app.core.logging import get_logger
from app.memory.models import Memory

logger = get_logger(__name__)

# Removed before comparison: they carry no distinguishing meaning, and every
# memory begins with "User ...", which would otherwise inflate every score.
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "and", "or", "that", "this", "these", "those",
    "their", "they", "them", "has", "have", "had", "in", "on", "at", "with",
    "as", "it", "its", "user", "users",
})

# A small, closed set of verbs that dominate memory statements and mean the
# same thing for deduplication purposes. Collapsing them catches rewordings
# that differ in both verb and word order, e.g.
#   "User prefers practical explanations."
#   "User likes explanations that are practical."
# which otherwise score only 0.52.
#
# Deliberately narrow. Verbs of *different strength* are kept apart --
# "wants to learn Rust" and "decided to learn Rust" are different facts --
# so each group maps to its own canonical form rather than one blob.
_VERB_SYNONYMS = {
    "likes": "prefers",
    "enjoys": "prefers",
    "loves": "prefers",
    "favours": "prefers",
    "favors": "prefers",
    "wishes": "wants",
    "desires": "wants",
    "hopes": "wants",
    "chose": "decided",
    "selected": "decided",
    "picked": "decided",
}

_PUNCTUATION = re.compile(r"[^\w\s]")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def normalize(text: str) -> str:
    """Case-folded, punctuation-free, whitespace-collapsed form.

    Used both for exact-match lookup and as the input to similarity scoring.
    """
    lowered = _PUNCTUATION.sub(" ", text.lower().strip())
    return " ".join(lowered.split())


def _content_tokens(text: str) -> Set[str]:
    """Meaningful tokens, with synonym verbs collapsed to a canonical form.

    Applied only to token-set comparison. `normalize()` itself stays a faithful
    normalisation, because it backs exact-duplicate matching where a synonym
    swap genuinely is a different string.
    """
    return {
        _VERB_SYNONYMS.get(word, word)
        for word in normalize(text).split()
        if word not in _STOPWORDS
    }


def _numbers(text: str) -> Set[str]:
    return set(_NUMBER.findall(text))


def _proper_nouns(text: str) -> Set[str]:
    """Capitalised words that are not merely sentence-initial.

    Product and place names are exactly the tokens that distinguish otherwise
    identical statements ("Bangalore" vs "Berlin").
    """
    words = re.findall(r"\b[A-Za-z][\w]*\b", text)
    return {
        w.lower()
        for index, w in enumerate(words)
        if index > 0 and w[0].isupper() and w.lower() not in _STOPWORDS
    }


def has_conflicting_details(left: str, right: str) -> bool:
    """True when the two texts disagree on a number or a proper noun.

    Such a disagreement means different facts, regardless of similarity.
    """
    left_numbers, right_numbers = _numbers(left), _numbers(right)
    if left_numbers != right_numbers:
        return True

    left_nouns, right_nouns = _proper_nouns(left), _proper_nouns(right)
    return left_nouns != right_nouns


# A reworded restatement shares nearly all of its content words with the
# original AND is of comparable length. Both conditions are needed: a short
# statement fully contained in a longer one ("User likes coffee." inside "User
# likes coffee in the morning before work.") scores 1.0 on containment alone,
# yet the longer one carries information the shorter does not, so merging them
# would lose it.
_CONTAINMENT_THRESHOLD = 0.80
_LENGTH_RATIO_THRESHOLD = 0.60


def _containment(left_tokens: Set[str], right_tokens: Set[str]) -> float:
    """Share of the smaller token set that also appears in the larger."""
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _length_ratio(left_tokens: Set[str], right_tokens: Set[str]) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    return min(len(left_tokens), len(right_tokens)) / max(
        len(left_tokens), len(right_tokens)
    )


def is_restatement(left: str, right: str) -> bool:
    """True when the two texts say the same thing in a different order.

    Catches reordering plus synonym swaps that character-level similarity
    misses, e.g. "User prefers concise answers with practical examples." and
    "User prefers practical examples and short answers." (similarity 0.67,
    containment 0.80).
    """
    left_tokens, right_tokens = _content_tokens(left), _content_tokens(right)
    return (
        _containment(left_tokens, right_tokens) >= _CONTAINMENT_THRESHOLD
        and _length_ratio(left_tokens, right_tokens) >= _LENGTH_RATIO_THRESHOLD
    )


def similarity(left: str, right: str) -> float:
    """Lexical similarity in [0, 1].

    The maximum of a character-level ratio (robust to small rewordings) and a
    content-token Jaccard score (robust to reordering).
    """
    left_norm, right_norm = normalize(left), normalize(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0

    sequence_ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()

    left_tokens, right_tokens = _content_tokens(left), _content_tokens(right)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0

    return max(sequence_ratio, jaccard)


@dataclass(frozen=True)
class DuplicateMatch:
    """Why a candidate was considered a duplicate."""

    existing: Memory
    score: float
    reason: str  # "exact" | "similar" | "restatement"


def find_duplicate(
    content: str,
    existing: Iterable[Memory],
    threshold: float,
) -> Optional[DuplicateMatch]:
    """Return the best duplicate match among `existing`, if any.

    Callers should pass only memories of the same type: a goal and a
    preference worded alike are different kinds of fact.
    """
    normalized = normalize(content)
    best: Optional[DuplicateMatch] = None

    for memory in existing:
        if normalize(memory.content) == normalized:
            # Exact match after normalisation -- no guard needed.
            return DuplicateMatch(existing=memory, score=1.0, reason="exact")

        if has_conflicting_details(content, memory.content):
            # Differs on a number or proper noun: a different fact.
            continue

        score = similarity(content, memory.content)
        if score >= threshold:
            if best is None or score > best.score:
                best = DuplicateMatch(existing=memory, score=score, reason="similar")
        elif is_restatement(content, memory.content):
            # Same content words, different order or wording.
            if best is None:
                best = DuplicateMatch(
                    existing=memory, score=score, reason="restatement"
                )

    return best
