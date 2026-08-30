"""Deterministic conflict policies.

Everything here is data and pure functions. No database, no model call, no
clock. The rules are stated once, in one place, so "why was this considered a
conflict?" has a single answer a developer can read.

The governing bias is stated in the specification and worth repeating: a false
conflict is worse than a missed one. A missed conflict leaves two memories
where one is stale; a false conflict hides true knowledge behind a
"historical" label. Every rule below is therefore written to abstain when the
evidence is not clear.

The central difficulty
----------------------

Relationship type alone cannot decide a conflict. Both of these are the same
shape -- one source, one relationship type, two targets:

    Mai USES OpenRouter   ->  Mai USES Groq      # a replacement
    Mai USES PostgreSQL   +   Mai USES Groq      # both true at once

The first supersedes, the second does not, and no amount of schema inspection
separates them. What separates them is *language in the new memory*: someone
who replaced a provider says so ("switched from OpenRouter to Groq"), while
someone adding a second tool does not.

So supersession is driven by explicit replacement language, not by co-existence
of two targets. Co-existence alone is treated as a conflict only for the small
set of relationship types where two simultaneous targets are genuinely
incoherent -- and even there the outcome is "unresolved", not a winner.
"""

import re
from typing import FrozenSet, List, NamedTuple, Optional

from app.relationships.models import RelationshipType

# --- Relationship policies --------------------------------------------------

#: Relationship types where one active target is the normal case, so a second
#: one is worth flagging.
#:
#: Deliberately tiny. Every type not listed here is treated as non-exclusive,
#: which is the safe default. Two entries only:
#:
#: - PREFERS: a stated preference between alternatives replaces the previous
#:   one ("prefers remote" -> "now prefers hybrid").
#: - LOCATED_IN: a person is in one place ("lives in Hyderabad" vs
#:   "lives in Bangalore").
#:
#: USES is **not** here, and that is the whole point of the module docstring:
#: `Mai USES PostgreSQL` and `Mai USES Groq` are simultaneously true.
EXCLUSIVE_RELATIONSHIP_TYPES: FrozenSet[RelationshipType] = frozenset({
    RelationshipType.PREFERS,
    RelationshipType.LOCATED_IN,
})

#: Everything else. Listed explicitly rather than derived, so adding a new
#: relationship type forces a deliberate decision instead of silently
#: defaulting.
NON_EXCLUSIVE_RELATIONSHIP_TYPES: FrozenSet[RelationshipType] = frozenset(
    set(RelationshipType) - EXCLUSIVE_RELATIONSHIP_TYPES
)


def is_exclusive(relationship_type: RelationshipType) -> bool:
    """True when a second active target for the same source is worth flagging."""
    return relationship_type in EXCLUSIVE_RELATIONSHIP_TYPES


# --- Replacement language ---------------------------------------------------


class Replacement(NamedTuple):
    """One "X was replaced by Y" claim found in a memory's text.

    Both names are normalised fragments as they appeared; resolving them to
    entities is the caller's job, because that needs the database.
    """

    old: str
    new: str
    pattern: str


#: Patterns that name both sides of a replacement.
#:
#: Each must capture `old` and `new`. They are matched against normalised text
#: (lowercased, punctuation stripped), so no punctuation appears here. The
#: fragments are bounded with a lazy quantifier and a length cap: an unbounded
#: capture would swallow the rest of the sentence and resolve to nothing.
_FRAGMENT = r"(.{2,60}?)"

_REPLACEMENT_PATTERNS: List[str] = [
    # "switched from OpenRouter to Groq", "migrated Mai from X to Y"
    rf"\b(?:switched|migrated|moved|changed|converted)\b.{{0,30}}?"
    rf"\bfrom\b\s+{_FRAGMENT}\s+\bto\b\s+{_FRAGMENT}(?:\s+for\b|\s+in\b|$)",
    # "replaced OpenRouter with Groq"
    rf"\breplaced\b\s+{_FRAGMENT}\s+\bwith\b\s+{_FRAGMENT}(?:\s+for\b|\s+in\b|$)",
    # "uses Groq instead of OpenRouter"  (order reversed: new, then old).
    # Anchored on the verb: an unanchored capture swallows the subject too,
    # turning "mai uses groq instead of openrouter" into new="mai uses groq".
    rf"\b(?:uses|using|use|prefers|prefer|chose|chosen|picked|selected|runs|"
    rf"running)\s+{_FRAGMENT}\s+\binstead of\b\s+{_FRAGMENT}"
    rf"(?:\s+for\b|\s+in\b|$)",
]

#: Which capture group holds the *old* side, per pattern index above.
_OLD_GROUP = (1, 1, 2)
_NEW_GROUP = (2, 2, 1)

#: Patterns naming only the thing dropped: "no longer uses OpenRouter".
_ABANDONMENT_PATTERNS: List[str] = [
    rf"\bno longer\b\s+(?:uses|using|use|prefers|prefer|needs|need)\s+{_FRAGMENT}"
    rf"(?:\s+for\b|\s+in\b|$)",
    rf"\bstopped\b\s+(?:using|preferring)\s+{_FRAGMENT}(?:\s+for\b|\s+in\b|$)",
]

_COMPILED_REPLACEMENTS = [re.compile(pattern) for pattern in _REPLACEMENT_PATTERNS]
_COMPILED_ABANDONMENTS = [re.compile(pattern) for pattern in _ABANDONMENT_PATTERNS]

#: Words marking a statement as describing the present, used only to resolve a
#: conflict between *exclusive* relationship types. On a non-exclusive type
#: they mean nothing: "I now use Redis" does not retire PostgreSQL.
CURRENCY_MARKERS: FrozenSet[str] = frozenset({
    "now", "currently", "these days", "nowadays", "at the moment",
})


def find_replacements(normalized_text: str) -> List[Replacement]:
    """Every explicit replacement claim in one memory's normalised text.

    Returns an empty list far more often than not, which is intended. Only
    text that names *both* sides of a change produces a result.
    """
    if not normalized_text:
        return []

    found: List[Replacement] = []
    seen = set()

    for index, pattern in enumerate(_COMPILED_REPLACEMENTS):
        for match in pattern.finditer(normalized_text):
            old = match.group(_OLD_GROUP[index]).strip()
            new = match.group(_NEW_GROUP[index]).strip()
            if not old or not new or old == new:
                continue
            key = (old, new)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Replacement(old=old, new=new, pattern=pattern.pattern[:40])
            )
    return found


def find_abandonments(normalized_text: str) -> List[str]:
    """Things the text says are no longer used, without naming a successor."""
    if not normalized_text:
        return []

    found: List[str] = []
    seen = set()
    for pattern in _COMPILED_ABANDONMENTS:
        for match in pattern.finditer(normalized_text):
            fragment = match.group(1).strip()
            if fragment and fragment not in seen:
                seen.add(fragment)
                found.append(fragment)
    return found


def states_the_present(normalized_text: str) -> bool:
    """True when the text explicitly frames itself as the current state.

    Used only for exclusive relationship types, where "now prefers hybrid"
    is enough to retire "prefers remote". It is never enough on its own for a
    non-exclusive type.
    """
    if not normalized_text:
        return False
    return any(marker in normalized_text for marker in CURRENCY_MARKERS)


def candidate_names(fragment: str) -> List[str]:
    """Progressively shorter suffixes of a captured fragment.

    A capture like "openrouter for inference" should still resolve to the
    entity "openrouter". Rather than guessing where the name ends, every
    prefix is offered to the resolver longest-first and the first hit wins.
    """
    words = fragment.split()
    if not words:
        return []

    names: List[str] = []
    for size in range(min(len(words), 4), 0, -1):
        names.append(" ".join(words[:size]))
    return names


def looks_like_temporal_progress(normalized_text: str) -> bool:
    """True when the text reports progress on something, not a replacement.

    "User completed Project A" follows "User is working on Project A" without
    contradicting it -- the second describes a later point in the same story.
    Used to abstain rather than to conclude: this never *creates* a conflict.
    """
    if not normalized_text:
        return False
    markers = (
        "completed", "finished", "shipped", "launched", "delivered",
        "abandoned", "paused", "resumed", "started", "began",
    )
    return any(f" {marker} " in f" {normalized_text} " for marker in markers)


__all__ = [
    "CURRENCY_MARKERS",
    "EXCLUSIVE_RELATIONSHIP_TYPES",
    "NON_EXCLUSIVE_RELATIONSHIP_TYPES",
    "Replacement",
    "candidate_names",
    "find_abandonments",
    "find_replacements",
    "is_exclusive",
    "looks_like_temporal_progress",
    "states_the_present",
]
