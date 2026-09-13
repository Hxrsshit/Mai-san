"""Reading past a typo, without reading anything into it.

Mai's recognisers are deterministic grammars over exact words. That is what
makes them auditable -- and it means "callender" is not a calendar, so a
perfectly ordinary question falls through to an ordinary turn.

This module repairs the input *before* recognition and nothing else. It is an
interpretation aid, and the distinction from an authority mechanism is the
whole of its design:

    it can only ever produce a word Mai already recognises
    it can never produce a word Mai does not

Both halves matter. The target vocabulary is a closed set of terms the
existing grammars already use, so a correction cannot invent a capability --
the most a typo can become is a word that was already going to be routed
somewhere. And the vocabulary is small and hand-written, so it cannot quietly
rewrite ordinary prose.

What this is not
----------------

**Not a spellchecker.** It knows about twenty words, all of them names of
things Mai can actually do. "recieve", "seperate" and "definately" pass
through untouched, because correcting them would change a sentence without
changing what Mai does with it -- all cost, no benefit, and a larger surface.

**Not an authority.** Normalising "callender" to "calendar" does not grant
calendar access, create an approval, or change a policy. It changes which
grammar matches, and every gate downstream is asked exactly as before.

**Not applied to anything but the user's own message.** Calendar events, web
results, retrieved memories and file contents never pass through here. A
normaliser that touched untrusted content would be a way to massage a hostile
string until it matched a request grammar -- turning content into intent,
which is the one thing the whole system is built to prevent.

Bounds
------

Every one is a constant, and none is derived from the input:

    MAX_INPUT_CHARS      the message is not examined beyond this
    MAX_TOKENS           tokens beyond this are passed through unchanged
    MIN_TOKEN_CHARS      shorter tokens are never corrected
    MAX_TOKEN_CHARS      longer tokens are never corrected
    MAX_EDIT_DISTANCE    the furthest a correction may reach
    MAX_CORRECTIONS      corrections per message

The vocabulary is fixed at import. Cost is therefore
`O(tokens x vocabulary x word length)` with every factor a constant, which is
why there is no pathological input -- and a test asserts it against one.
"""

import re
from typing import Dict, FrozenSet, NamedTuple, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Longest message this module will examine. Beyond it the text is returned
#: unchanged rather than truncated: a half-normalised message is worse than an
#: unnormalised one, because the recognisers would see a sentence the user did
#: not write.
MAX_INPUT_CHARS = 2000

#: Most tokens examined. A message at the character limit cannot exceed this.
MAX_TOKENS = 400

#: Tokens outside this range are never corrected.
#:
#: The floor is the important one. At four characters almost everything is one
#: edit from something -- "call" reaches "calls", "mail" reaches "email" -- and
#: correcting short words rewrites ordinary sentences.
MIN_TOKEN_CHARS = 5
MAX_TOKEN_CHARS = 24

#: The furthest a correction may reach. Two edits covers the ordinary human
#: typo -- a doubled letter, a transposition, a dropped vowel -- and stops
#: well short of turning one word into a different one.
MAX_EDIT_DISTANCE = 2

#: Most corrections in one message. A message needing more than this is not a
#: message with typos in it.
MAX_CORRECTIONS = 8


# --- The vocabulary ---------------------------------------------------------

#: The words a correction may produce. **A closed set.**
#:
#: Every entry is a term one of Mai's existing grammars already matches on, so
#: normalisation can only ever move a token onto a word that was already going
#: to be recognised. It cannot produce a word outside this set, which is what
#: makes "normalisation cannot create a capability" a structural property
#: rather than a promise.
#:
#: Deliberately absent: verbs. "serach" is not corrected to "search", because a
#: verb is what turns a sentence into a request -- and a layer that can repair
#: a broken verb into a working one is a layer that can manufacture an
#: instruction out of noise. Nouns name what the user is talking about; the
#: grammars still require a real verb, spelled correctly, before anything
#: happens.
CANONICAL_TERMS: FrozenSet[str] = frozenset({
    # Calendar and scheduling
    "calendar", "calendars", "schedule", "schedules", "agenda", "diary",
    "meeting", "meetings", "appointment", "appointments", "reminder",
    "reminders", "event", "events", "booking", "bookings",
    # Time
    "tomorrow", "today", "tonight", "yesterday", "morning", "afternoon",
    "evening", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "weekend",
    # Things Mai produces
    "notification", "notifications", "briefing", "briefings", "summary",
    "document", "available",
})

#: Misspellings seen in the wild, mapped explicitly.
#:
#: Checked before edit distance because an exact table is auditable in a way a
#: distance metric is not: every entry here is a decision someone made, and a
#: reader can see the whole of it.
KNOWN_MISSPELLINGS: Dict[str, str] = {
    # calendar -- by some distance the most misspelled word Mai deals with
    "callender": "calendar", "calender": "calendar", "calandar": "calendar",
    "calender's": "calendar", "calanders": "calendars",
    "callenders": "calendars", "calenders": "calendars",
    "calandars": "calendars", "kalendar": "calendar", "calendr": "calendar",
    "calnedar": "calendar", "caledar": "calendar", "calenar": "calendar",
    # schedule
    "schedual": "schedule", "shedule": "schedule", "scedule": "schedule",
    "schdule": "schedule", "scheduel": "schedule",
    # meeting / appointment
    "meetng": "meeting", "meting": "meeting", "meetting": "meeting",
    "appointmnet": "appointment", "appointement": "appointment",
    "apointment": "appointment", "appt": "appointment",
    # reminder / notification
    "remider": "reminder", "reminer": "reminder", "remindr": "reminder",
    "reminderr": "reminder",
    "notifcation": "notification", "notifiction": "notification",
    "notificaton": "notification", "notifcations": "notifications",
    # time words
    "tommorow": "tomorrow", "tommorrow": "tomorrow", "tomorow": "tomorrow",
    "tomorrrow": "tomorrow", "tmrw": "tomorrow", "yesterdy": "yesterday",
    "afernoon": "afternoon", "afternon": "afternoon", "moring": "morning",
    "evning": "evening", "wendsday": "wednesday", "wensday": "wednesday",
    "thurday": "thursday", "thusday": "thursday", "saterday": "saturday",
    "tuesdy": "tuesday",
    # misc
    "breifing": "briefing", "brifing": "briefing", "summry": "summary",
    "documnet": "document", "avaliable": "available",
    "availible": "available", "avialable": "available",
}

#: Words that must never be corrected, however close they sit to a term above.
#:
#: Each is a real English word that edit distance would otherwise swallow. The
#: list exists because the failure is silent: the user writes one thing, the
#: grammar sees another, and nobody finds out until the answer is wrong.
PROTECTED_WORDS: FrozenSet[str] = frozenset({
    "agenda", "agent", "agents", "agency", "agencies",
    "eventual", "eventually", "eventful",
    "meaning", "meanings", "melting", "meeting",
    "schedules", "scheduled", "scheduling", "scheduler",
    "remainder", "remainders", "remind", "reminded", "reminding",
    "moaning", "morning", "mourning", "warning", "warnings",
    "diary", "dairy", "dairies",
    "booking", "booked", "looking", "cooking",
    "summer", "summary", "summaries", "summon",
    "monday", "money", "monkey", "sunday", "sundry", "friday", "fridge",
    "documents", "documented", "documenting",
})

#: The token shape a correction may apply to: letters, with an optional
#: possessive or plural apostrophe. Anything carrying a digit, a slash, an @ or
#: a dot is left alone -- it is an identifier, a URL fragment or a date, and
#: none of those is a misspelled English noun.
_CORRECTABLE = re.compile(r"^[A-Za-z]+(?:['’][A-Za-z]{1,2})?$")

#: Splits text into tokens and the separators between them, so the message can
#: be rebuilt with its punctuation and spacing exactly as written.
_TOKENS = re.compile(r"([A-Za-z][A-Za-z'’]*)")

#: Characters that, immediately before a token, mean it is part of something
#: structured rather than part of a sentence.
_GLUED_BEFORE = frozenset("@/\\._-:=#$0123456789")

#: The same, after a token. A trailing "." or "-" is only structural when a
#: letter or digit follows it -- otherwise it is the end of a sentence, and
#: "check my callender." must still be corrected.
_GLUED_AFTER = frozenset("@/\\_:=#$0123456789")


def _is_glued(before: str, after: str) -> bool:
    """Whether a token sits inside an identifier, address, URL or other word.

    `user@calender.example` and `v1.2.3-calendr` split into bare word tokens
    like any sentence does, so without this the normaliser rewrote the middle
    of an email address. Correcting a *name* is not reading past a typo; it
    changes an identifier into a different identifier.

    The adjacent-letter case is the security-relevant one, and a test found
    it. The tokeniser only captures ASCII letters, so a homoglyph splits a
    word in two: "cаlendar" with a Cyrillic "а" yields the fragment "lendar",
    which sits two insertions from "calendar" and was duly "corrected" --
    producing "cаcalendar", text the user never wrote, assembled out of a
    confusable. A fragment touching a letter is not a word, whatever script
    that letter belongs to.
    """
    if before and (before[-1] in _GLUED_BEFORE or before[-1].isalpha()):
        return True
    if after and after[0].isalpha():
        return True
    if not after:
        return False
    if after[0] in _GLUED_AFTER:
        return True
    if after[0] in ".-" and len(after) > 1 and after[1].isalnum():
        return True
    return False


class Correction(NamedTuple):
    """One replacement. Kept so the audit trail can show the whole change."""

    original: str
    replacement: str
    #: "table" or "distance" -- how the correction was reached.
    source: str


class Normalisation(NamedTuple):
    """The result. Carries the original, always.

    Both strings travel together on purpose. Anything that stores, displays,
    logs or reasons about what the *user* said must use `original`; only the
    recognisers see `text`. Returning a bare string would have made the wrong
    one the easy one to reach for.
    """

    original: str
    text: str
    corrections: Tuple[Correction, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.corrections)


def normalise(message: str) -> Normalisation:
    """Repair known misspellings of terms Mai recognises. Never raises.

    Returns the message unchanged when there is nothing to do, which is the
    overwhelmingly common case.
    """
    if not message:
        return Normalisation(original=message or "", text=message or "")

    if len(message) > MAX_INPUT_CHARS:
        # Not truncated. A half-normalised message is a sentence the user did
        # not write, and the recognisers would be matching against it.
        return Normalisation(original=message, text=message)

    parts = _TOKENS.split(message)
    corrections = []
    examined = 0

    for index, part in enumerate(parts):
        # `re.split` with one capture group alternates separator, token,
        # separator, ... so the odd positions are the tokens.
        if index % 2 == 0:
            continue
        examined += 1
        if examined > MAX_TOKENS or len(corrections) >= MAX_CORRECTIONS:
            break

        if _is_glued(parts[index - 1], parts[index + 1] if index + 1 < len(parts) else ""):
            continue

        replacement, source = _correction_for(part)
        if replacement is None:
            continue

        parts[index] = _match_case(part, replacement)
        corrections.append(Correction(part, parts[index], source))

    if not corrections:
        return Normalisation(original=message, text=message)

    logger.info(
        "Normalised a message before recognition",
        # Counts only. The message itself can name a person, an employer or a
        # diagnosis, and Stage 3D's rule is that such text does not reach INFO.
        extra={"corrections": len(corrections)},
    )
    return Normalisation(
        original=message, text="".join(parts), corrections=tuple(corrections)
    )


def _correction_for(token: str) -> Tuple[Optional[str], str]:
    """The canonical form of this token, or None. Table first, then distance."""
    if not _CORRECTABLE.match(token):
        return None, ""

    lowered = token.lower()

    if lowered in CANONICAL_TERMS:
        return None, ""

    # The table first, and *before* the protection list.
    #
    # The two guard different things. `PROTECTED_WORDS` restrains the distance
    # heuristic, which is a guess; the table is an explicit decision someone
    # made and reviewed. Checking protection first meant "calender" -- which
    # is both a real word for a paper-smoothing machine and, in every message
    # Mai will ever see, a misspelling of "calendar" -- was silently left
    # alone despite having a table entry.
    table = KNOWN_MISSPELLINGS.get(lowered)
    if table is not None:
        return table, "table"

    if lowered in PROTECTED_WORDS:
        # A real word the heuristic must not touch.
        return None, ""

    if not (MIN_TOKEN_CHARS <= len(lowered) <= MAX_TOKEN_CHARS):
        return None, ""

    return _nearest(lowered), "distance"


def _nearest(token: str) -> Optional[str]:
    """The one canonical term within `MAX_EDIT_DISTANCE`, or None.

    *One.* A token equidistant from two terms is ambiguous, and guessing
    between them would silently pick a meaning the user did not write. The
    whole set is scanned rather than stopping at the first hit, because
    "there is exactly one" cannot be established by finding one.
    """
    best: Optional[str] = None
    best_distance = MAX_EDIT_DISTANCE + 1
    tied = False

    for candidate in CANONICAL_TERMS:
        # Length alone rules most of the vocabulary out before any work.
        if abs(len(candidate) - len(token)) > MAX_EDIT_DISTANCE:
            continue
        distance = _bounded_distance(token, candidate, MAX_EDIT_DISTANCE)
        if distance is None:
            continue
        if distance < best_distance:
            best, best_distance, tied = candidate, distance, False
        elif distance == best_distance:
            tied = True

    if tied:
        # A tie usually means the intent is unknowable -- but not when the
        # candidates are inflections of one another. "calendarr" is one edit
        # from both "calendar" and "calendars"; the user meant a calendar
        # either way, and refusing to choose leaves the word unrecognised for
        # no gain. Resolved only when one candidate is a prefix of every
        # other, which is exactly the singular/plural case and nothing else.
        best = _shared_stem(token, best_distance)
        if best is None:
            return None

    if best is None:
        return None

    # Two edits is only permitted for a long token.
    #
    # The earlier rule -- distance must be less than half the length -- let a
    # six-character fragment travel two edits, which is how "lendar" reached
    # "calendar". Below eight characters a single edit is the most that can be
    # called a typo rather than a different word.
    #
    # A second rule stood here, requiring `distance * 3 <= length`. Mutation
    # testing showed deleting it changed nothing, and working through the
    # cases explains why: this line already caps distance at 1 below eight
    # characters and at 2 above, and `3 > 8` is false -- so there is no
    # (length, distance) pair the ratio could ever reject that this has not
    # rejected first. It was removed rather than kept. Unreachable security
    # code advertises a protection that is not operating, which is worse than
    # not having it.
    if len(token) < 8 and best_distance > 1:
        return None

    return best


def _shared_stem(token: str, distance: int) -> Optional[str]:
    """The shortest of the tied candidates, when they are all inflections.

    Returns None unless every candidate at this distance starts with the
    shortest one -- so "calendar"/"calendars" resolves and two unrelated words
    at the same distance still do not.
    """
    tied = [
        candidate for candidate in CANONICAL_TERMS
        if abs(len(candidate) - len(token)) <= distance
        and _bounded_distance(token, candidate, distance) == distance
    ]
    if not tied:
        return None
    shortest = min(tied, key=len)
    if all(candidate.startswith(shortest) for candidate in tied):
        return shortest
    return None


def _bounded_distance(left: str, right: str, limit: int) -> Optional[int]:
    """Damerau-Levenshtein distance, or None once it exceeds `limit`.

    Transpositions count as one edit because "claendar" is one slip of the
    fingers, not two. The row is banded to `limit` either side of the
    diagonal, so the cost is `O(len x limit)` rather than `O(len^2)` -- with
    both factors bounded by constants in this module, there is no input that
    makes this slow.
    """
    if abs(len(left) - len(right)) > limit:
        return None

    previous_previous: list = []
    previous = list(range(len(right) + 1))

    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        # Only the band within `limit` of the diagonal can matter.
        low = max(1, i - limit)
        high = min(len(right), i + limit)
        for j in range(1, len(right) + 1):
            if j < low or j > high:
                current[j] = limit + 1
                continue
            cost = 0 if left_char == right[j - 1] else 1
            current[j] = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            )
            if (
                i > 1 and j > 1
                and left_char == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                current[j] = min(current[j], previous_previous[j - 2] + cost)

        if min(current[low:high + 1] or [limit + 1]) > limit:
            # Every cell in the band already exceeds the limit, and distance
            # is non-decreasing down the rows, so no later row can recover.
            return None

        previous_previous, previous = previous, current

    distance = previous[len(right)]
    return distance if distance <= limit else None


def _match_case(original: str, replacement: str) -> str:
    """Keep the shape the user typed. "Callender" -> "Calendar"."""
    if original.isupper() and len(original) > 1:
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def vocabulary() -> Tuple[str, ...]:
    """Everything a correction may produce. For tests and for auditing."""
    return tuple(sorted(CANONICAL_TERMS))


__all__ = [
    "CANONICAL_TERMS",
    "KNOWN_MISSPELLINGS",
    "MAX_CORRECTIONS",
    "MAX_EDIT_DISTANCE",
    "MAX_INPUT_CHARS",
    "MAX_TOKENS",
    "PROTECTED_WORDS",
    "Correction",
    "Normalisation",
    "normalise",
    "vocabulary",
]
