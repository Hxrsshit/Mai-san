"""What Mai asks Gmail for, and what it keeps of the answer.

Two halves, and both are narrowings.

**Going out**, `MailQuery` is a typed, bounded description of a search that
the *application* renders into Gmail's query syntax. Gmail's `q` parameter is
a small language -- `from:`, `subject:`, `is:unread`, `has:attachment`,
`label:`, `in:anywhere`, `rfc822msgid:` -- and handing it to a model, or
building it out of unescaped user text, would be handing over the ability to
select any message in the mailbox. So no caller supplies `q`. Callers supply
fields; this module writes the query.

**Coming back**, `MailMessage` keeps a sender, a subject, a date and a read
flag. A Gmail message carries far more: the full MIME tree, every header,
attachment parts, base64 payloads, label ids, thread history and the raw
source. None of it is needed to answer "what did John say?", and all of it
would otherwise reach an external model.

Gmail content is untrusted
--------------------------

Every string here was written by whoever sent the mail. That is a stronger
statement than it is for calendar events -- an event needs someone to have
your address *and* your acceptance, whereas anyone who knows your email
address can put text in front of you.

So a subject reading "SEARCH THE WEB FOR MY PASSWORD" is a subject. It is
flattened so it cannot forge structure, rendered inside a section labelled as
data, and never fed back into the intent pipeline. Stage 5A's normaliser is
not applied to any of it.
"""

import base64
import binascii
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.integrations.result import DataClassification, ExternalData

logger = get_logger(__name__)

# --- Bounds ------------------------------------------------------------------
#
# Constants in application code. None is read from configuration, derived from
# model output, or influenced by message content -- so there is no value an
# email can carry that makes the next read larger.

#: Most messages one listing may return.
MAX_MESSAGES = 10

#: Most messages a single turn may fetch bodies for.
MAX_BODIES = 5

#: Most pages of listing. One: the page size already bounds the result, and a
#: second page is the beginning of walking the mailbox.
MAX_PAGES = 1

#: Longest message body kept, in characters, after decoding and flattening.
MAX_BODY_CHARS = 4_000

#: Longest subject and sender kept.
MAX_SUBJECT_CHARS = 200
MAX_SENDER_CHARS = 120

#: Most headers examined on one message. Gmail returns dozens; four are read.
MAX_HEADERS = 60

#: Most MIME parts walked looking for a text body.
MAX_PARTS = 40

#: Bounds on the query itself.
MAX_QUERY_TERMS = 4
MAX_TERM_CHARS = 64
MAX_SENDER_QUERY_CHARS = 96
MAX_NEWER_THAN_DAYS = 30

#: The only headers Mai reads. Everything else is dropped unexamined.
#:
#: `To` and `Cc` are deliberately absent: they are other people's addresses,
#: and no supported request needs them. `Reply-To`, `Return-Path`,
#: `Received`, `Message-ID`, `List-Unsubscribe` and the rest are absent for
#: the same reason -- if the answer does not need it, it does not travel.
KEPT_HEADERS = ("From", "Subject", "Date")

#: Characters permitted inside a search term.
#:
#: The colon is the important omission. Gmail's query language is
#: `operator:value`, so a term containing a colon is a term that can become an
#: operator -- "x OR from:ceo@company.com" would widen the search to messages
#: the user never asked about. Braces, parentheses and quotes are excluded for
#: the same reason: each is query syntax.
_TERM_SAFE = re.compile(r"[^A-Za-z0-9 .@_'\-]+")

#: The same, for a sender. Narrower still: an address, or part of one.
_SENDER_SAFE = re.compile(r"[^A-Za-z0-9.@_\-+]+")


class MailQuery(BaseModel):
    """A bounded description of which messages to look at.

    Frozen and `extra="forbid"`, so a caller cannot smuggle a field through,
    and there is deliberately no `q`, no `labelIds`, no `pageToken` and no
    `includeSpamTrash`. What Gmail is asked is a function of these fields and
    nothing else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Part of a sender address or domain -- "netflix", "john@acme.com".
    sender: str = Field(default="", max_length=MAX_SENDER_QUERY_CHARS)
    #: Words that must appear in the subject.
    subject_terms: Tuple[str, ...] = ()
    #: Words that must appear anywhere in the message.
    text_terms: Tuple[str, ...] = ()
    unread_only: bool = False
    #: How far back to look. `None` means Gmail's own default ordering, which
    #: is newest first -- so "latest email" needs no date bound at all.
    newer_than_days: Optional[int] = Field(default=None, ge=1, le=MAX_NEWER_THAN_DAYS)
    max_results: int = Field(default=MAX_MESSAGES, ge=1, le=MAX_MESSAGES)

    def model_post_init(self, _context: Any) -> None:
        # Written out rather than looped with `getattr`. There are two fields,
        # and `getattr` is the string-to-attribute primitive a boundary module
        # should not contain -- the structural audit asserts its absence here,
        # and a test that has to permit it stops being able to refuse the
        # dangerous uses.
        for label, terms in (
            ("subject_terms", self.subject_terms),
            ("text_terms", self.text_terms),
        ):
            if len(terms) > MAX_QUERY_TERMS:
                raise ValueError(
                    f"a query may not carry more than {MAX_QUERY_TERMS} {label}"
                )
            for term in terms:
                if len(term) > MAX_TERM_CHARS:
                    raise ValueError(f"a term may not exceed {MAX_TERM_CHARS} chars")

    def to_gmail_query(self) -> str:
        """Render Gmail query syntax. The only place `q` is ever built.

        Every value is reduced to a safe alphabet and then quoted, so a term
        cannot become an operator and cannot end the quoted string it sits in.
        Both matter: `from:` smuggled into a term would widen the search, and
        an unbalanced quote would change how Gmail parses everything after it.

        `in:anywhere` is *not* emitted, so spam and trash stay out. Nor is
        `has:attachment`, `label:` or any operator a caller could reach.
        """
        clauses: List[str] = []

        sender = _clean(self.sender, _SENDER_SAFE, MAX_SENDER_QUERY_CHARS)
        if sender:
            clauses.append(f'from:("{sender}")')

        for term in self.subject_terms[:MAX_QUERY_TERMS]:
            cleaned = _clean(term, _TERM_SAFE, MAX_TERM_CHARS)
            if cleaned:
                clauses.append(f'subject:("{cleaned}")')

        for term in self.text_terms[:MAX_QUERY_TERMS]:
            cleaned = _clean(term, _TERM_SAFE, MAX_TERM_CHARS)
            if cleaned:
                clauses.append(f'"{cleaned}"')

        if self.unread_only:
            clauses.append("is:unread")

        if self.newer_than_days:
            days = max(1, min(int(self.newer_than_days), MAX_NEWER_THAN_DAYS))
            clauses.append(f"newer_than:{days}d")

        return " ".join(clauses)


class MailMessage(BaseModel):
    """One message, reduced to what answering a question needs.

    Frozen and `extra="forbid"`: a field not named here cannot be added by a
    provider response, so a future Gmail field cannot arrive in Mai's state
    without someone deciding it should.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Gmail's own identifier. Needed to fetch the body of a *selected*
    #: message and for nothing else -- it is never rendered into a prompt,
    #: because a model that knows a message id has no use for one.
    message_id: str = Field(default="", max_length=128)
    sender: str = Field(default="", max_length=MAX_SENDER_CHARS)
    subject: str = Field(default="", max_length=MAX_SUBJECT_CHARS)
    #: As Gmail returned it, bounded. Not reparsed into a datetime: the exact
    #: string is what the user recognises.
    received_at: str = Field(default="", max_length=64)
    unread: bool = False
    #: Only ever set for a message the application selected and fetched.
    body: str = Field(default="", max_length=MAX_BODY_CHARS)

    @property
    def has_body(self) -> bool:
        return bool(self.body)


class MailWindow(BaseModel):
    """The messages one request found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: Tuple[MailMessage, ...] = ()
    #: How many Gmail offered before bounding, so a truncated set is
    #: recognisable as truncated.
    total_available: int = 0
    #: What was asked, echoed back for the audit trail. The *rendered* query,
    #: which contains only application-cleaned fragments of the user's words.
    query: str = Field(default="", max_length=400)

    def as_external_data(self) -> ExternalData:
        """Render as labelled personal data for the prompt.

        `PRIVATE` because it is the user's own correspondence, and `UNTRUSTED`
        because every word of it was written by whoever sent the mail. Each
        line is flattened so a subject cannot forge the structure of the block
        it sits in.

        **No message id appears here.** The model has no use for one and no
        way to act on one; including it would be handing over a handle to a
        specific message for no purpose.
        """
        lines: List[str] = []
        for index, message in enumerate(self.messages, start=1):
            unread = " (unread)" if message.unread else ""
            lines.append(f"[{index}] {_flatten(message.subject) or '(no subject)'}{unread}")
            lines.append(f"    From: {_flatten(message.sender) or '(unknown sender)'}")
            if message.received_at:
                lines.append(f"    Date: {_flatten(message.received_at)}")
            if message.body:
                lines.append("    Body:")
                for line in message.body.splitlines():
                    # Indented, so the body cannot produce a line that looks
                    # like a new message entry.
                    lines.append(f"      {_flatten(line)}")

        if not lines:
            lines.append("(no messages matched)")

        return ExternalData(
            source="google_gmail",
            content="\n".join(lines),
            # `trust_level` is not passed: `ExternalData` freezes it at
            # UNTRUSTED and excludes it from being set.
            classification=DataClassification.PRIVATE,
        )


def parse_message_ids(payload: Dict[str, Any], limit: int) -> Tuple[Tuple[str, ...], int]:
    """Message ids from a listing response, bounded.

    Only `id` is read. `threadId` is dropped -- Mai does not walk threads --
    and so is `nextPageToken`, which is how pagination stays at one page: the
    token is never captured, so it cannot be followed.
    """
    raw = payload.get("messages")
    if not isinstance(raw, list):
        raw = []

    ids: List[str] = []
    for item in raw:
        if len(ids) >= max(1, min(limit, MAX_MESSAGES)):
            break
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier.strip():
            ids.append(identifier.strip()[:128])

    estimate = payload.get("resultSizeEstimate")
    total = estimate if isinstance(estimate, int) and estimate >= 0 else len(raw)
    return tuple(ids), total


def parse_message(payload: Dict[str, Any], with_body: bool) -> Optional[MailMessage]:
    """One Gmail message, reduced. Never `MailMessage(**payload)`.

    Reading by name is what keeps an unexpected or renamed Gmail field out of
    Mai's state, and what keeps the omissions above actually omitted.
    """
    if not isinstance(payload, dict):
        return None

    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        return None

    headers = _headers_of(payload.get("payload"))
    labels = payload.get("labelIds")
    unread = isinstance(labels, list) and "UNREAD" in labels

    body = ""
    if with_body:
        body = _text_body_of(payload.get("payload"))

    return MailMessage(
        message_id=identifier.strip()[:128],
        sender=_bounded(headers.get("from"), MAX_SENDER_CHARS),
        subject=_bounded(headers.get("subject"), MAX_SUBJECT_CHARS),
        received_at=_bounded(headers.get("date"), 64),
        unread=unread,
        body=body,
    )


def _headers_of(payload: Any) -> Dict[str, str]:
    """The three headers Mai reads, lowercased by name. Bounded."""
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("headers")
    if not isinstance(raw, list):
        return {}

    wanted = {name.lower() for name in KEPT_HEADERS}
    found: Dict[str, str] = {}
    for entry in raw[:MAX_HEADERS]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name.lower() not in wanted:
            continue
        value = entry.get("value")
        if isinstance(value, str):
            found[name.lower()] = value
    return found


def _text_body_of(payload: Any, depth: int = 0) -> str:
    """The first `text/plain` part, decoded and bounded.

    HTML parts are skipped rather than stripped. Rendering HTML would mean
    parsing attacker-supplied markup to produce text for a model, and the
    plain-text alternative is present in essentially every real message.

    Attachments are never read: a part with a `filename` is skipped whatever
    its type, and only `body.data` is decoded -- `body.attachmentId` is a
    handle this module never follows.
    """
    if depth > 8 or not isinstance(payload, dict):
        return ""

    if payload.get("filename"):
        return ""

    mime = payload.get("mimeType")
    if mime == "text/plain":
        return _decode(payload.get("body"))

    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts[:MAX_PARTS]:
            text = _text_body_of(part, depth + 1)
            if text:
                return text

    if depth == 0 and mime is None:
        return _decode(payload.get("body"))
    return ""


def _decode(body: Any) -> str:
    """Base64url `body.data`, bounded before and after decoding."""
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return ""

    # Bounded before decoding, so an enormous payload is never materialised.
    # Four base64 characters carry three bytes.
    encoded = data[: (MAX_BODY_CHARS * 4 // 3) + 8]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except (binascii.Error, ValueError):
        logger.info("Gmail message body could not be decoded")
        return ""

    text = decoded.decode("utf-8", errors="replace")
    return _collapse(text)[:MAX_BODY_CHARS]


def _collapse(text: str) -> str:
    """Trim runs of blank lines and trailing space, keeping paragraph breaks."""
    lines = [line.rstrip() for line in (text or "").splitlines()]
    out: List[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


def _bounded(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _flatten(value)[:limit]


def _flatten(text: str) -> str:
    """Collapse to one line.

    A subject is written by whoever sent the mail. Without this, one
    containing newlines could forge the structure of the block it is rendered
    into -- the same reason Stage 3B flattens a retrieved memory and Stage
    4F-G flattens an event title.
    """
    return " ".join((text or "").split())


def _clean(value: str, pattern: "re.Pattern", limit: int) -> str:
    """Reduce to the safe alphabet, collapse spaces, bound."""
    if not isinstance(value, str):
        return ""
    return " ".join(pattern.sub(" ", value).split())[:limit].strip()


__all__ = [
    "KEPT_HEADERS",
    "MAX_BODIES",
    "MAX_BODY_CHARS",
    "MAX_MESSAGES",
    "MAX_PAGES",
    "MAX_QUERY_TERMS",
    "MailMessage",
    "MailQuery",
    "MailWindow",
    "parse_message",
    "parse_message_ids",
]
