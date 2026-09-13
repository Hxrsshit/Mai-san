"""Recognising a research request in ordinary language, and extracting its subject.

Stage 4F-D identified research with five literal phrases -- "search the web",
"search online", and three more. That worked for the exact wordings it listed
and for nothing else: *"Search up the web and find out about Godzilla Minus
One"* matched none of them, because the intervening "up" defeats a literal
phrase. No candidate meant no proposal, so the turn became an ordinary one and
the model answered from runtime facts, which say Mai cannot perform actions.
The user saw "I can't search the web" from a Mai that could.

The fix is not a longer list. A list long enough to cover paraphrase is long
enough to fire on mention, which is the failure Stage 4D was avoiding when it
removed the phrase "web search" for matching *"tell me about web search
engines"*. So this module is a small **grammar** instead: a handful of request
shapes that capture their subject, and a set of guards that reject text which
talks *about* searching rather than asking for one.

Deterministic throughout. No model is consulted -- not to recognise a request
and not to extract a query. Extraction by model would put a model call on a
path that makes none, and would let a model choose what Mai sends to a third
party.

**Recognition is not permission.** What comes out of here is a *candidate*. It
still travels Stage 4C authorization, the Stage 4F-D consent gate and the
Stage 4E dispatcher before anything reaches the network.
"""

import re
from typing import NamedTuple, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Longest subject this module will produce. Below the search layer's own
#: bound, so the query is shortened here rather than truncated there.
MAX_QUERY_CHARS = 240

#: Shortest subject worth searching for. One character is a typo, not a topic.
MIN_QUERY_CHARS = 2

# --- Grammar fragments ------------------------------------------------------

#: Where a search happens. Required by the scoped family below, which is what
#: separates "search the web for X" from "search my notes for X".
_SCOPE = r"(?:the\s+web|the\s+internet|the\s+net|online|on\s+the\s+web|on\s+the\s+internet)"

#: Politeness and modality that may precede any request.
_LEAD = r"(?:(?:can|could|would|will)\s+you\s+|please\s+|hey\s+|i\s+want\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+)?"

#: What may sit between a scoped verb and its subject.
_CONNECTOR = (
    r"(?:\s*,)?\s*(?:and\s+)?"
    r"(?:find\s+out\s+about|find\s+out|tell\s+me\s+about|tell\s+me|"
    r"explain|look\s+for|search\s+for|see\s+about|for|about|on)?\s*"
)

#: Google products Mai either integrates with or would have to.
#:
#: "google calendar" is the name of a thing. Treating it as "search the web
#: for calendar" is the defect Stage 5A found -- and the neighbours are listed
#: too, because "add this to my google drive" should reach an honest "I can't
#: do that" rather than succeed as a web search for the word "drive".
_GOOGLE_PRODUCT = (
    r"(?:calendars?|drive|docs?|sheets|slides|meet|mail|gmail|"
    r"photos|maps|keep|tasks|contacts|chat|workspace|account)"
)

#: Request shapes, most specific first. Each captures `subject`.
#:
#: Ordering matters: the scoped family must be tried before the bare ones, so
#: "search the web for X" yields "X" rather than "the web for X".
_FAMILIES = (
    # "Search the web for X", "Search up the web and find out about X",
    # "Look online for X", "Check online and tell me about X"
    (
        "scoped_search",
        re.compile(
            rf"{_LEAD}(?:search|look|check|hunt|dig)(?:\s+up)?\s+{_SCOPE}"
            rf"{_CONNECTOR}(?P<subject>.+)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Search up X on the web", "Look X up online" -- scope trailing the subject.
    (
        "trailing_scope",
        re.compile(
            rf"{_LEAD}(?:search|look)(?:\s+up)?\s+(?P<subject>.+?)\s+"
            rf"(?:up\s+)?{_SCOPE}\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "What's the latest on X?"
    (
        "latest_on",
        re.compile(
            r"what(?:'|’)?s?\s+(?:is\s+)?the\s+(?:latest|newest|current)\s+"
            r"(?:news\s+)?(?:on|about|with|for)\s+(?P<subject>.+)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Find out about X", "Find information about X",
    # "Find out what's happening with X"
    (
        "find_out",
        re.compile(
            rf"{_LEAD}find\s+(?:out\s+)?"
            r"(?:what(?:(?:'|’)?s|\s+is)\s+happening\s+(?:with|to|in)\s+|"
            r"information\s+(?:about|on)\s+|"
            r"the\s+latest\s+(?:information\s+)?(?:about|on)\s+|"
            r"about\s+|out\s+about\s+)"
            r"(?P<subject>.+)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Research X", "Google X"
    #
    # `google` carries two guards that `research` does not need, because it is
    # the only trigger here that is also a company whose products Mai
    # integrates with.
    #
    # Without them "what is on my google calendar tomorrow?" parsed as
    # `google <subject>` and searched the web for "calendar tomorrow" -- a
    # correctly spelled, entirely ordinary calendar question sent to a search
    # provider. The typo report that prompted Stage 5A exposed this defect;
    # the spelling was never the cause of it.
    #
    #   a determiner before it  "my google calendar" is a noun phrase, not an
    #                           instruction. Nobody commands "the google X".
    #   a product name after it "google calendar" names a thing, not a search
    #                           subject.
    (
        "research_verb",
        re.compile(
            rf"{_LEAD}(?:research|(?<!\bmy\s)(?<!\bthe\s)(?<!\byour\s)"
            rf"(?<!\bour\s)(?<!\btheir\s)(?<!\bhis\s)(?<!\bher\s)"
            rf"google(?!\s+{_GOOGLE_PRODUCT}\b))\s+(?P<subject>.+)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Look up X", "Search for X"
    (
        "lookup_verb",
        re.compile(
            rf"{_LEAD}(?:look\s+up|search\s+for)\s+(?P<subject>.+)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

#: A trailing instruction to search, after the subject has already been given.
#:
#: "What's the latest on India's AI policy? Search online." -- the subject is
#: in the first sentence and the second is only the instruction. Stripped
#: before matching so the families see the subject rather than an empty tail.
_TRAILING_COMMAND = re.compile(
    rf"[.,;!?]*\s*(?:and\s+|then\s+)?{_LEAD}"
    rf"(?:search|look\s+(?:it|this|that)\s+up|check|google\s+it)"
    rf"(?:\s+up)?(?:\s+{_SCOPE})?\s*[.!?]*\s*$",
    re.IGNORECASE,
)

# --- Guards: text that talks about searching rather than asking for one -----

#: A question *about* the capability or the concept, not a request.
#:
#: "what" is deliberately absent: "What's the latest on X?" is a request, and
#: it is the one interrogative that is.
_INTERROGATIVE = re.compile(r"^\s*(?:why|how|when|who|whose|whom)\b", re.IGNORECASE)

#: Negation anywhere in the message.
#:
#: Blunt on purpose. "Find out about X, but don't guess" is refused along with
#: "don't search the web", and refusing a genuine request is the safe
#: direction: the user rephrases, and nothing was sent anywhere.
_NEGATION = re.compile(
    r"\b(?:don'?t|do\s+not|does\s+not|doesn'?t|cannot|can'?t|won'?t|will\s+not|"
    r"shouldn'?t|should\s+not|never|no\s+need|without)\b",
    re.IGNORECASE,
)

#: The user reporting their own past action.
_FIRST_PERSON_PAST = re.compile(
    r"\bi\s+(?:already\s+)?(?:searched|looked|googled|checked|researched|found)\b",
    re.IGNORECASE,
)

#: A request to explain the concept rather than to perform it.
_EXPLANATORY = re.compile(
    r"\b(?:explain\s+how|how\s+(?:does|do|to|would|can)|what\s+is\s+(?:a\s+|an\s+)?"
    r"(?:web\s+)?search\b|is\s+an?\s+\w+\s+concept)\b",
    re.IGNORECASE,
)

#: Subjects that are not subjects -- anaphora with no antecedent this layer
#: can see. "Look this up online" is a genuine request whose subject lives in
#: the previous turn, which deterministic extraction cannot reach.
_ANAPHORIC = frozenset({
    "this", "that", "it", "them", "these", "those", "the above", "my question",
    "what i said", "the previous", "this one", "that one",
})

#: Words that cannot be the whole of a search subject.
_EMPTY_SUBJECT = frozenset({
    "", "for", "about", "on", "up", "me", "please", "something", "anything",
    "stuff", "things", "info", "information", "more",
})


#: A captured "subject" that begins like a predicate, which means the verb
#: before it was a noun.
#:
#: "Research is important in science" captures "is important in science". The
#: word "Research" there is the sentence's subject, not an imperative, and the
#: giveaway is that what follows is a verb phrase rather than a topic. Without
#: this, any sentence *about* research would propose one.
#: A heuristic over a genuinely open set -- any finite verb can follow a noun
#: -- so it lists the ones that actually occur. The failure direction is
#: deliberate: an unlisted verb produces a *proposal* the user declines, which
#: costs a message, while the reverse would search for a clause nobody asked
#: about.
_SUBJECT_STARTS_WITH_PREDICATE = re.compile(
    r"^\s*(?:is|are|was|were|isn'?t|aren'?t|means?|matters?|helps?|seems?|"
    r"appears?|remains?|becomes?|has|have|had|will|would|should|could|may|"
    r"might|must|can|shows?|suggests?|indicates?|proves?|demonstrates?|"
    r"reveals?|confirms?|involves?|requires?|takes?|needs?|tells?|says?|"
    r"finds?|found|gives?|makes?|comes?|goes?|tends?|often|usually|always|"
    r"generally|typically|that)\b",
    re.IGNORECASE,
)

#: A subject that is itself interrogative, so its question mark is part of it.
_SUBJECT_IS_A_QUESTION = re.compile(
    r"^\s*(?:who|what|where|when|why|how|which|whose|is|are|was|were|did|does|do)\b",
    re.IGNORECASE,
)


class Recognition(NamedTuple):
    """What the recogniser made of one message.

    Three outcomes rather than two, because "this is a research request but I
    cannot tell what about" is not the same as "this is not a research
    request", and they call for completely different replies.
    """

    #: The request shape that matched, for logs and tests. Empty when none did.
    family: str = ""
    #: The extracted subject. Empty when a request was recognised but its
    #: subject could not be determined.
    query: str = ""
    #: Why the subject is missing, when it is. An application constant.
    needs_clarification: str = ""

    @property
    def is_request(self) -> bool:
        return bool(self.family)

    @property
    def is_actionable(self) -> bool:
        """A request *and* a usable subject."""
        return bool(self.family and self.query)


def recognise(message: str) -> Recognition:
    """Read one message. Never raises; returns a non-request by default.

    The overwhelmingly common answer is "not a research request", and it is
    the safe one.
    """
    if not message or not message.strip():
        return Recognition()

    text = " ".join(message.split())
    if len(text) > 2000:
        # Far past any real request. Refusing is cheaper than scanning, and a
        # message this long is not an imperative.
        return Recognition()

    if _is_about_searching(text):
        return Recognition()

    stripped, had_trailing = _strip_trailing_command(text)

    for family, pattern in _FAMILIES:
        match = pattern.search(stripped)
        if match is None:
            continue

        subject = _clean_subject(match.group("subject"))

        if _SUBJECT_STARTS_WITH_PREDICATE.match(subject):
            # The verb was a noun. This message is about research, not a
            # request for it.
            return Recognition()

        if subject.lower() in _ANAPHORIC:
            return Recognition(
                family=family, needs_clarification="anaphoric_subject"
            )
        if not subject or subject.lower() in _EMPTY_SUBJECT:
            return Recognition(family=family, needs_clarification="empty_subject")
        if len(subject) < MIN_QUERY_CHARS:
            return Recognition(family=family, needs_clarification="subject_too_short")

        return Recognition(family=family, query=subject[:MAX_QUERY_CHARS])

    if had_trailing:
        # The message ended in "search online" but nothing before it parsed as
        # a subject. A request without a topic.
        return Recognition(
            family="trailing_command", needs_clarification="empty_subject"
        )

    return Recognition()


def _is_about_searching(text: str) -> bool:
    """Whether this message discusses searching rather than requesting it."""
    return bool(
        _INTERROGATIVE.search(text)
        or _NEGATION.search(text)
        or _FIRST_PERSON_PAST.search(text)
        or _EXPLANATORY.search(text)
    )


def _strip_trailing_command(text: str):
    """Remove a trailing "search online" so the subject before it can match."""
    stripped = _TRAILING_COMMAND.sub("", text).strip()
    if stripped and stripped != text:
        return stripped, True
    return text, False


def _clean_subject(raw: str) -> str:
    """Tidy the captured subject without rewriting it.

    Whitespace is collapsed, a trailing command is removed, and dangling
    connectives and terminal punctuation are trimmed. Nothing is summarised,
    reworded, or added: quoted phrases, capitalisation, apostrophes and
    question marks all survive, because they are the user's own words and
    they are what gets shown in the consent prompt.
    """
    subject = " ".join((raw or "").split())
    subject = _TRAILING_COMMAND.sub("", subject).strip()

    # A dangling connective left by an over-eager capture.
    subject = re.sub(
        r"^(?:for|about|on|up|and|to|the\s+topic\s+of)\s+", "", subject,
        flags=re.IGNORECASE,
    ).strip()

    # Trailing politeness. "google this for me" should reduce to "this", which
    # the anaphoric check then catches -- otherwise the literal words "this
    # for me" would be sent to a search provider.
    subject = re.sub(
        r"\s+(?:for\s+me|please|thanks|thank\s+you|if\s+you\s+can)\s*$",
        "", subject, flags=re.IGNORECASE,
    ).strip()

    subject = subject.rstrip(".,;:! ").strip()

    # A trailing question mark belongs to the *request* in "Can you research
    # X?" and to the *subject* in "who won the 2026 US Open?". Kept only when
    # the subject is itself a question, which is the case that reads worse
    # without it.
    if subject.endswith("?") and not _SUBJECT_IS_A_QUESTION.match(subject):
        subject = subject.rstrip("? ").strip()

    # Balanced quotes are kept; a single dangling one is noise.
    if subject.count('"') == 1:
        subject = subject.replace('"', "").strip()

    return subject


def known_families():
    """The request shapes this module recognises. For tests and auditing."""
    return tuple(name for name, _ in _FAMILIES)


__all__ = [
    "MAX_QUERY_CHARS",
    "MIN_QUERY_CHARS",
    "Recognition",
    "known_families",
    "recognise",
]
