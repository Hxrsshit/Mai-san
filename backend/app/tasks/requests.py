"""Stage 6N: confirmed Gmail monitoring, from a chat turn to a scheduled task.

The front half of the agent loop, and the one door a chat turn has into the
task layer:

    turn N      "watch my Gmail for an email from apple.com about invoice
                 and tell me when it arrives"
                  -> recognise()                   (a grammar; no model)
                  -> one savepoint: create_for_user -> attach_plan
                                    -> configure_monitoring
                  -> a PLANNED task: the proposal. Runtime-inert.
                  -> an application-written proposal reply

    turn N+1    "yes, do it"
                  -> classify_reply()              (a grammar; no model)
                  -> the pending row, claimed conditionally
                  -> one savepoint: authorize_plan -> transition(QUEUED)
                                    -> GrantService.create
                                    -> schedule_background
                  -> "Done. I'm monitoring ..."   only after it succeeded

Everything after scheduling already existed and is not touched here: the
background runtime claims the task, the runner checks it through the one
authorization service and the one execution service, Gmail is listed, the
evaluator compares, and 6H records a notification on a match.

### The proposal is inert

A proposal is a task in `planned` with a monitor, no `authorized_at` and no
`next_run_at`. The runtime claims only authorized, scheduled `queued` or
`running` tasks; `schedule_background` refuses an unauthorized one; the
runner refuses one with `plan_not_authorized`. No grant exists until the
person confirms. So writing a proposal cannot run anything, read Gmail, or
write a notification -- and the propose path below names none of the
authorization, grant or scheduling calls, which a structural test asserts.

### What the person confirms is exactly what was written

The plan and the monitor are write-once and are written before
authorization, so the row the person saw is the row confirmation authorizes.
Anything other than a plain confirmation cancels the proposal. A changed
interval, sender or subject is never applied to the old row: the person sends
a new request and gets a new proposal.

### The baseline

`received_after` is the second the proposal was written (owner decision R1,
2026-10-08). Only mail Gmail received after it counts, rendered as Gmail's
own `after:<epoch>`, whose behaviour was verified live before this stage
(V-1). Mail arriving between the request and the confirmation counts as new:
it arrived after the person asked.

### The end

Owner decisions R2/R3 and the 6N answers of 2026-10-08:

* A monitor checks every 15 minutes by default, for 7 days by default, within
  5 minutes..24 hours and 1 hour..7 days.
* It can make at most `MAX_MONITOR_CHECKS` checks -- the task budget's
  `max_tool_calls`, which a caller cannot raise -- so its deadline is the
  sooner of the lifetime asked for and that many intervals. The proposal
  shows the deadline itself, so nothing about it is hidden.
* At the deadline the runner refuses with `monitor_expired` and the runtime
  cancels the task with reason `expired`: terminal, not a failure, no
  notification, and the grant released.

### The grant

Capability-wide, as the standing-grant model is: `gmail_list_messages`, at
the capability's own risk (HIGH), with a TTL that ends at the deadline (at
most `MAX_GRANT_TTL_SECONDS`). Created only by a confirmation, and only the
runner consults grants (`authorize_with_grants`), so a chat-time mail read
still asks the person as before.

### Owner

`MonitorRequestService` takes the owner from its caller, which takes it from
the one authentication seam (`app.api.deps.get_owner_id`). The Telegram
webhook passes none, so monitoring is recognised there but never set up.
"""

import enum
import math
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Optional, Tuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.gmail_schemas import NO_MESSAGES_MATCHED
from app.tasks.models import Task, TaskStep
from app.tasks.monitoring import MAX_MONITOR_LIFETIME_SECONDS
from app.tasks.schemas import DEFAULT_BUDGET, TaskOutcome, TaskResult
from app.tasks.service import TaskService
from app.tasks.states import TaskState

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)

# --- Bounds ------------------------------------------------------------------
#
# Constants in application code. Nothing a message says can widen them.

#: The one capability a monitor uses. Metadata only, read only.
MONITOR_CAPABILITY = "gmail_list_messages"

#: How long a proposal waits for its answer.
PROPOSAL_TTL_SECONDS = 900

#: Check interval: default and bounds.
DEFAULT_INTERVAL_SECONDS = 15 * 60
MIN_INTERVAL_SECONDS = 5 * 60
MAX_INTERVAL_SECONDS = 24 * 3600

#: Lifetime: default and bounds. The ceiling is the monitoring ceiling.
DEFAULT_LIFETIME_SECONDS = MAX_MONITOR_LIFETIME_SECONDS
MIN_LIFETIME_SECONDS = 3600
MAX_LIFETIME_SECONDS = MAX_MONITOR_LIFETIME_SECONDS

#: The most checks a monitor may make: the task budget's tool-call bound,
#: which a caller may tighten and never raise. A deadline further away than
#: this many intervals would end in `budget_exceeded` instead of at the
#: deadline, so the deadline is brought forward to meet it.
MAX_MONITOR_CHECKS = DEFAULT_BUDGET["max_tool_calls"]

MAX_REQUEST_CHARS = 300
MAX_REPLY_CHARS = 40
MAX_SUBJECT_TERMS = 3
MAX_SENDER_CHARS = 96
MAX_TERM_CHARS = 64

#: Most grants one release may revoke. A bound on a loop, nothing more.
MAX_RELEASED_GRANTS = 20


# --- Recognition: the request ------------------------------------------------


class RecognitionKind(str, enum.Enum):
    NOT_MONITOR = "not_monitor"
    REQUEST = "request"
    CLARIFY = "clarify"
    REFUSED = "refused"


class MonitorRequest(NamedTuple):
    """What the person asked to monitor. Built by the grammar, nothing else."""

    sender: str
    subject_terms: Tuple[str, ...]
    interval_seconds: int
    lifetime_seconds: int


class Recognition(NamedTuple):
    kind: RecognitionKind
    request: Optional[MonitorRequest] = None
    #: An application reason code, for a clarification or a refusal.
    reason: Optional[str] = None


def _normalise(message: str) -> str:
    """NFKC, case-folded, one space between words."""
    text = unicodedata.normalize("NFKC", message or "").casefold()
    # NFKC leaves typographic quotes alone; the grammars want plain ones.
    text = text.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})
    return " ".join(text.split())


_LEAD = (
    r"(?:(?:hey|hi|ok|okay)\s+mai\s*,?\s+)?(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
)
_TRIGGER_FORM = re.compile(
    rf"^{_LEAD}(?:watch|monitor|keep\s+an\s+eye\s+on)\s+"
    # "watch my Gmail ...", or "watch (out) for an email ..."
    r"(?:(?:my\s+)?(?:gmail|e-?mail|inbox|mail|mailbox)\b"
    r"|(?:out\s+)?for\s+(?:an?\s+|any\s+|new\s+|the\s+)?(?:new\s+)?"
    r"(?:e-?mails?|mails?|messages?|gmails?)\b)"
)
_NOTIFY = re.compile(r"\b(?:tell\s+me|let\s+me\s+know|notify\s+me|alert\s+me|ping\s+me)\b")
_SHORT_FORM = re.compile(
    rf"^{_LEAD}(?:tell\s+me|let\s+me\s+know|notify\s+me|alert\s+me|ping\s+me)\s+"
    r"(?:when|if|as\s+soon\s+as|once|whenever)\b"
)
_MAIL_NOUN = re.compile(r"\b(?:gmail|e-?mails?|inbox|mails?|messages?|mailbox)\b")
_ARRIVAL = re.compile(
    r"\b(?:i\s+(?:get|receive)|arrives?|comes?\s+in|lands?|shows?\s+up"
    r"|there\s+is\s+an?\s+(?:new\s+)?(?:e-?mail|mail|message)"
    r"|there's\s+an?\s+(?:new\s+)?(?:e-?mail|mail|message))\b"
)

#: Anything that would act on mail. A monitor only reads.
_WRITE_VERB = re.compile(
    r"\b(?:reply|respond|forward|delete|archive|label|mark|move|trash|draft"
    r"|unsubscribe|star)\b|\bsend\b(?!\s+me\b)"
)

#: An absolute date or time. "For N days" is supported; "until Friday" is not.
_ABSOLUTE_DATE = re.compile(
    r"\b(?:until|till|til|tomorrow|tonight|weekend"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    # "may" and "march" are left out: as a verb each is far commoner in a
    # request than as a month, and "for 3 days" is the supported form anyway.
    r"|january|february|april|june|july|august|september|october|november"
    r"|december|jan|feb|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b"
    r"|\b\d{1,4}[/.-]\d{1,2}(?:[/.-]\d{1,4})?\b"
)

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
    "sixty": 60, "ninety": 90,
}
_NUMBER = r"(?:\d{1,4}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve" \
          r"|fifteen|twenty|thirty|forty|forty-five|sixty|ninety)"
_UNIT_SECONDS = {
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86_400, "days": 86_400,
    "week": 604_800, "weeks": 604_800,
}
_UNIT = r"(?:minutes?|mins?|hours?|hrs?|days?|weeks?)"

_EVERY_WORD = re.compile(r"\bevery\b|\bhourly\b|\bdaily\b")
_EVERY = re.compile(
    rf"\bevery\s+(?:(?P<n>{_NUMBER})\s+)?(?P<unit>{_UNIT})\b"
    r"|\bevery\s+half\s+(?:an\s+)?hour\b|\b(?P<adverb>hourly|daily)\b"
)
_LIFETIME = re.compile(
    rf"\bfor\s+(?:the\s+next\s+|the\s+|another\s+)?(?P<n>{_NUMBER})\s+(?P<unit>{_UNIT})\b"
)
#: "for 2 months", "for 30 seconds": a duration in a unit not supported.
_LIFETIME_OTHER = re.compile(
    rf"\bfor\s+(?:the\s+next\s+|the\s+)?{_NUMBER}\s+"
    r"(?:months?|years?|seconds?|secs?|fortnights?)\b"
)
_INTERVAL_OTHER = re.compile(r"\bevery\s+(?:\d+\s+)?(?:seconds?|secs?|months?|years?)\b")

_FROM = re.compile(r"\bfrom\s+(?P<sender>[^\s,;]+)(?P<rest>.*)$")
_SENDER_SHAPE = re.compile(r"^[a-z0-9.@_+\-]{2,96}$")
#: Words after "from" that are not a sender.
_NOT_A_SENDER = frozenset({"now", "today", "then", "here", "there", "time"})
#: Words after "from" that name nobody in particular.
_VAGUE_SENDER = frozenset({
    "my", "the", "a", "an", "any", "anyone", "anybody", "someone", "somebody",
    "everyone", "them", "him", "her", "it", "me", "us", "you", "gmail",
    "email", "e-mail", "inbox", "mail", "work", "home", "people",
})
#: Words that may follow a sender and continue the sentence.
_CONTINUES = frozenset({
    "and", "tell", "let", "notify", "alert", "ping", "about", "regarding",
    "with", "every", "for", "then", "when", "if", "once", "as", "subject",
    "titled", "called", "hourly", "daily", "please",
})
_SECOND_SENDER = re.compile(r"^\s*(?:,|/|&|\bor\b|\band\b)\s*(?P<next>[^\s,;]+)")

_ABOUT = re.compile(
    r"\b(?:about|regarding|titled|called"
    r"|with\s+(?:the\s+)?(?:subject|title)(?:\s+line)?|subject(?:\s+line)?)\s+"
    r"(?P<rest>.*)$"
)
_TERM = re.compile(r"^[a-z0-9.@_'\-]{1,64}$")
_TERM_STOPS = frozenset({
    "and", "then", "tell", "let", "notify", "alert", "ping", "every", "for",
    "from", "when", "if", "once", "as", "hourly", "daily", "please", "or",
    "in", "to", "arrives", "arrive", "comes", "lands", "shows",
})
_TERM_FILLER = frozenset({"the", "a", "an", "it", "this", "that", "my", "some", "any"})


def _amount(raw: Optional[str]) -> int:
    if raw is None:
        return 1
    return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 0)


def _interval_in(text: str) -> Tuple[Optional[int], Optional[str]]:
    """The interval asked for in seconds, the default, or a reason code."""
    if _INTERVAL_OTHER.search(text):
        return None, "invalid_interval"
    mentions = len(_EVERY_WORD.findall(text))
    matches = list(_EVERY.finditer(text))
    if mentions == 0:
        return DEFAULT_INTERVAL_SECONDS, None
    if len(matches) != 1 or mentions != 1:
        # "every" that does not parse, or two intervals at once.
        return None, "invalid_interval"
    match = matches[0]
    if match.group("adverb"):
        seconds = 3600 if match.group("adverb") == "hourly" else 86_400
    elif match.group("unit"):
        seconds = _amount(match.group("n")) * _UNIT_SECONDS[match.group("unit")]
    else:
        seconds = 1800                                    # every half hour
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        return None, "invalid_interval"
    return seconds, None


def _lifetime_in(text: str) -> Tuple[Optional[int], Optional[str]]:
    """The lifetime asked for in seconds, the default, or a reason code."""
    if _LIFETIME_OTHER.search(text):
        return None, "invalid_lifetime"
    matches = list(_LIFETIME.finditer(text))
    if not matches:
        return DEFAULT_LIFETIME_SECONDS, None
    if len(matches) != 1:
        return None, "invalid_lifetime"
    match = matches[0]
    seconds = _amount(match.group("n")) * _UNIT_SECONDS[match.group("unit")]
    if not MIN_LIFETIME_SECONDS <= seconds <= MAX_LIFETIME_SECONDS:
        return None, "invalid_lifetime"
    return seconds, None


def _sender_in(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Exactly one sender, or a reason code."""
    found: List[Tuple[str, str]] = []
    position = 0
    while True:
        match = _FROM.search(text, position)
        if match is None:
            break
        position = match.start() + 4
        token = match.group("sender").strip("\"'.,;:!?()")
        if token in _NOT_A_SENDER:
            continue
        found.append((token, match.group("rest")))
    if not found:
        return None, "missing_sender"
    if len(found) > 1:
        return None, "multiple_senders"
    token, rest = found[0]
    if token in _VAGUE_SENDER:
        return None, "missing_sender"
    if not _SENDER_SHAPE.match(token):
        return None, "invalid_sender"
    second = _SECOND_SENDER.match(rest)
    if second is not None:
        following = second.group("next").strip("\"'.,;:!?()")
        if following and following not in _CONTINUES:
            return None, "multiple_senders"
    return token[:MAX_SENDER_CHARS], None


def _subject_terms_in(text: str) -> Tuple[Optional[Tuple[str, ...]], Optional[str]]:
    """Up to three subject words, or a reason code.

    The words after "about", "regarding" or "with the subject", up to the
    first word that continues the sentence ("and tell me", "every hour") or a
    comma. A quoted subject is taken whole and must be plain words.
    """
    terms: List[str] = []
    for match in _ABOUT.finditer(text):
        rest = match.group("rest")
        quoted = re.match(r'^"(?P<inner>[^"]{1,200})"', rest)
        if quoted:
            for raw in quoted.group("inner").split():
                word = raw.strip(".,;:!?()'\"")
                if not word or word in _TERM_FILLER:
                    continue
                if not _TERM.match(word):
                    return None, "invalid_subject"
                terms.append(word)
            continue
        for raw in rest.split():
            ends_clause = raw[-1:] in ",;:"
            word = raw.strip(".,;:!?()'\"")
            if word in _TERM_STOPS:
                break
            if word and word not in _TERM_FILLER:
                if not _TERM.match(word):
                    break
                terms.append(word)
            if ends_clause:
                break
    unique = tuple(dict.fromkeys(terms))
    if len(unique) > MAX_SUBJECT_TERMS:
        return None, "too_many_terms"
    return unique, None


def recognise(message: str) -> Recognition:
    """Whether this user message asks for Gmail monitoring, and what exactly.

    Pure and deterministic: no model, no clock, no database. Reads the raw
    user message only -- its one caller passes the user's own text, and a
    structural test asserts nothing else reaches it.
    """
    text = _normalise(message)
    if not text or len(text) > MAX_REQUEST_CHARS:
        return Recognition(RecognitionKind.NOT_MONITOR)
    text = text.strip(" .!?")

    triggered = bool(_TRIGGER_FORM.match(text))
    short = (
        bool(_SHORT_FORM.match(text))
        and bool(_MAIL_NOUN.search(text))
        and bool(_ARRIVAL.search(text))
    )
    if (triggered or short) and _WRITE_VERB.search(text):
        # "watch my inbox for X and forward it": refused outright, whether
        # or not it also asks to be told.
        return Recognition(RecognitionKind.REFUSED, reason="write_verb")
    if not ((triggered and _NOTIFY.search(text)) or short):
        return Recognition(RecognitionKind.NOT_MONITOR)

    if _ABSOLUTE_DATE.search(text):
        return Recognition(RecognitionKind.CLARIFY, reason="absolute_date")

    sender, problem = _sender_in(text)
    if problem:
        return Recognition(RecognitionKind.CLARIFY, reason=problem)
    terms, problem = _subject_terms_in(text)
    if problem:
        return Recognition(RecognitionKind.CLARIFY, reason=problem)
    interval, problem = _interval_in(text)
    if problem:
        return Recognition(RecognitionKind.CLARIFY, reason=problem)
    lifetime, problem = _lifetime_in(text)
    if problem:
        return Recognition(RecognitionKind.CLARIFY, reason=problem)

    return Recognition(
        RecognitionKind.REQUEST,
        request=MonitorRequest(
            sender=sender,
            subject_terms=terms,
            interval_seconds=interval,
            lifetime_seconds=lifetime,
        ),
    )


# --- Recognition: the reply to a proposal --------------------------------------


class ReplyClass(str, enum.Enum):
    CONFIRM = "confirm"
    DECLINE = "decline"
    REPLACE = "replace"
    INVALIDATED = "invalidated"
    OTHER = "other"


_AFFIRM = frozenset({"yes", "yeah", "yep", "ok", "okay", "sure", "confirm", "confirmed"})
_ACTIONS = (
    ("do", "it"), ("go", "ahead"), ("start", "it"), ("start", "monitoring"),
    ("set", "it", "up"),
)
_POLITE = (("please",), ("thanks",), ("thank", "you"))
_DECLINES = (
    ("no",), ("nope",), ("cancel",), ("stop",), ("don't",), ("do", "not"),
    ("never", "mind"), ("nevermind",), ("no", "thanks"), ("no", "thank", "you"),
)
#: Words that change what is monitored, how often or for how long.
_SPEC_CHANGE = frozenset({
    "but", "except", "instead", "actually", "change", "changed", "make",
    "every", "each", "hourly", "daily", "minute", "minutes", "min", "mins",
    "hour", "hours", "day", "days", "week", "weeks", "from", "about",
    "regarding", "subject", "sender", "until", "longer", "shorter", "sooner",
    "later",
})


def _reply_tokens(message: str, alphabet: str) -> Optional[List[str]]:
    text = _normalise(message)
    text = re.sub(r"[,;:]", " ", text)
    text = re.sub(r"[.!]+$", "", text.strip()).strip()
    text = " ".join(text.split())
    if not text or len(text) > MAX_REPLY_CHARS or re.search(alphabet, text):
        return None
    return text.split()


def _consume(tokens: List[str], phrases) -> Tuple[List[str], bool]:
    for phrase in phrases:
        if tuple(tokens[: len(phrase)]) == phrase:
            return tokens[len(phrase):], True
    return tokens, False


def _is_confirmation(message: str) -> bool:
    tokens = _reply_tokens(message, r"[^a-z ]")
    if tokens is None:
        return False
    tokens, _ = _consume(tokens, (("please",),))
    affirmed = bool(tokens) and tokens[0] in _AFFIRM
    if affirmed:
        tokens = tokens[1:]
    tokens, acted = _consume(tokens, _ACTIONS)
    tokens, _ = _consume(tokens, _POLITE)
    return not tokens and (affirmed or acted)


def _is_decline(message: str) -> bool:
    tokens = _reply_tokens(message, r"[^a-z' ]")
    if tokens is None:
        return False
    tokens, declined = _consume(tokens, sorted(_DECLINES, key=len, reverse=True))
    tokens, _ = _consume(tokens, _POLITE)
    return declined and not tokens


def _starts_affirmatively(message: str) -> bool:
    words = re.findall(r"[a-z']+", _normalise(message))
    if not words:
        return False
    if words[0] in _AFFIRM:
        return True
    return any(tuple(words[: len(action)]) == action for action in _ACTIONS)


def classify_reply(message: str) -> ReplyClass:
    """How a message answers a pending proposal. Deterministic; no model.

    Only an exact, plain confirmation confirms. Anything that adds to it --
    "yes, every 30 minutes", "yes but change it" -- or talks about the
    specification without confirming it is not a confirmation, and the
    proposal is withdrawn rather than adjusted.
    """
    if _is_confirmation(message):
        return ReplyClass.CONFIRM
    if _is_decline(message):
        return ReplyClass.DECLINE
    if recognise(message).kind is not RecognitionKind.NOT_MONITOR:
        return ReplyClass.REPLACE
    raw = _normalise(message)
    words = set(re.findall(r"[a-z']+", raw))
    if (
        _starts_affirmatively(message)
        or words & _SPEC_CHANGE
        or re.search(r"\d", raw)
    ):
        return ReplyClass.INVALIDATED
    return ReplyClass.OTHER


# --- Results -----------------------------------------------------------------


class MonitorOutcome(str, enum.Enum):
    NOT_MONITOR = "not_monitor"
    NEEDS_CLARIFICATION = "needs_clarification"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    PROPOSED = "proposed"
    ALREADY_MONITORING = "already_monitoring"
    DECLINED = "declined"
    INVALIDATED = "invalidated"
    ABANDONED = "abandoned"
    EXPIRED = "expired"
    #: Stage 6P. The proposal was answered by a concurrent turn first.
    NOT_PENDING = "not_pending"
    SCHEDULED = "scheduled"
    FAILED = "failed"


class MonitorResult(BaseModel):
    """What the monitor step did with one turn. Application state."""

    model_config = ConfigDict(frozen=True)

    outcome: MonitorOutcome = MonitorOutcome.NOT_MONITOR
    #: Application-written. Empty when the turn continues to the next layer.
    reply: str = ""
    #: The proposal or the monitor this turn concerned, if any.
    task_id: Optional[uuid.UUID] = None

    @property
    def has_reply(self) -> bool:
        return bool(self.reply)


_NOT_SET_UP = "I haven't set anything up."

_CLARIFICATIONS = {
    "missing_sender": (
        "I can watch your Gmail for a message from one sender. Who should it be "
        "from? For example: “watch my Gmail for an email from apple.com and "
        "tell me when it arrives.”"
    ),
    "multiple_senders": (
        "I can watch for one sender at a time. Which sender should I watch for?"
    ),
    "invalid_sender": (
        "I couldn't read that sender. Give me a name, an email address or a "
        "domain, like apple.com."
    ),
    "invalid_subject": (
        "I couldn't read those subject words. Use plain words, at most three."
    ),
    "too_many_terms": (
        "I can match at most three subject words. Which ones matter most?"
    ),
    "invalid_interval": (
        "I can check every 5 minutes at the most and once a day at the least. "
        "How often should I check?"
    ),
    "invalid_lifetime": (
        "I can monitor for between 1 hour and 7 days. How long should I watch "
        "for?"
    ),
    "absolute_date": (
        "I can't monitor until a particular date or day. Tell me how long "
        "instead, for example “for 3 days” (up to 7 days)."
    ),
}

_REFUSAL = (
    "I can only watch your Gmail and tell you when a message arrives. I can't "
    "reply to, forward, delete, label or otherwise change mail."
)

_UNAVAILABLE = (
    "Gmail monitoring can only be set up in the Mai app, not from here. "
    + _NOT_SET_UP
)


def _clarification(reason: Optional[str]) -> str:
    return f"{_CLARIFICATIONS.get(reason or '', _CLARIFICATIONS['missing_sender'])} {_NOT_SET_UP}"


# --- Rendering ---------------------------------------------------------------


def _zone(settings: Settings):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(str(getattr(settings, "MAI_TIMEZONE", "UTC") or "UTC"))
    except Exception:  # noqa: BLE001 - a bad zone falls back to UTC, stated
        return timezone.utc


def _when(moment: datetime, settings: Settings) -> str:
    zone = _zone(settings)
    local = _utc(moment).astimezone(zone)
    name = getattr(zone, "key", "UTC")
    return f"{local:%a} {local.day} {local:%b} {local:%Y}, {local:%H:%M} ({name})"


def _every(seconds: int) -> str:
    if seconds % 86_400 == 0:
        days = seconds // 86_400
        return "day" if days == 1 else f"{days} days"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "hour" if hours == 1 else f"{hours} hours"
    return f"{seconds // 60} minutes"


def _what(sender: str, terms: Tuple[str, ...]) -> str:
    about = ""
    if terms:
        about = " with " + ", ".join(f"“{t}”" for t in terms) + " in the subject"
    return f"a Gmail message from {sender}{about}"


def _channels(settings: Settings) -> str:
    """Where a notification will go, from what is actually configured."""
    try:
        from app.background.deliveries import usable_channels

        names = usable_channels(settings)
    except Exception:  # noqa: BLE001 - unknown means not promised
        names = []
    if not names:
        return "in Mai"
    readable = ", ".join(name.capitalize() for name in names)
    return f"in Mai, and it will be sent to {readable}"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --- The service -------------------------------------------------------------


class _Rollback(Exception):
    """Unwinds a savepoint. Carries an application reason code only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MonitorRequestService:
    """The one door from a chat turn into the task layer.

    Owner-bound, like `TaskService`: every query filters on the owner it was
    constructed with, and no method accepts one. An owner of None -- the
    Telegram webhook -- can be told what monitoring is, never given one.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        owner_id: Optional[uuid.UUID] = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._owner_id = owner_id
        self._tasks = (
            TaskService(session, settings=self._settings, owner_id=owner_id)
            if owner_id is not None else None
        )

    # --- One turn --------------------------------------------------------

    async def handle(self, conversation_id: uuid.UUID, message: str) -> MonitorResult:
        """Decide what this user message means for monitoring. Never raises.

        Takes the conversation and the user's own message, and nothing else:
        no model output, no retrieved text and no tool result can reach the
        grammar through this signature.
        """
        try:
            return await self._handle(conversation_id, message)
        except Exception as exc:  # noqa: BLE001 - a turn must not 500 here
            logger.error(
                "Monitor request handling failed",
                extra={"conversation_id": str(conversation_id), "error": type(exc).__name__},
            )
            return MonitorResult(
                outcome=MonitorOutcome.FAILED,
                reply=f"Monitoring was not set up. {_NOT_SET_UP}",
            )

    async def _handle(self, conversation_id, message) -> MonitorResult:
        if self._tasks is None:
            # No authenticated owner: recognised, explained, never persisted.
            if recognise(message).kind is RecognitionKind.NOT_MONITOR:
                return MonitorResult()
            return MonitorResult(outcome=MonitorOutcome.UNAVAILABLE, reply=_UNAVAILABLE)

        pending, expired, superseded = await self._pending_for(conversation_id)
        for task in expired:
            await self._discard(task, "proposal_expired")
        for task in superseded:
            await self._discard(task, "proposal_replaced")

        if pending is not None:
            answer = classify_reply(message)
            if answer is ReplyClass.CONFIRM:
                return await self._confirm(pending)
            if answer is ReplyClass.DECLINE:
                if not await self._discard(pending, "proposal_declined"):
                    # Stage 6P. A concurrent turn answered it first.
                    return await self._no_longer_pending(pending.id)
                return MonitorResult(
                    outcome=MonitorOutcome.DECLINED, task_id=pending.id,
                    reply="Okay, I won't monitor that. Nothing was set up.",
                )
            if answer is ReplyClass.REPLACE:
                withdrawn = await self._discard(pending, "proposal_replaced")
                earlier = (
                    "I've cancelled the earlier proposal. " if withdrawn
                    # Stage 6P. Not withdrawn: a concurrent "yes" activated
                    # it first, and it stays active. Say so, and never claim
                    # a cancellation that did not happen.
                    else await self._earlier_status(pending.id)
                )
                replaced = await self._from_request(conversation_id, message)
                if not replaced.has_reply:  # pragma: no cover - REPLACE means recognised
                    return replaced
                return replaced.model_copy(update={"reply": f"{earlier}{replaced.reply}"})
            if answer is ReplyClass.INVALIDATED:
                if not await self._discard(pending, "proposal_invalidated"):
                    return await self._no_longer_pending(pending.id)
                return MonitorResult(
                    outcome=MonitorOutcome.INVALIDATED, task_id=pending.id,
                    reply=(
                        "I didn't start monitoring. Your reply didn't simply "
                        "confirm the proposal, so I've cancelled it. If you "
                        "want monitoring, send the full request again."
                    ),
                )
            await self._discard(pending, "proposal_abandoned")
            return MonitorResult(outcome=MonitorOutcome.ABANDONED, task_id=pending.id)

        if expired and classify_reply(message) is ReplyClass.CONFIRM:
            return MonitorResult(
                outcome=MonitorOutcome.EXPIRED, task_id=expired[0].id,
                reply=(
                    "That proposal expired before you confirmed it, so nothing "
                    "was started. Send the request again if you still want it."
                ),
            )

        return await self._from_request(conversation_id, message)

    async def _from_request(self, conversation_id, message) -> MonitorResult:
        recognition = recognise(message)
        if recognition.kind is RecognitionKind.NOT_MONITOR:
            return MonitorResult()
        if recognition.kind is RecognitionKind.REFUSED:
            return MonitorResult(
                outcome=MonitorOutcome.REFUSED, reply=f"{_REFUSAL} {_NOT_SET_UP}"
            )
        if recognition.kind is RecognitionKind.CLARIFY:
            return MonitorResult(
                outcome=MonitorOutcome.NEEDS_CLARIFICATION,
                reply=_clarification(recognition.reason),
            )
        request = recognition.request
        existing = await self._active_duplicate(request)
        if existing is not None:
            return MonitorResult(
                outcome=MonitorOutcome.ALREADY_MONITORING, task_id=existing,
                reply=(
                    f"I'm already monitoring for {_what(request.sender, request.subject_terms)}. "
                    "Nothing new was set up."
                ),
            )
        return await self._propose(conversation_id, request)

    # --- Proposing ---------------------------------------------------------

    async def _propose(self, conversation_id, request: MonitorRequest) -> MonitorResult:
        """Write the proposal: a planned, configured, unauthorized task.

        Calls nothing that authorizes, grants or schedules. The row this
        writes is inert until a person confirms it.
        """
        from app.planning.schemas import Goal, IntentType, Plan, PlanTask
        from app.tools.catalog import GmailListMessagesArguments

        now = datetime.now(timezone.utc).replace(microsecond=0)
        lifetime = min(request.lifetime_seconds, MAX_MONITOR_CHECKS * request.interval_seconds)
        deadline = now + timedelta(seconds=lifetime)
        arguments = GmailListMessagesArguments(
            sender=request.sender,
            subject_terms=request.subject_terms,
            text_terms=(),
            unread_only=False,
            newer_than_days=None,
            max_results=1,
            body_count=0,
            received_after=int(now.timestamp()),
        ).model_dump(mode="json")
        plan = Plan(
            goal=Goal(
                summary="Tell the owner when a matching Gmail message arrives",
                source_intent=IntentType.ACTION,
            ),
            tasks=[PlanTask(
                id="check", title="Check Gmail for a matching message",
                order=1, depth=0, dependencies=[],
                capability=MONITOR_CAPABILITY, arguments=arguments,
            )],
        )
        spec = {
            "condition": {
                "kind": "value", "path": "external.content",
                "operator": "ne", "expected": NO_MESSAGES_MATCHED,
            },
            "interval_seconds": request.interval_seconds,
        }
        objective = f"Watch Gmail for {_what(request.sender, request.subject_terms)}"

        try:
            async with self._session.begin_nested():
                created = await self._tasks.create_for_user(
                    objective, conversation_id=conversation_id, deadline=deadline,
                )
                if not created.ok:
                    raise _Rollback(created.reason or "create_failed")
                attached = await self._tasks.attach_plan(created.task_id, plan)
                if not attached.ok:
                    raise _Rollback(attached.reason or "plan_refused")
                configured = await self._tasks.configure_monitoring(created.task_id, spec)
                if not configured.ok:
                    raise _Rollback(configured.reason or "monitor_refused")
        except (_Rollback, *_DB_ERRORS) as refusal:
            logger.info(
                "Monitor proposal not written",
                extra={"reason": getattr(refusal, "reason", type(refusal).__name__)},
            )
            return MonitorResult(
                outcome=MonitorOutcome.FAILED,
                reply=f"I couldn't set that up. {_NOT_SET_UP}",
            )

        logger.info(
            "Monitor proposed",
            extra={
                "task_id": str(created.task_id),
                "interval_seconds": request.interval_seconds,
                "lifetime_seconds": lifetime,
                "subject_terms": len(request.subject_terms),
            },
        )
        return MonitorResult(
            outcome=MonitorOutcome.PROPOSED, task_id=created.task_id,
            reply=self._proposal_text(request, now, deadline, lifetime),
        )

    def _proposal_text(self, request, baseline, deadline, lifetime) -> str:
        shortened = lifetime < request.lifetime_seconds
        until = f"until {_when(deadline, self._settings)}"
        if shortened:
            until += (
                f" (a monitor makes at most {MAX_MONITOR_CHECKS} checks, so at "
                f"every {_every(request.interval_seconds)} it ends then; check "
                "less often to watch for longer)"
            )
        return "\n".join([
            "I can set this up. Nothing is running yet. Here's what I'd monitor:",
            f"- Watching for: {_what(request.sender, request.subject_terms)}.",
            f"- Only mail received after {_when(baseline, self._settings)} counts.",
            f"- Checked every {_every(request.interval_seconds)}, {until}.",
            "- Gmail metadata only (sender, subject, date). I won't read message "
            "bodies, and I won't change anything in your mailbox.",
            f"- When a matching message arrives, you'll get a notification {_channels(self._settings)}.",
            "Confirming gives Mai standing permission to list your Gmail message "
            "metadata without asking each time, until the monitor ends. Gmail "
            "access is treated as high-risk.",
            "Reply yes to start, or no to cancel.",
        ])

    # --- Confirming --------------------------------------------------------

    async def _confirm(self, pending: Task) -> MonitorResult:
        """Authorize, approve, grant and schedule the one pending row. Atomic.

        The row is claimed conditionally first -- still planned, still
        unauthorized -- so two confirmations of one proposal cannot both
        succeed: the loser's claim matches nothing.
        """
        from app.authorization.grants import GrantRefused, GrantService
        from app.authorization.models import MAX_GRANT_TTL_SECONDS
        from app.tools.registry import get_registry

        task_id = pending.id
        request = self._request_of(pending)
        now = datetime.now(timezone.utc)
        deadline = _utc(pending.deadline)
        ttl = math.ceil((deadline - now).total_seconds())
        definition = get_registry().definition(MONITOR_CAPABILITY)

        claimed = False
        try:
            async with self._session.begin_nested():
                claim = await self._session.execute(
                    update(Task)
                    .where(
                        Task.id == task_id,
                        Task.owner_id == self._owner_id,
                        Task.state == TaskState.PLANNED,
                        Task.authorized_at.is_(None),
                    )
                    .values(updated_at=now)
                    .execution_options(synchronize_session="fetch")
                )
                if claim.rowcount != 1:
                    raise _Rollback("already_answered")
                claimed = True
                if definition is None or not 1 <= ttl <= MAX_GRANT_TTL_SECONDS:
                    raise _Rollback("invalid_lifetime")

                authorized = await self._tasks.authorize_plan(task_id)
                if not authorized.ok:
                    raise _Rollback(authorized.reason or "authorization_refused")
                if authorized.state is TaskState.AWAITING_APPROVAL:
                    # The person's approval: this confirmation.
                    approved = await self._tasks.transition(
                        task_id, TaskState.QUEUED, actor="user", reason="monitor_confirmed",
                    )
                    if not approved.ok:
                        raise _Rollback(approved.reason or "approval_refused")
                await GrantService(self._session, owner_id=self._owner_id).create(
                    MONITOR_CAPABILITY, definition.risk_level, ttl_seconds=ttl, now=now,
                )
                scheduled = await self._tasks.schedule_background(task_id, now=now)
                if not scheduled.ok:
                    raise _Rollback(scheduled.reason or "scheduling_refused")
        except (_Rollback, GrantRefused, *_DB_ERRORS) as refusal:
            reason = getattr(refusal, "reason", None) or type(refusal).__name__
            logger.info(
                "Monitor confirmation rolled back",
                extra={"task_id": str(task_id), "reason": str(reason)[:64]},
            )
            if not claimed:
                # Someone else answered this proposal first. Nothing here
                # owns it, so nothing here may cancel it -- and nothing here
                # may say it started. Stage 6P: say what actually happened.
                return await self._no_longer_pending(task_id)
            await self._discard_by_id(task_id, "proposal_failed")
            return MonitorResult(
                outcome=MonitorOutcome.FAILED, task_id=task_id,
                reply=f"Monitoring was not set up. {_NOT_SET_UP}",
            )

        logger.info("Monitor confirmed", extra={"task_id": str(task_id), "ttl_seconds": ttl})
        return MonitorResult(
            outcome=MonitorOutcome.SCHEDULED, task_id=task_id,
            reply=self._scheduled_text(pending, request, deadline),
        )

    def _scheduled_text(self, pending, request, deadline) -> str:
        """The "Done" sentence. Only ever built after the savepoint held.

        Built defensively: what it describes has already happened, so a
        formatting problem must not turn it into a reply saying otherwise.
        """
        try:
            what = _what(request.sender, request.subject_terms)
            baseline = self._baseline_of(pending)
            since = f", received after {_when(baseline, self._settings)}" if baseline else ""
            return (
                f"Done. I'm monitoring for {what}{since}. I'll check every "
                f"{_every(request.interval_seconds)} until "
                f"{_when(deadline, self._settings)}, and you'll get a "
                f"notification {_channels(self._settings)} if one arrives."
            )
        except Exception:  # noqa: BLE001 - see the docstring
            return "Done. I'm monitoring your Gmail as proposed."

    # --- Cancelling ----------------------------------------------------------

    async def cancel(self, task_id: uuid.UUID) -> TaskResult:
        """Cancel one of the owner's tasks, through `TaskService.cancel`.

        Idempotent for a task already cancelled. A monitor's grant is released
        with it, unless another active task still needs the capability.
        """
        if self._tasks is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")
        # Locked and read fresh, before anything is decided. A monitoring
        # check holds this row from its claim until it commits, so a cancel
        # arriving while Gmail is being read waits for the check to finish
        # and then sees what it did: a monitor that already matched is
        # completed, and the answer is "terminal" -- never a cancellation
        # written over a completion whose notification already exists.
        # (SQLite has no row locks and needs none: it has one writer.)
        task = (
            await self._session.execute(
                select(Task)
                .where(Task.id == task_id, Task.owner_id == self._owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
        if task is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")
        if task.state is TaskState.CANCELLED:
            return TaskResult(
                outcome=TaskOutcome.CANCELLED, task_id=task.id, state=task.state,
            )
        was_monitor = task.monitor is not None
        result = await self._tasks.cancel(task_id, reason="cancelled_by_owner")
        if result.ok and was_monitor:
            await release_monitor_grants(self._session, self._owner_id)
        return result

    # --- Reading -----------------------------------------------------------

    async def _pending_for(
        self, conversation_id
    ) -> Tuple[Optional[Task], List[Task], List[Task]]:
        """This conversation's live proposal, the proposals that expired, and
        any older live proposal a newer one superseded."""
        rows = (
            await self._session.execute(
                select(Task)
                .where(
                    Task.owner_id == self._owner_id,
                    Task.conversation_id == conversation_id,
                    Task.state == TaskState.PLANNED,
                    Task.authorized_at.is_(None),
                )
                .order_by(Task.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        proposals = [row for row in rows if row.monitor is not None]
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=PROPOSAL_TTL_SECONDS)
        live = [row for row in proposals if _utc(row.created_at) >= cutoff]
        expired = [row for row in proposals if _utc(row.created_at) < cutoff]
        # One pending proposal per conversation: anything older than the
        # newest live one was superseded by it.
        return (live[0] if live else None), expired, live[1:]

    async def _active_duplicate(self, request: MonitorRequest) -> Optional[uuid.UUID]:
        """An active monitor of this owner watching for exactly this."""
        rows = (
            await self._session.execute(
                select(Task.id, TaskStep.arguments)
                .join(TaskStep, TaskStep.task_id == Task.id)
                .where(
                    Task.owner_id == self._owner_id,
                    Task.state.in_([TaskState.QUEUED, TaskState.RUNNING]),
                    TaskStep.capability == MONITOR_CAPABILITY,
                )
                .limit(50)
            )
        ).all()
        for task_id, arguments in rows:
            arguments = arguments or {}
            if (
                arguments.get("sender") == request.sender
                and tuple(arguments.get("subject_terms") or ()) == request.subject_terms
            ):
                return task_id
        return None

    @staticmethod
    def _arguments_of(task: Task) -> dict:
        for entry in (task.plan or {}).get("tasks") or []:
            if isinstance(entry, dict) and isinstance(entry.get("arguments"), dict):
                return entry["arguments"]
        return {}

    def _request_of(self, task: Task) -> Optional[MonitorRequest]:
        arguments = self._arguments_of(task)
        interval = (task.monitor or {}).get("interval_seconds")
        if not arguments or not isinstance(interval, int):
            return None
        return MonitorRequest(
            sender=str(arguments.get("sender") or ""),
            subject_terms=tuple(arguments.get("subject_terms") or ()),
            interval_seconds=interval,
            lifetime_seconds=0,
        )

    def _baseline_of(self, task: Task) -> Optional[datetime]:
        value = self._arguments_of(task).get("received_after")
        if isinstance(value, int) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return None

    # --- Withdrawing ---------------------------------------------------------

    async def _discard(self, task: Task, reason: str) -> bool:
        return await self._discard_by_id(task.id, reason)

    async def _discard_by_id(self, task_id: uuid.UUID, reason: str) -> bool:
        """Cancel a proposal. It never had a grant, so there is none to release.

        Stage 6P. Only a proposal still planned and unauthorised is withdrawn:
        the cancellation is `TaskService.transition`'s compare-and-set, so a
        proposal a concurrent confirmation has just activated is left active,
        and False says so.
        """
        task = await self._tasks.refresh(task_id)
        if task is None or task.state is not TaskState.PLANNED or task.authorized_at is not None:
            return False
        result = await self._tasks.cancel(task_id, reason=reason)
        logger.info(
            "Monitor proposal withdrawn" if result.ok else "Monitor proposal not withdrawn",
            extra={"task_id": str(task_id), "reason": reason, "outcome": result.outcome.value},
        )
        return result.ok

    async def _no_longer_pending(self, task_id: uuid.UUID) -> MonitorResult:
        """Stage 6P. The truth about a proposal this turn did not get to answer."""
        task = await self._tasks.refresh(task_id)
        if task is not None and task.state in (TaskState.QUEUED, TaskState.RUNNING):
            return MonitorResult(
                outcome=MonitorOutcome.ALREADY_MONITORING, task_id=task_id,
                reply=(
                    "That proposal had already been confirmed, so that monitor "
                    "is active and nothing was changed. To stop it, cancel the task."
                ),
            )
        return MonitorResult(
            outcome=MonitorOutcome.NOT_PENDING, task_id=task_id,
            reply=(
                "That proposal is no longer waiting for an answer -- it was "
                "replaced, cancelled or has ended -- so nothing was started or changed."
            ),
        )

    async def _earlier_status(self, task_id: uuid.UUID) -> str:
        task = await self._tasks.refresh(task_id)
        if task is not None and task.state in (TaskState.QUEUED, TaskState.RUNNING):
            return "Your earlier proposal had just been confirmed, so that monitor is active. "
        return ""


async def release_monitor_grants(
    session: AsyncSession, owner_id: uuid.UUID, now: Optional[datetime] = None
) -> int:
    """Revoke the owner's live grants for the monitor capability, unless an
    active task still needs it. Returns how many were revoked.

    Called when a monitor ends -- cancelled by its owner, or expired -- so an
    ended monitor leaves no permission behind. Revocation only ever narrows
    what may run.
    """
    from app.authorization.grants import GrantService

    still_needed = (
        await session.execute(
            select(TaskStep.id)
            .join(Task, Task.id == TaskStep.task_id)
            .where(
                Task.owner_id == owner_id,
                Task.state.in_([TaskState.QUEUED, TaskState.RUNNING]),
                TaskStep.capability == MONITOR_CAPABILITY,
            )
            .limit(1)
        )
    ).first()
    if still_needed is not None:
        return 0
    grants = GrantService(session, owner_id=owner_id)
    revoked = 0
    for _ in range(MAX_RELEASED_GRANTS):
        grant = await grants.active_for(MONITOR_CAPABILITY, now=now)
        if grant is None:
            break
        if await grants.revoke(grant.id, reason="monitor_ended", now=now) is None:
            break
        revoked += 1
    if revoked:
        logger.info("Monitor grants released", extra={"revoked": revoked})
    return revoked


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_LIFETIME_SECONDS",
    "MAX_MONITOR_CHECKS",
    "MONITOR_CAPABILITY",
    "PROPOSAL_TTL_SECONDS",
    "MonitorOutcome",
    "MonitorRequest",
    "MonitorRequestService",
    "MonitorResult",
    "Recognition",
    "RecognitionKind",
    "ReplyClass",
    "classify_reply",
    "recognise",
    "release_monitor_grants",
]
