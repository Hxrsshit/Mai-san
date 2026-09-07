"""Did the user confirm? Decided by the application, never by the model.

This is the smallest module in the stage and the one most worth reading
carefully, because it is where a chat message becomes an approval.

If a model decided whether "yes" meant yes, then model output would grant
approval -- and every stage from 4C onward exists to prevent exactly that. So
confirmation is a deterministic lookup against an application-authored phrase
table, in the same spirit as Stage 4D's action matcher: no model call, no
fuzzy matching, no interpretation.

Three outcomes, and the third is the important one:

    CONFIRMED   an exact affirmative
    DECLINED    an exact negative
    UNRELATED   anything else -- which *discards* the pending proposal

`UNRELATED` discarding rather than preserving is deliberate. A proposal that
survived unrelated turns could be confirmed by a "yes" three messages later
that meant something else entirely. A pending search is confirmable by the
immediately following turn or not at all.
"""

import enum
import re
from typing import FrozenSet

class Confirmation(str, enum.Enum):
    """What the user's reply did to a pending proposal."""

    CONFIRMED = "confirmed"
    DECLINED = "declined"
    #: Neither. The proposal is discarded and the turn proceeds normally.
    UNRELATED = "unrelated"


#: Exact affirmatives. Whole-message matches only.
#:
#: Every entry is unambiguous on its own. Nothing here can appear inside an
#: ordinary sentence and be mistaken for consent, because a match requires the
#: *entire* message -- "yes" confirms, "yes but what about..." does not.
_AFFIRMATIVE: FrozenSet[str] = frozenset({
    "y", "yes", "yes please", "yes go ahead", "yep", "yeah", "yup",
    "ok", "okay", "sure", "please do", "do it", "go ahead", "go for it",
    "confirm", "confirmed", "approve", "approved", "search", "search it",
    "do the search", "run the search", "please search", "sounds good",
})

#: Exact negatives. Same rule.
_NEGATIVE: FrozenSet[str] = frozenset({
    "n", "no", "no thanks", "no thank you", "nope", "nah",
    "cancel", "cancelled", "stop", "don't", "dont", "do not",
    "never mind", "nevermind", "forget it", "skip it", "leave it",
    "decline", "declined", "not now",
})

#: Trailing punctuation carries no meaning here and would otherwise make
#: "yes!" a different string from "yes".
_TRAILING = re.compile(r"[\s.!?,;:]+$")


def interpret(message: str) -> Confirmation:
    """Classify one message against a pending proposal. Deterministic.

    Normalisation is deliberately minimal: lowercase, collapse whitespace,
    drop trailing punctuation. Nothing that could change which word was
    meant, and certainly no stemming -- "unconfirm" must not reduce to
    "confirm".
    """
    if not isinstance(message, str):
        return Confirmation.UNRELATED

    cleaned = " ".join(message.split()).lower()
    cleaned = _TRAILING.sub("", cleaned)

    if not cleaned:
        return Confirmation.UNRELATED

    # Whole-message equality, which is the entire guard.
    #
    # An earlier draft also carried a length bound. Mutation testing showed it
    # was dead: exact matching against a table whose longest entry is a dozen
    # characters already rejects everything a length bound would, so it was a
    # protection that could never fire. Removed rather than kept -- a guard
    # that cannot be triggered is worse than none, because it reads as though
    # something is being enforced.
    #
    # "yes, and also delete my files" is refused here, by equality.
    if cleaned in _AFFIRMATIVE:
        return Confirmation.CONFIRMED
    if cleaned in _NEGATIVE:
        return Confirmation.DECLINED
    return Confirmation.UNRELATED


def affirmative_phrases() -> FrozenSet[str]:
    """The table, for tests. A copy in name only -- the set is frozen."""
    return _AFFIRMATIVE


def negative_phrases() -> FrozenSet[str]:
    return _NEGATIVE


__all__ = [
    "Confirmation",
    "affirmative_phrases",
    "interpret",
    "negative_phrases",
]
