"""Recognising a request about the user's email, and bounding what it asks.

The same shape as the calendar and briefing grammars, and for the same
reasons: a small set of request families, guards against text that merely
mentions email, no model call, and a deterministic result the application can
build authorization on.

What comes out is a **typed query**, never Gmail syntax. The user's words
supply a sender fragment or a subject term; this module puts them in fields,
and `MailQuery` turns fields into a query. So the path from "unread emails
from Netflix" to `from:("netflix") is:unread` runs entirely through
application code, and there is no point at which a phrase becomes an operator.

Recognition is not permission. What comes out is a candidate; execution being
switched on, the integration being connected, Stage 4C authorization and the
user's own confirmation all still stand between it and a request to Google.
"""

import enum
import re
from typing import NamedTuple, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Bounds. Constants, none derived from the message.
MAX_MESSAGE_CHARS = 1000
MAX_LIST_RESULTS = 5
MAX_SUMMARY_BODIES = 3
#: Most messages an "anything I should look at?" read may return.
#:
#: Larger than a plain listing because the question is a selection -- judging
#: which of three messages matters is not the question asked -- and still well
#: inside `gmail_schemas.MAX_MESSAGES`, which bounds it again on the way out.
#: Bodies are *not* read for it: a sender and a subject are what a triage
#: answer is built from, and reading eight bodies to answer "anything urgent?"
#: would send the model far more of the mailbox than the question needs.
MAX_ATTENTION_RESULTS = 8
MAX_SENDER_CHARS = 64
MAX_SUBJECT_TERM_CHARS = 64

#: The nouns that make a message about email rather than about something else.
#:
#: "mail" alone is deliberately absent -- "mail me the report", "royal mail",
#: "mailing list" are not requests to read a mailbox. "Gmail", "inbox" and
#: "email" are unambiguous; "mail" needs "my" in front of it, which the
#: families below require.
_MAIL_NOUN = (
    r"(?:e-?mails?|gmail|g-?mail|inbox|mailbox|messages?|correspondence)"
)

#: A possessive is required for the weaker nouns.
_MY = r"(?:my|the)"

_LEAD = (
    r"(?:(?:can|could|would|will)\s+you\s*,?\s*|"
    r"(?:please|hey|hi|hello|ok|okay|so|and|also)\s*,?\s*)?"
)


class MailIntent(str, enum.Enum):
    """What the user wants done with the messages once they are read.

    Three, because they call for three different amounts of data -- which is
    the point. A listing needs senders and subjects; a summary needs bodies.
    Asking for the narrower one when it will do is the whole of the
    minimum-data rule.
    """

    #: "what emails did I get today?" -- senders, subjects, dates. No bodies.
    LIST = "mail_list"
    #: "what did John say?" -- one body.
    READ = "mail_read"
    #: "summarise my latest emails" -- a few bodies.
    SUMMARISE = "mail_summarise"
    #: "which emails need my attention?" -- a bounded recent set, and the
    #: judgement is made during synthesis from what came back.
    #:
    #: Deliberately a separate intent rather than a LIST with a flag. What
    #: distinguishes it is not the retrieval -- that is an ordinary bounded
    #: read -- but that the *answer* is a selection, and the model must be
    #: told to select from the retrieved set rather than from its idea of
    #: what an important email looks like.
    ATTENTION = "mail_attention"


class _Family(NamedTuple):
    name: str
    intent: MailIntent
    pattern: "re.Pattern"


#: Request families, tried in order. Most specific first.
_FAMILIES: Tuple[_Family, ...] = (
    # "What did John say in his latest email?"
    _Family(
        "what_did_say",
        MailIntent.READ,
        re.compile(
            rf"^{_LEAD}what\s+did\s+(?P<sender>[^.?!,;]{{1,60}}?)\s+"
            rf"(?:say|write|send)\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Summarise the latest email from Acme."
    _Family(
        "summarise",
        MailIntent.SUMMARISE,
        re.compile(
            rf"^{_LEAD}(?:summari[sz]e|give\s+me\s+a\s+summary\s+of|"
            rf"tl;?dr)\b[^.?!]{{0,40}}?\b{_MAIL_NOUN}\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Find the email from Acme about the meeting."
    _Family(
        "find",
        MailIntent.READ,
        re.compile(
            # "show" without "me": "show my unread emails" and "show emails
            # from Amazon" are ordinary phrasings that reached no handler at
            # all -- the user asked for their mail and got a general answer.
            # The mail noun is still required, so "show me the weather" is
            # untouched.
            rf"^{_LEAD}(?:find|search|look\s+for|show|get)\s+"
            rf"(?:me\s+)?(?:the|a|any|an)?\s*(?:\w+\s+){{0,2}}?"
            rf"{_MAIL_NOUN}\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Do I have any unread emails from Netflix?"
    _Family(
        "do_i_have",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:do\s+i\s+have|have\s+i\s+got|are\s+there|is\s+there)\s+"
            rf"(?:any\s+|an?y?\s+)?(?:\w+\s+){{0,2}}?{_MAIL_NOUN}\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "What emails did I get today?", "what are my latest emails?"
    #
    # Three intervening words rather than two: "what **are my latest**
    # emails?" is an ordinary way to ask, and at two it fell through to no
    # handler at all -- the user asked for their mail and got a general
    # answer. The product guard below is what keeps the extra word from
    # widening this into questions *about* Gmail.
    _Family(
        "what_emails",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:what|which|how\s+many)\s+(?:\w+\s+){{0,3}}?"
            rf"{_MAIL_NOUN}\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Check my latest emails.", "check my gmail"
    _Family(
        "check_mail",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:check|read|open|see|look\s+at|go\s+through)\s+"
            rf"(?:{_MY}\s+)?(?:\w+\s+){{0,2}}?{_MAIL_NOUN}\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Any emails I need to respond to?", "any unread messages from Amazon?"
    #
    # A bare "any <noun>" lead. Narrow because the mail noun is still
    # required: "any news?" and "any ideas?" are untouched.
    _Family(
        "any_mail",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:any|some)\s+(?:\w+\s+){{0,2}}?"
            rf"{_MAIL_NOUN}\b(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # "Do I have anything from Google?"
    #
    # The one family with no mail noun, and the narrowest in the table: it
    # requires a sender-shaped fragment and must end there. "Do I have
    # anything from Google?" is recognised; "do I have anything from Google
    # about the meeting tomorrow" is not, because at that length the sentence
    # is as likely to be about the calendar.
    #
    # "Do I have anything important today?" is deliberately *not* here. It
    # carries no mail noun and no sender, the calendar recogniser already
    # claims it, and the calendar runs first -- so adding it would either be
    # dead code or a regression in calendar routing.
    _Family(
        "anything_from",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:do\s+i\s+have|have\s+i\s+got|is\s+there|"
            rf"are\s+there)\s+(?:any(?:thing)?|some(?:thing)?)\s+"
            rf"from\s+(?P<sender>[A-Za-z0-9.@_\-+]{{2,64}})\s*\??$",
            re.IGNORECASE,
        ),
    ),
    # "Anything new in my inbox?"
    _Family(
        "anything_new",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}"
            rf"(?:(?:do\s+i\s+have|have\s+i\s+got|is\s+there|are\s+there)\s+)?"
            rf"(?:anything|something|any\s+news)\s+"
            # Up to two intervening words, so "anything **urgent** in my
            # inbox" and "anything important in my email" are recognised.
            rf"(?:\w+\s+){{0,2}}?(?:in|from)\s+{_MY}\s+{_MAIL_NOUN}\b"
            rf"(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

#: Words that mean "the ones that matter", and what they must never become.
#:
#: These set the *intent* and nothing else. "Important" is not a Gmail
#: operator and must never become a search term: `subject:("important")` would
#: find messages with the word in the subject line, which is not the question
#: asked and would quietly answer a different one. Gmail's own `is:important`
#: is equally absent -- it is Google's classifier, not the user's judgement,
#: and `MailQuery` has no field that could carry it.
#:
#: So a priority word changes how many messages are read and how the answer is
#: written. It never changes *which* messages Gmail is asked for.
_PRIORITY = re.compile(
    r"\b(?:important|urgent|priority|pressing|critical|"
    r"need(?:s)?\s+(?:my\s+)?(?:attention|response|reply|replying)|"
    r"needs?\s+(?:to\s+be\s+)?(?:answered|actioned)|"
    r"requires?\s+(?:my\s+)?attention|"
    r"(?:should|must|do)\s+i\s+(?:look\s+at|read|respond|reply|deal\s+with)|"
    r"i\s+need\s+to\s+(?:respond|reply|answer|look\s+at|deal\s+with)|"
    r"worth\s+(?:reading|a\s+look)|"
    r"anything\s+i\s+(?:missed|should\s+know))\b",
    re.IGNORECASE,
)

#: Words a priority phrase must never contribute to a query.
#:
#: Belt and braces: the intent path already keeps them out of the fields, and
#: these make a regression visible if someone later routes them through
#: `_subject_terms_in`. A test asserts the two agree.
PRIORITY_WORDS = frozenset({
    "important", "urgent", "priority", "pressing", "critical", "attention",
    "response", "reply", "answered", "actioned", "unanswered",
})

#: Text that discusses email rather than asking to read any.
#:
#: The research guard is the important one. "search the web for Gmail
#: pricing", "what is the latest Gmail feature?" and "google Gmail API
#: documentation" are questions *about* Gmail, and must reach the research
#: path exactly as they did before this module existed.
_ABOUT_THE_WEB = re.compile(
    r"\b(?:search(?:\s+(?:the\s+)?(?:web|internet|online))?\s+for|"
    # "google" is the search verb -- except after "from", where it is a
    # sender. Without the lookbehind, "do I have anything from Google?" was
    # read as a request to search the web and never reached the mailbox.
    # The verb sense is untouched: "google the latest news" still guards.
    r"(?<!from )google|"
    r"look\s+up|research|on\s+the\s+web|online)\b",
    re.IGNORECASE,
)
#: Questions about the mail *product* rather than the user's mailbox.
#:
#: "What changed in Gmail recently?" and "what is the latest Gmail feature?"
#: name Gmail and ask nothing about the mailbox -- they are news and product
#: questions, and Stage 5A.1 requires them to reach web research. The tell is
#: a change/feature/release word with **no possessive**: "check my gmail" is
#: the user's mail however it is worded, and "what changed in Gmail" is not.
_PRODUCT_QUESTION = re.compile(
    r"(?!.*\b(?:my|our)\b)"
    r".*\b(?:changed|change|new\s+features?|latest\s+features?|feature|"
    r"features|release[ds]?|version|update[ds]?|pricing|price|outage|"
    r"down|roadmap|announcement)\b",
    re.IGNORECASE | re.DOTALL,
)

_EXPLANATORY = re.compile(
    r"\b(?:what\s+is\s+(?:a|an|the)\b|what(?:'|’)?s\s+(?:a|an)\b|"
    r"explain|how\s+(?:do|does|to|can)\b|"
    r"the\s+latest\s+\w+\s+feature|api\s+documentation|pricing)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:don'?t|do\s+not|cannot|can'?t|won'?t|never|without|no\s+need)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_PAST = re.compile(
    r"\bi\s+(?:already\s+)?(?:read|checked|saw|deleted|replied|sent|archived)\b",
    re.IGNORECASE,
)
_WISH = re.compile(r"\b(?:i\s+wish|if\s+only|i'?d\s+love)\b", re.IGNORECASE)

#: A request to *change* the mailbox. Recognised so it can be refused
#: truthfully, never so it can be performed -- no write capability exists.
_WRITE_VERB = (
    r"(?:send|reply|respond|forward|delete|trash|archive|star|unstar|"
    r"label|mark|draft|compose|write|unsubscribe|move|spam)"
)
_WRITE_REQUEST = re.compile(
    rf"^{_LEAD}{_WRITE_VERB}\b[^.?!]{{0,60}}?\b(?:{_MAIL_NOUN}|to\s+\S+@)",
    re.IGNORECASE,
)

#: "email this to everyone", "mail him the file", "email bob@x.com".
#:
#: A second pattern rather than an entry in `_WRITE_VERB`, because these verbs
#: need a *recipient-shaped* object and the others do not. Widening the shared
#: object to "to <anything>" would have made "move to the next topic" a mail
#: write request -- "move" is already a write verb, and "to the" would have
#: matched.
_SEND_REQUEST = re.compile(
    rf"^{_LEAD}(?:e-?mail|mail)\s+"
    #: Pronouns and addresses only. A proper-noun alternative was here and
    #: was removed: under `IGNORECASE` a `[A-Z][a-z]+` class matches any
    #: lowercase word, so "mail order companies are common" became a request
    #: to send mail. "email John" now reaches an ordinary turn instead, where
    #: the runtime capability facts answer it truthfully.
    rf"(?:this|that|it|them|him|her|me|us|everyone|anyone|\S+@\S+)\b",
    re.IGNORECASE,
)

#: "from X" / "by X" -- the sender fragment.
_FROM = re.compile(
    r"\bfrom\s+(?P<sender>[A-Za-z0-9.@_\-+]{2,64})", re.IGNORECASE
)
#: "about X" -- a subject term.
_ABOUT = re.compile(
    r"\babout\s+(?:the\s+|a\s+|an\s+)?(?P<term>[A-Za-z0-9 .'\-]{2,64})",
    re.IGNORECASE,
)
_UNREAD = re.compile(r"\bunread\b|\bnew\b", re.IGNORECASE)
#: "the latest email" is one message. Asking for three would fetch two bodies
#: nobody wanted -- the minimum-data rule applied to the user's own grammar.
#: A plural mail noun. "emails", "messages", "e-mails".
_PLURAL = re.compile(r"\b(?:e-?mails|messages|gmails)\b", re.IGNORECASE)

_SINGULAR = re.compile(
    r"\b(?:the\s+)?(?:latest|last|most\s+recent|newest)\s+"
    r"(?:e-?mail|message|gmail)\b(?!s)",
    re.IGNORECASE,
)
_TODAY = re.compile(r"\btoday\b|\bthis\s+morning\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)
_THIS_WEEK = re.compile(r"\bthis\s+week\b|\bpast\s+week\b|\blast\s+week\b", re.IGNORECASE)

#: Words that are never a sender on their own.
_SENDER_STOPWORDS = frozenset({
    "my", "the", "a", "an", "any", "all", "them", "him", "her", "it", "you",
    "me", "us", "anyone", "someone", "everyone", "today", "yesterday",
    "unread", "new", "latest", "recent", "last", "first", "email", "emails",
    "e-mail", "e-mails", "gmail", "inbox", "mail", "message", "messages",
    "his", "hers", "their", "in", "on", "at", "and", "or",
})


class MailRequest(NamedTuple):
    """A recognised email request, resolved to a bounded query.

    Application state, never model output. Every field is either a constant, a
    boolean the grammar set, or a fragment of the user's own words that has
    been bounded -- and `MailQuery` cleans them again before they reach a
    query string.
    """

    family: str = ""
    intent: Optional[MailIntent] = None
    sender: str = ""
    subject_terms: Tuple[str, ...] = ()
    unread_only: bool = False
    newer_than_days: Optional[int] = None
    max_results: int = MAX_LIST_RESULTS
    #: How many bodies the answer needs. Zero for a listing.
    body_count: int = 0
    is_write_request: bool = False
    #: Whether the user asked which messages matter, rather than for all of
    #: them. Shapes the proposal sentence and the synthesis instruction; it is
    #: **not** a query field and never reaches Gmail.
    priority: bool = False

    @property
    def is_request(self) -> bool:
        return bool(self.family) or self.is_write_request

    @property
    def is_readable(self) -> bool:
        return bool(self.family) and not self.is_write_request


def recognise(message: str) -> MailRequest:
    """Read one message. Never raises; not-a-request is the default."""
    if not message or not message.strip():
        return MailRequest()

    text = " ".join(message.split())
    if len(text) > MAX_MESSAGE_CHARS:
        return MailRequest()

    if _WRITE_REQUEST.search(text) or _SEND_REQUEST.search(text):
        # Recognised so Mai can say plainly that it cannot do this, rather
        # than answering a "send this" with a list of messages. No write
        # capability exists anywhere; this only shapes the reply.
        return MailRequest(is_write_request=True)

    if (
        _NEGATION.search(text)
        or _EXPLANATORY.search(text)
        or _FIRST_PERSON_PAST.search(text)
        or _WISH.search(text)
        or _ABOUT_THE_WEB.search(text)
        or _PRODUCT_QUESTION.match(text)
    ):
        # The last of these keeps "search the web for Gmail pricing" on the
        # research path, where it belongs.
        return MailRequest()

    for family in _FAMILIES:
        match = family.pattern.search(text)
        if match is None:
            continue

        rest = (match.groupdict().get("rest") or "")
        sender = _clean_sender(match.groupdict().get("sender") or "") or _sender_in(text)
        subject_terms = _subject_terms_in(text)

        intent = family.intent
        # A plural noun means a listing, whatever family matched. "show me
        # unread emails from Netflix" wants senders and subjects; "find the
        # email from Acme" wants the one message. Reading five bodies for the
        # first would send four the user never asked for.
        if intent is MailIntent.READ and _PLURAL.search(text) and not _SINGULAR.search(text):
            intent = MailIntent.LIST

        # A priority question is still a bounded read; what changes is how
        # many messages are worth looking at and how the answer is written.
        # The priority words themselves go no further than this boolean --
        # they are not added to `subject_terms`, and there is no query field
        # they could reach even if they were.
        priority = bool(_PRIORITY.search(text))
        if priority and intent is MailIntent.LIST:
            intent = MailIntent.ATTENTION

        body_count = 0
        max_results = MAX_LIST_RESULTS
        if intent is MailIntent.ATTENTION:
            # Metadata only. Judging what needs a reply from senders and
            # subjects is the bounded question; reading eight bodies to
            # answer "anything urgent?" would send most of a mailbox to the
            # model to answer a triage question.
            max_results = MAX_ATTENTION_RESULTS
            body_count = 0
        elif intent is MailIntent.READ:
            # One message, one body: "what did John say" wants the message,
            # not the mailbox.
            max_results = 1
            body_count = 1
        elif intent is MailIntent.SUMMARISE:
            singular = bool(_SINGULAR.search(text))
            max_results = 1 if singular else MAX_SUMMARY_BODIES
            body_count = max_results

        return MailRequest(
            family=family.name,
            intent=intent,
            sender=sender,
            subject_terms=subject_terms,
            unread_only=bool(_UNREAD.search(text)),
            newer_than_days=_days_in(text),
            max_results=max_results,
            body_count=body_count,
            priority=priority,
        )

    return MailRequest()


def _sender_in(text: str) -> str:
    match = _FROM.search(text)
    return _clean_sender(match.group("sender")) if match else ""


def _clean_sender(raw: str) -> str:
    """A sender fragment, or "". Bounded and stripped of filler."""
    words = [
        word for word in re.split(r"\s+", (raw or "").strip())
        if word and word.strip(".,'").lower() not in _SENDER_STOPWORDS
    ]
    if not words:
        return ""
    # One token. A sender is a name, an address or a domain, never a phrase.
    sender = words[0].strip(".,'\"")
    sender = re.sub(r"[^A-Za-z0-9.@_\-+]", "", sender)
    return sender[:MAX_SENDER_CHARS] if len(sender) >= 2 else ""


def _subject_terms_in(text: str) -> Tuple[str, ...]:
    match = _ABOUT.search(text)
    if match is None:
        return ()
    term = " ".join(match.group("term").split())[:MAX_SUBJECT_TERM_CHARS]
    # Priority words are dropped here as well as never being routed here.
    # "emails about important changes" asks about changes; searching Gmail
    # for the literal word "important" would answer a different question,
    # and `is:important` -- Google's classifier rather than the user's
    # judgement -- has no field on `MailQuery` at all.
    words = [
        w for w in term.split()
        if w.lower() not in _SENDER_STOPWORDS and w.lower() not in PRIORITY_WORDS
    ]
    term = " ".join(words[:3]).strip(".,'\"")
    return (term,) if len(term) >= 2 else ()


def _days_in(text: str) -> Optional[int]:
    """A coarse date bound, in days. Never finer than Gmail supports."""
    if _TODAY.search(text):
        return 1
    if _YESTERDAY.search(text):
        return 2
    if _THIS_WEEK.search(text):
        return 7
    return None


def known_families() -> Tuple[str, ...]:
    return tuple(family.name for family in _FAMILIES)


def known_intents() -> Tuple[str, ...]:
    return tuple(intent.value for intent in MailIntent)


__all__ = [
    "MAX_ATTENTION_RESULTS",
    "MAX_LIST_RESULTS",
    "PRIORITY_WORDS",
    "MAX_SUMMARY_BODIES",
    "MailIntent",
    "MailRequest",
    "known_families",
    "known_intents",
    "recognise",
]
