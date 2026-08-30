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

# Words that mark a question as being about the past. Stage 3C uses this to
# decide whether superseded knowledge may be retrieved.
#
# Deliberately conservative. Common past-tense auxiliaries ("was", "were",
# "did") are excluded: "what was my name" asks about the present, and treating
# it as historical would surface retired knowledge on ordinary questions. Only
# words whose whole job is to point backwards are listed.
HISTORICAL_MARKERS = frozenset({
    "previously", "before", "earlier", "formerly", "historically",
    "originally", "past", "prior", "old", "older", "ago",
})

# Multi-word markers, matched against the normalised string rather than tokens.
HISTORICAL_PHRASES = ("used to", "no longer", "in the past", "back then")


@dataclass(frozen=True)
class NormalizedQuery:
    """The retrieval-facing view of a user message."""

    original: str
    normalized: str
    tokens: Tuple[str, ...] = field(default_factory=tuple)
    keywords: Tuple[str, ...] = field(default_factory=tuple)
    phrases: Tuple[str, ...] = field(default_factory=tuple)

    #: True when the question explicitly asks about the past. Stage 3C widens
    #: retrieval to superseded knowledge only when this is set.
    historical_intent: bool = False

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


def has_historical_intent(normalized: str) -> bool:
    """True when the query explicitly asks about the past.

    Word-boundary matching on the normalised string, so "before" matches and
    "beforehand" does not turn into a false positive through substring luck.

    This is a keyword test, not an intent model. It will miss "what did I use
    when I started Mai" and it will fire on "tell me about my old laptop" even
    though nothing there is superseded. Both failure modes are safe: a miss
    means normal current-state retrieval, and a false positive merely widens
    the candidate pool -- ranking still decides what survives.
    """
    if not normalized:
        return False

    tokens = set(normalized.split())
    if tokens & HISTORICAL_MARKERS:
        return True
    return any(phrase in normalized for phrase in HISTORICAL_PHRASES)


def analyse(raw: str) -> NormalizedQuery:
    """Full deterministic analysis of one user message."""
    normalized = normalize_query(raw)
    return NormalizedQuery(
        original=raw,
        normalized=normalized,
        tokens=tuple(normalized.split()),
        keywords=tuple(extract_keywords(normalized)),
        phrases=tuple(extract_phrases(normalized)),
        historical_intent=has_historical_intent(normalized),
    )
