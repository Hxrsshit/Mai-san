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
from typing import List, Sequence, Set, Tuple

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


#: Irregular plurals, both directions. Substring matching already lets a
#: singular keyword find its regular plural ("partner" in "partners"); these
#: are the forms it cannot reach that way ("woman" is not in "women").
#: "man"/"men" are left out: `%man%` would also match "human", "manage" and
#: "woman", flooding the candidate pool for a gain substring matching on
#: "men" mostly already has.
_IRREGULAR_FORMS = {
    "woman": "women", "child": "children", "person": "people",
    "wife": "wives", "life": "lives", "foot": "feet", "tooth": "teeth",
    "mouse": "mice", "knife": "knives",
}
_IRREGULAR = {**_IRREGULAR_FORMS, **{v: k for k, v in _IRREGULAR_FORMS.items()}}


def keyword_variants(keyword: str) -> Tuple[str, ...]:
    """The keyword plus the inflected forms a substring match would miss.

    Deterministic and small on purpose: irregular plurals in both directions,
    and a plural keyword reduced to its singular (which then also matches the
    plural by substring). No stemming library and no synonyms -- a question
    worded entirely differently from the stored memory is still a miss, which
    is the documented limit of lexical retrieval.
    """
    forms = [keyword]
    if keyword in _IRREGULAR:
        forms.append(_IRREGULAR[keyword])
    elif len(keyword) > 4 and keyword.endswith("ies"):
        forms.append(keyword[:-3] + "y")
    elif len(keyword) > 4 and keyword.endswith(("ches", "shes", "sses", "xes", "zes")):
        forms.append(keyword[:-2])
    elif len(keyword) > 3 and keyword.endswith("s") and not keyword.endswith(("ss", "us", "is")):
        forms.append(keyword[:-1])
    return tuple(dict.fromkeys(form for form in forms if len(form) >= MIN_KEYWORD_LENGTH))


#: Words that say *that* the user likes something, not *what*. One explicit
#: family, matched as equivalents ("prefer" finds "favourite" and "liked"),
#: and weighted low: a memory sharing only this word with the question shares
#: nothing about its subject. Deliberately small -- not a thesaurus.
PREFERENCE_TERMS = frozenset({
    "prefer", "prefers", "preferred", "preferring", "preference", "preferences",
    "favourite", "favourites", "favorite", "favorites",
    "like", "likes", "liked", "liking", "enjoy", "enjoys", "enjoyed", "enjoying",
    "love", "loves", "loved", "loving", "ideal", "attracted", "attraction",
})

#: Words that frame a question ("what *kind* of tea") without naming its
#: subject. Weighted lowest, and never enough on their own to make a memory
#: a candidate.
FRAME_TERMS = frozenset({"kind", "kinds", "type", "types", "sort", "sorts"})

#: Concept groups for subjects that people name with different words. A
#: different word from the same group counts as a match only when the memory
#: also expresses a preference, so "my sister is a woman engineer" does not
#: answer "what kind of partner do I prefer?". One group today, explicitly.
CONCEPT_GROUPS = (
    frozenset({
        "partner", "partners", "woman", "women", "girlfriend", "girlfriends",
        "boyfriend", "boyfriends", "spouse", "spouses", "wife", "wives",
        "husband", "husbands",
    }),
)

#: Relative weight of a matched keyword in text relevance, by kind.
TERM_WEIGHTS = {"subject": 1.0, "preference": 0.35, "frame": 0.2}

#: The most keywords one memory search uses. Each keyword becomes up to 22
#: whole-word patterns, and every pattern is a bound SQL parameter (three, with
#: its padding), so the query must be bounded independently of how long the
#: message is: uncapped, a 32,000-character message of distinct words built
#: a query of over 43,000 parameters -- above the 32,767 that asyncpg accepts
#: -- and 1,000 distinct words took over ten seconds just to build it. 24
#: bounds any query below 3,400 parameters (adversarial 32,000-character
#: messages measure under 600) and covers any ordinary question several
#: times over. The first keywords are kept, in message
#: order: deterministic, and, as for intent classification, a request's
#: opening is where it says what it wants.
MAX_QUERY_KEYWORDS = 24


def word_forms(keyword: str) -> frozenset:
    """The word forms a keyword matches, as whole words.

    Matching is by whole word, never substring, so "tea" no longer matches
    "team", "steady" or "instead". To keep reasonable inflections, a keyword
    also matches its plural, past and -ing forms and, for an inflected
    keyword, its base ("liked" matches "like", "explaining" matches
    "explain"). Over-generated forms are harmless: only words actually in the
    memory can match.
    """
    forms = set(keyword_variants(keyword))
    if len(keyword) > 5 and keyword.endswith("ing"):
        forms.update({keyword[:-3], keyword[:-3] + "e"})
    elif len(keyword) > 4 and keyword.endswith("ed"):
        forms.update({keyword[:-2], keyword[:-1]})
    for base in list(forms):
        forms.update({base + "s", base + "es"})
        if base.endswith("e"):
            forms.update({base + "d", base[:-1] + "ing"})
        else:
            forms.update({base + "ed", base + "ing"})
    return frozenset(form for form in forms if len(form) >= MIN_KEYWORD_LENGTH)


def term_kind(keyword: str) -> str:
    """"subject", "preference" or "frame" -- see `TERM_WEIGHTS`."""
    if keyword in PREFERENCE_TERMS:
        return "preference"
    if keyword in FRAME_TERMS:
        return "frame"
    return "subject"


def term_weight(keyword: str) -> float:
    return TERM_WEIGHTS[term_kind(keyword)]


def concept_group(keyword: str) -> frozenset:
    """The concept group a keyword belongs to, or an empty set."""
    for group in CONCEPT_GROUPS:
        if keyword in group:
            return group
    return frozenset()


def keyword_matches(keyword: str, memory_words: Set[str]) -> bool:
    """Whether one query keyword matches a memory, given its set of words.

    Exact word forms always match. A preference word matches any preference
    word. A different member of the keyword's concept group matches only when
    the memory also expresses a preference.
    """
    if word_forms(keyword) & memory_words:
        return True
    kind = term_kind(keyword)
    if kind == "preference":
        return bool(PREFERENCE_TERMS & memory_words)
    group = concept_group(keyword)
    if group and group & memory_words and PREFERENCE_TERMS & memory_words:
        return True
    return False


def candidate_forms(keywords: Sequence[str]) -> List[str]:
    """The word forms that may make a memory a keyword candidate.

    Subject keywords only, whenever the question has one: a memory that shares
    nothing but "prefer" with "which tea do I prefer?" is not a candidate. A
    question with no subject ("what do I like?") falls back to its other
    keywords. Concept-group members are included; `keyword_matches` decides
    whether they count.
    """
    subjects = [k for k in keywords if term_kind(k) == "subject"]
    chosen = subjects or list(keywords)
    forms: Set[str] = set()
    for keyword in chosen:
        forms |= word_forms(keyword)
        forms |= concept_group(keyword)
        if term_kind(keyword) == "preference":
            forms |= PREFERENCE_TERMS
    return sorted(forms)


def query_keywords(keywords: Sequence[str]) -> Tuple[str, ...]:
    """The keywords a memory search uses: the first `MAX_QUERY_KEYWORDS`
    distinct ones, in message order. The same input always gives the same
    result, and a question with no more keywords than that is unchanged."""
    return tuple(dict.fromkeys(keywords))[:MAX_QUERY_KEYWORDS]


def keyword_form_groups(keywords: Sequence[str], forms: Sequence[str]) -> List[List[str]]:
    """For each keyword, the candidate `forms` it accounts for, in the order
    of `forms`.

    Each keyword's own forms are computed once and intersected with the
    candidate set. Scanning every candidate form for every keyword instead,
    as this first did, recomputed `word_forms` once per pair: quadratic in a
    message's distinct words.
    """
    order = {form: index for index, form in enumerate(forms)}
    candidates = frozenset(order)
    return [
        sorted((word_forms(keyword) | concept_group(keyword)) & candidates, key=order.__getitem__)
        for keyword in keywords
    ]


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
