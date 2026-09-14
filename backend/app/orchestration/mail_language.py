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
            rf"^{_LEAD}(?:find|search|look\s+for|show\s+me|get)\s+"
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
    # "What emails did I get today?"
    _Family(
        "what_emails",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:what|which|how\s+many)\s+(?:\w+\s+){{0,2}}?"
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
    # "Anything new in my inbox?"
    _Family(
        "anything_new",
        MailIntent.LIST,
        re.compile(
            rf"^{_LEAD}(?:anything|something|any\s+news)\s+"
            rf"(?:new\s+|unread\s+)?(?:in|from)\s+{_MY}\s+{_MAIL_NOUN}\b"
            rf"(?P<rest>.*)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

#: Text that discusses email rather than asking to read any.
#:
#: The research guard is the important one. "search the web for Gmail
#: pricing", "what is the latest Gmail feature?" and "google Gmail API
#: documentation" are questions *about* Gmail, and must reach the research
#: path exactly as they did before this module existed.
_ABOUT_THE_WEB = re.compile(
    r"\b(?:search(?:\s+(?:the\s+)?(?:web|internet|online))?\s+for|"
    r"google|look\s+up|research|on\s+the\s+web|online)\b",
    re.IGNORECASE,
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

        body_count = 0
        max_results = MAX_LIST_RESULTS
        if intent is MailIntent.READ:
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
    words = [w for w in term.split() if w.lower() not in _SENDER_STOPWORDS]
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
    "MAX_LIST_RESULTS",
    "MAX_SUMMARY_BODIES",
    "MailIntent",
    "MailRequest",
    "known_families",
    "known_intents",
    "recognise",
]
