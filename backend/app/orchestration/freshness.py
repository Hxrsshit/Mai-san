"""Deciding whether a question's answer depends on information that may have changed.

Mai's recognisers, up to Stage 5B, all answer the same kind of question: *did
the user ask me to do something?* "Search the web for X", "what's on my
calendar", "check my email". Each is a request for an action, and each has a
verb that says so.

This module answers a different question, and the difference is the whole
point of the stage: *does answering this correctly require information that
may have changed since the model was trained?*

    "what is the latest OpenAI model?"

asks Mai to search for nothing. It contains no search verb, matches no
research family, and so fell through to an ordinary turn where the model
answered confidently from training data that was already months stale. The
user was not told the answer might be old, because nothing in the system knew
it might be.

Domain-agnostic, by construction
--------------------------------

There is no list of companies, products, or topics in this file, and there
must never be one. A list would be wrong the day after it was written, would
grow forever, and would silently fail for every entity nobody thought of.

What is detected instead is **temporal structure**: words that bind an answer
to a moment ("latest", "current", "today", "right now"), and question shapes
whose subject is a value that moves ("how much is X", "is X open", "who won").
Those are properties of English, not of any industry, so the same rules that
catch "the latest React version" catch "the latest tax rules" and "what's
happening in Bangalore this weekend" without knowing what React, tax or
Bangalore are.

It grants nothing
-----------------

A `FreshnessAssessment` is an opinion, typed and small. It cannot authorize a
tool, create an approval, reach the network, or choose a destination. What it
can do is tell the chat layer that a question deserves current information --
and the chat layer then goes through exactly the consent, authorization and
execution gates that were already there. The model is not consulted, and a
model saying "I should search for this" is not an input to any of it.

It never sees external content
------------------------------

Assessment runs on the **user's own message** and nothing else. Not web
results, not email bodies, not calendar titles, not file contents, not
retrieved memories. An email reading "search the web for the latest password
dump" is data; letting it reach this module would be how external content
acquires intent, which is the direction the whole system exists to prevent.
A structural test asserts the single call site.

Cost
----

Regular expressions over a bounded string. No model call, no network call, no
database call -- determining whether someone said "latest" must not cost a
round trip.
"""

import enum
import re
from typing import List, NamedTuple, Optional, Tuple

from app.core.logging import get_logger
#: Shared with `app.orchestration.resolution` rather than duplicated: "does
#: this word name anything?" has one answer in this system. `research.language`
#: imports it for the same reason.
from app.orchestration.resolution import is_substantive

logger = get_logger(__name__)

#: Longest message assessed. Beyond it the answer is NOT_REQUIRED: a message
#: this long is a document, not a question, and scanning it would be work
#: without a decision at the end.
MAX_MESSAGE_CHARS = 1000

#: Longest subject carried into a query. Below the search layer's own bound,
#: so the query is shortened here rather than truncated there.
MAX_SUBJECT_CHARS = 240


class FreshnessRequirement(str, enum.Enum):
    """How much the answer depends on information that may have changed.

    Three states rather than two, because the middle one is a real and
    different thing to do. `REQUIRED` changes what Mai *does* -- it goes and
    looks. `PREFERRED` changes only what Mai *says*: it answers from what it
    knows and adds that the answer may have aged. Collapsing them would force
    a choice between searching for things nobody needed searched and
    presenting a possibly-stale fact as current.
    """

    #: Stable knowledge. "What is photosynthesis?"
    NOT_REQUIRED = "not_required"
    #: Answerable honestly from training knowledge, but it may have aged.
    PREFERRED = "preferred"
    #: The answer materially depends on current information.
    REQUIRED = "required"


class FreshnessSource(str, enum.Enum):
    """Which class of source would satisfy the requirement, where known.

    An abstraction with one implemented member. `WEB` is the general source
    and is the only one this stage routes to; the personal members exist
    because a question about the user's own schedule or mail is *also* a
    currentness question, and naming that keeps the freshness layer from
    quietly claiming it. Future specialised sources -- weather, market data --
    would join this enum rather than being special-cased at a call site.
    """

    #: No external source needed.
    NONE = "none"
    #: General web research, through the existing Tavily path.
    WEB = "web"
    #: The user's own calendar. Claimed earlier in the chain, never here.
    CALENDAR = "calendar"
    #: The user's own mail. Claimed earlier in the chain, never here.
    MAIL = "mail"


class FreshnessAssessment(NamedTuple):
    """What this module concluded. Application state, never model output."""

    requirement: FreshnessRequirement = FreshnessRequirement.NOT_REQUIRED
    #: A short application reason code, for the reply and the audit trail.
    reason: str = ""
    source: FreshnessSource = FreshnessSource.NONE
    #: The user's question, reduced to a faithful search subject. Empty when
    #: no external lookup is called for.
    subject: str = ""

    @property
    def needs_current_information(self) -> bool:
        return self.requirement is FreshnessRequirement.REQUIRED

    @property
    def wants_web(self) -> bool:
        """Whether *this layer* would route to general web research.

        Deliberately not "may search". Nothing here decides that; the chat
        layer asks, and the existing consent and authorization gates answer.
        """
        return (
            self.requirement is FreshnessRequirement.REQUIRED
            and self.source is FreshnessSource.WEB
            and bool(self.subject)
        )


# --- Signals -----------------------------------------------------------------

#: Words that bind an answer to a moment.
#:
#: Every one is a property of English rather than of a subject area, which is
#: what makes the detector domain-agnostic. "The latest X" needs current
#: information whatever X is.
_RECENCY_MARKER = re.compile(
    r"\b(?:"
    r"latest|newest|most\s+recent|up[\s-]?to[\s-]?date|"
    r"current(?:ly)?|right\s+now|as\s+of\s+(?:now|today)|"
    r"at\s+the\s+moment|these\s+days|nowadays|"
    r"today|tonight|this\s+(?:week|month|morning|afternoon|evening|weekend|year)|"
    r"recent(?:ly)?|lately|so\s+far\s+this\s+(?:week|month|year)|"
    r"just\s+(?:announced|released|launched|published|happened|came\s+out)|"
    r"last\s+night|past\s+(?:week|month|few\s+days)|"
    r"still\s+(?:the|a|an)\b"
    r")\b",
    re.IGNORECASE,
)

#: Question shapes whose subject is a value that moves.
#:
#: Structural rather than lexical: what is recognised is "a question about a
#: price", not "a question about gold". The predicate is the signal.
_VOLATILE_PREDICATE = re.compile(
    r"\b(?:"
    # Price, cost and market value.
    r"how\s+much\s+(?:is|are|does|do|did)\b|"
    r"(?:the\s+)?price\s+of\b|\bprice\s+(?:is|for)\b|"
    r"trading\s+at\b|stock\s+price\b|share\s+price\b|exchange\s+rate\b|"
    r"what\s+(?:is|are)\s+\w+\s+worth\b|"
    # Weather.
    r"weather\b|forecast\b|temperature\s+(?:in|at|outside)\b|"
    # Availability and opening.
    r"(?:is|are)\s+\w+\s+open\b|what(?:'|’)?s\s+open\b|open\s+(?:now|tonight|today)\b|"
    r"(?:is|are)\s+\w+\s+(?:down|up|working|online|offline)\b|"
    # Employment.
    r"(?:is|are)\s+\w+\s+hiring\b|job\s+openings?\b|vacanc(?:y|ies)\b|"
    # Outcomes and scores.
    r"who\s+won\b|what\s+was\s+the\s+score\b|final\s+score\b|"
    # Happenings.
    r"what(?:'|’)?s\s+happening\b|what\s+happened\b|what(?:'|’)?s\s+going\s+on\b|"
    r"any\s+news\b|what(?:'|’)?s\s+new\b|what\s+changed\b"
    r")",
    re.IGNORECASE,
)

#: Roles and attributes whose holder changes, asked without a marker.
#:
#: "Who is the CEO of Nvidia?" is answerable from stable knowledge and often
#: right -- but it is the kind of fact that turns over, and saying so is more
#: honest than either searching unbidden or answering as though it were fixed.
#: This is the only thing that produces `PREFERRED`.
_VOLATILE_ROLE = re.compile(
    r"\b(?:"
    r"who\s+(?:is|are)\s+(?:the\s+)?(?:ceo|cto|cfo|president|prime\s+minister|"
    r"chairman|chairwoman|chair|head|leader|manager|coach|captain|owner|"
    r"director)\b|"
    r"what\s+version\s+(?:of|is)\b|which\s+version\b|"
    r"how\s+many\s+(?:employees|users|subscribers|customers)\b"
    r")",
    re.IGNORECASE,
)

#: Questions about settled concepts. Only consulted when no marker is present.
#:
#: "What is the latest Python version?" has the same opening as "What is
#: Python?" and a completely different answer lifetime, so a definitional
#: shape cannot by itself mean "stable" -- the absence of a recency marker is
#: what means that.
_DEFINITIONAL = re.compile(
    r"\b(?:"
    r"what\s+(?:is|are)\s+(?:a|an|the)?\s*\w+\s*\??$|"
    r"what\s+(?:is|does)\s+\w+\s+(?:mean|stand\s+for)\b|"
    r"how\s+(?:does|do|did)\s+.{0,40}?\s*work\b|"
    r"explain\b|describe\s+how\b|what(?:'|’)?s\s+the\s+difference\s+between\b|"
    r"why\s+(?:is|are|does|do)\b|"
    r"teach\s+me\b|help\s+me\s+understand\b"
    r")",
    re.IGNORECASE,
)

#: Work Mai is being asked to *do*, not look up.
_TASK_REQUEST = re.compile(
    r"\b(?:"
    r"write\s+(?:me\s+)?(?:a|an|some)?\s*(?:python|javascript|typescript|java|"
    r"go|rust|sql|bash|shell|c\+\+|code|function|script|class|test|query)\b|"
    r"help\s+me\s+(?:draft|write|compose|fix|debug|refactor)\b|"
    r"(?:draft|compose)\s+(?:me\s+)?(?:a|an)\s+\w+|"
    r"refactor\b|debug\b|fix\s+this\b|review\s+this\s+code\b|"
    r"translate\s+(?:this|the\s+following)\b|summari[sz]e\s+this\b"
    r")",
    re.IGNORECASE,
)

#: The user's own data, whatever the temporal wording.
#:
#: "What did I tell you about Mai?" and "what do you know about my project?"
#: are currentness questions about material Mai already holds. They must not
#: become web searches, and the personal recognisers that own calendar and
#: mail have already declined by the time this runs -- so this catches the
#: remainder.
#: Adjectives people put between the possessive and the thing owned.
#:
#: "my **latest** emails" is as personal as "my emails", and without this the
#: recency marker dragged it onto the web -- the same shape Stage 5A found in
#: "my google calendar". A closed list, so an arbitrary word cannot make an
#: unrelated phrase look personal.
_OWNED_QUALIFIER = (
    r"(?:most\s+recent|latest|newest|recent|last|next|upcoming|unread|new|"
    r"old|work|personal|shared|google|gmail|outlook|main|primary)"
)

_PERSONAL_SCOPE = re.compile(
    r"\b(?:"
    rf"(?:my|our)\s+(?:{_OWNED_QUALIFIER}\s+){{0,2}}"
    r"(?:calendar|schedule|diary|agenda|meetings?|appointments?)\b|"
    rf"(?:my|our)\s+(?:{_OWNED_QUALIFIER}\s+){{0,2}}"
    r"(?:e-?mails?|inbox|gmail|messages)\b|"
    r"what\s+did\s+i\s+(?:tell|say|ask|mention)\b|"
    r"what\s+do\s+you\s+(?:know|remember)\s+about\s+(?:my|our|me)\b|"
    rf"(?:my|our)\s+(?:{_OWNED_QUALIFIER}\s+){{0,2}}"
    r"(?:project|notes|files|workspace|documents?|memory|memories)\b|"
    r"do\s+you\s+remember\b"
    r")",
    re.IGNORECASE,
)

#: Questions whose subject is a participant in the conversation.
#:
#: "How are you today?", "which LLM provider am I currently using?", "what
#: technology stack am I currently using for Mai?" all carry a recency marker
#: and none of them is about the world. The first is a pleasantry; the other
#: two are answered from runtime capability facts and retrieved context, which
#: Mai already holds.
#:
#: The existing suite caught all three, which is the argument for the guard
#: being about *subject* rather than about those sentences: a marker tells you
#: the answer has a time attached, and this tells you whose answer it is.
#:
#: Deliberately narrow. "Can you tell me the latest iPhone?" addresses Mai and
#: asks about the world, so it must stay a web question -- which is why this
#: requires an interrogative *followed by* "am I" or "are you", rather than
#: guarding the pronouns wherever they appear.
_PARTICIPANT_SUBJECT = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay|so|and|please)\s*,?\s*)*"
    r"(?:"
    r"how(?:'|’)?s?\s+(?:are\s+)?(?:you|it\s+going|things)\b|"
    r"good\s+(?:morning|afternoon|evening|day)\b|"
    r"(?:what|which|who|how)\b[^?]{0,60}?\b(?:am\s+i|are\s+you|do\s+you\s+have)\b"
    r")",
    re.IGNORECASE,
)

#: Which personal source, when the scope guard fires. For classification only.
_CALENDAR_SCOPE = re.compile(
    r"\b(?:calendar|schedule|diary|agenda|meetings?|appointments?)\b", re.IGNORECASE
)
_MAIL_SCOPE = re.compile(r"\b(?:e-?mails?|inbox|gmail|messages)\b", re.IGNORECASE)

#: Leading interrogative frame, stripped so the subject survives intact.
#:
#: "what is the latest OpenAI model?" -> "latest OpenAI model". The marker is
#: deliberately kept: "latest iPhone" is the query a person would type, and
#: dropping the word would ask the web about the iPhone in general.
_FRAME = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay|so|and|please)\s*,?\s*)*"
    r"(?:can|could|would|will|do|does|did)\s+you\s+(?:tell\s+me\s+)?|"
    r"^\s*(?:(?:hey|hi|hello|ok|okay|so|and|please)\s*,?\s*)*"
    r"(?:what|which|who|when|where|how\s+much|how\s+many|how)\s+"
    r"(?:is|are|was|were|do|does|did|has|have|had)?\s*(?:the|a|an)?\s*",
    re.IGNORECASE,
)

#: Trailing politeness and question marks left after framing is removed.
_TRAILING = re.compile(
    r"(?:\s*,?\s*(?:please|thanks|thank\s+you|for\s+me))?\s*[?!.]*\s*$",
    re.IGNORECASE,
)


#: A word, for clause merging. Trailing punctuation is not part of it.
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")

#: The same interrogative frame as `_FRAME`, unanchored, to find a *second*
#: question inside one message. Stage 5E.1.
#:
#: Shares `_FRAME`'s vocabulary deliberately: "a thing that opens a question"
#: has one definition here, and two copies would drift. The leading
#: separator is part of the match so the clause boundary is consumed rather
#: than left dangling on the front of the next clause.
_INNER_FRAME = re.compile(
    r"(?:\s*[,;]\s*|\s+and\s+|\s*\?\s*)"
    r"(?=(?:can|could|would|will|do|does|did)\s+you\b"
    r"|(?:what|which|who|when|where|how)\b)",
    re.IGNORECASE,
)

#: Most clauses read from one message. A question with more parts than this is
#: not a research request anyone expects one query to answer, and an unbounded
#: split is an unbounded query.
MAX_CLAUSES = 4


def _clauses(text: str) -> List[str]:
    """Split a compound question into its parts, longest-first-clause first.

    A message carrying a *second* interrogative frame is two questions:

        "what is the latest model by Claude, what is the latest model of opus"

    `_subject_of` strips one leading frame by design -- its contract is to stay
    faithful to the user's wording -- so before Stage 5E.1 the second frame
    survived into the search query verbatim, and the provider matched one
    noisy string against one article.
    """
    parts = [part.strip() for part in _INNER_FRAME.split(text) if part.strip()]
    return parts[:MAX_CLAUSES] if parts else []


def _merge_clauses(clauses: List[str]) -> str:
    """One query from several clauses, adding only what is genuinely new.

    The first clause is kept whole, because it is the question the user led
    with and its wording is theirs. Later clauses contribute only their
    *substantive* new words -- so "what is the latest model of opus" adds
    "opus" and not a second copy of "latest model".

    Nothing is invented. Every word in the result was typed by the user, which
    is what keeps this a reduction of their question rather than a rewrite of
    it. `is_substantive` is the resolver's vocabulary, shared rather than
    duplicated for the reason the same import exists in `research.language`.
    """
    if not clauses:
        return ""

    base = clauses[0]
    seen = {word.lower() for word in _WORDS.findall(base)}
    extra: List[str] = []

    for clause in clauses[1:]:
        for word in _WORDS.findall(clause):
            lowered = word.lower()
            if lowered in seen:
                continue
            if not is_substantive(word):
                continue
            seen.add(lowered)
            extra.append(word)

    return " ".join([base] + extra) if extra else base


def assess(message: str) -> FreshnessAssessment:
    """Judge one **user** message. Never raises; NOT_REQUIRED is the default.

    Must not be called on anything a person did not type. See the module
    docstring: every other string in the system is content, and content
    acquiring intent is the failure this boundary exists to prevent.
    """
    if not message or not message.strip():
        return FreshnessAssessment()

    text = " ".join(message.split())
    if len(text) > MAX_MESSAGE_CHARS:
        return FreshnessAssessment(reason="message_too_long")

    # The user's own data, whatever the temporal wording. Checked first so a
    # currentness marker cannot drag "my calendar tomorrow" onto the web.
    if _PERSONAL_SCOPE.search(text):
        return FreshnessAssessment(
            requirement=FreshnessRequirement.NOT_REQUIRED,
            reason="personal_scope",
            source=_personal_source(text),
        )

    # About Mai, or about the user's own situation. Not about the world.
    if _PARTICIPANT_SUBJECT.search(text):
        return FreshnessAssessment(reason="participant_subject")

    # Work to do, not a fact to look up.
    if _TASK_REQUEST.search(text):
        return FreshnessAssessment(reason="task_request")

    marker = _RECENCY_MARKER.search(text)
    volatile = _VOLATILE_PREDICATE.search(text)

    if marker or volatile:
        subject = _subject_of(text)
        if not subject:
            # Recognised as a currentness question whose subject cannot be
            # read. Searching for the leftovers would send a string nobody
            # wrote to a third party, so this stays an ordinary turn.
            return FreshnessAssessment(
                requirement=FreshnessRequirement.NOT_REQUIRED,
                reason="subject_unreadable",
            )
        return FreshnessAssessment(
            requirement=FreshnessRequirement.REQUIRED,
            reason="recency_marker" if marker else "volatile_predicate",
            source=FreshnessSource.WEB,
            subject=subject,
        )

    # No marker. A definitional question is settled knowledge.
    if _DEFINITIONAL.search(text):
        return FreshnessAssessment(reason="definitional")

    if _VOLATILE_ROLE.search(text):
        # Answerable, but the kind of fact that turns over. Mai says so
        # instead of searching unbidden or implying the answer is current.
        return FreshnessAssessment(
            requirement=FreshnessRequirement.PREFERRED,
            reason="volatile_role",
            source=FreshnessSource.WEB,
            subject=_subject_of(text),
        )

    return FreshnessAssessment(reason="no_signal")


def _personal_source(text: str) -> FreshnessSource:
    if _CALENDAR_SCOPE.search(text):
        return FreshnessSource.CALENDAR
    if _MAIL_SCOPE.search(text):
        return FreshnessSource.MAIL
    return FreshnessSource.NONE


def _subject_of(text: str) -> str:
    """The user's question, reduced to a faithful search subject.

    Faithful is the requirement. "what is the latest model launched by
    ChatGPT?" must not become "latest", or "model", or "web search" -- the
    words that make the question answerable are the ones a search needs. So
    only the interrogative frame and trailing politeness come off, and
    everything in between survives, including the recency marker and any
    punctuation inside a name.
    """
    stripped = _FRAME.sub("", text, count=1)
    stripped = _TRAILING.sub("", stripped)
    stripped = " ".join(stripped.split())

    # Stage 5E.1. A second interrogative frame means a second question, and
    # the whole sentence is then a poor search string -- measured: the
    # compound form returned five slots filled by one article, while the
    # merged form returned five distinct sources.
    #
    # Only reached when a compound question is actually present, so a simple
    # question takes exactly the path it took before.
    clauses = _clauses(stripped)
    if len(clauses) > 1:
        stripped = _TRAILING.sub("", _merge_clauses(clauses))
        stripped = " ".join(stripped.split())

    if len(stripped) < 3:
        return ""
    return stripped[:MAX_SUBJECT_CHARS].strip()


def signals() -> Tuple[str, ...]:
    """The patterns this module recognises. For tests and for auditing."""
    return (
        _RECENCY_MARKER.pattern,
        _VOLATILE_PREDICATE.pattern,
        _VOLATILE_ROLE.pattern,
    )


__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_SUBJECT_CHARS",
    "FreshnessAssessment",
    "FreshnessRequirement",
    "FreshnessSource",
    "assess",
    "signals",
]
