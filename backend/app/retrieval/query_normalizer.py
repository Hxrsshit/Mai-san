"""Query normalization and deterministic keyword extraction.

Nothing here calls a model. Retrieval must be reproducible: the same query
always produces the same keywords, the same n-grams, and therefore the same
candidates.

The user's original message is never modified. Normalization produces a
*separate* string used only for matching; the model still receives the message
exactly as typed.
"""

import re
from dataclasses import dataclass, field
from typing import List, Set, Tuple

# Words that carry no retrieval signal. Kept deliberately small: an
# over-eager stopword list silently drops meaningful terms.
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "as",
    "at", "by", "for", "from", "in", "into", "of", "on", "onto", "to", "with",
    "about", "over", "under", "is", "are", "was", "were", "be", "been", "being",
    "am", "do", "does", "did", "doing", "have", "has", "had", "having",
    "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "yours",
    "he", "him", "his", "she", "her", "it", "its", "they", "them", "their",
    "this", "that", "these", "those", "what", "which", "who", "whom", "whose",
    "when", "where", "why", "how", "all", "any", "both", "each", "more",
    "most", "some", "such", "no", "nor", "not", "only", "own", "same", "too",
    "very", "can", "will", "just", "should", "now", "would", "could", "there",
    "here", "again", "also", "still", "get", "got", "tell", "told", "say",
    "said", "please", "thanks", "thank",
})

# Tokens shorter than this are noise ("a", "of", "x").
MIN_KEYWORD_LENGTH = 3

# Longest multi-word phrase considered when matching entity names.
# "AI Product Development" is three words; four is past the point of
# diminishing returns and multiplies the lookup set.
MAX_PHRASE_WORDS = 4

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedQuery:
    """The retrieval-facing view of a user message."""

    original: str
    normalized: str
    tokens: Tuple[str, ...] = field(default_factory=tuple)
    keywords: Tuple[str, ...] = field(default_factory=tuple)
    phrases: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.keywords and not self.phrases


def normalize_query(raw: str) -> str:
    """Case-folded, punctuation-free, whitespace-collapsed form.

    Used only for matching. Short tokens like "AI" survive because
    normalization does not filter -- that is the keyword step's job.
    """
    if not raw:
        return ""
    lowered = _PUNCTUATION.sub(" ", raw.lower())
    return _WHITESPACE.sub(" ", lowered).strip()


def extract_keywords(normalized: str) -> List[str]:
    """Meaningful single tokens, stopwords and very short tokens removed.

    Order is preserved and duplicates dropped, so the result is stable.
    """
    seen: Set[str] = set()
    keywords: List[str] = []
    for token in normalized.split():
        if token in STOPWORDS or len(token) < MIN_KEYWORD_LENGTH:
            continue
        if not any(character.isalnum() for character in token):
            continue
        if token in seen:
            continue
        seen.add(token)
        keywords.append(token)
    return keywords


def extract_phrases(normalized: str) -> List[str]:
    """Contiguous word n-grams, longest first.

    These are matched against entity names by exact lookup, which is what
    lets a multi-word entity like "AI Product Development" be recognised
    without any fuzzy matching. Longest-first ordering means the most
    specific match is found before a shorter one inside it.
    """
    words = normalized.split()
    if not words:
        return []

    seen: Set[str] = set()
    phrases: List[str] = []
    for size in range(min(MAX_PHRASE_WORDS, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start : start + size])
            # A single stopword is never an entity name.
            if size == 1 and (phrase in STOPWORDS or len(phrase) < 2):
                continue
            if phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
    return phrases


def analyse(raw: str) -> NormalizedQuery:
    """Full deterministic analysis of one user message."""
    normalized = normalize_query(raw)
    return NormalizedQuery(
        original=raw,
        normalized=normalized,
        tokens=tuple(normalized.split()),
        keywords=tuple(extract_keywords(normalized)),
        phrases=tuple(extract_phrases(normalized)),
    )
