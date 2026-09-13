"""Recognising a request to be briefed before a meeting, and planning it.

Deterministic throughout, like Stage 4F-E's planner and Stage 4G.1's calendar
grammar. A phrase table decides whether a message asks for a briefing; a
template decides what the plan is. No model is consulted, which is what makes
the resulting plan application state that authorization can be built on.

Three shapes, and which one you get is decided by the message alone:

    calendar                          "give me a briefing for my meeting
      -> synthesise                    tomorrow"   (no subject named)

    calendar -> research              "I have a meeting with Acme tomorrow.
      -> synthesise                    give me a briefing"

    calendar -> research              "...and write it up as a document"
      -> synthesise -> artifact

The research subject comes from the user's own words. Never the calendar
-----------------------------------------------------------------------

It is tempting to read the company name off the event title -- Mai is about to
fetch the event anyway, and "Acme <> Mai sync" contains exactly the subject a
briefing wants. That is refused here, and the reason is the whole of §12:
**anyone can put text into your calendar by sending you an invitation.**

If the event title chose the search query, then whoever sent the invitation
would choose what Mai sends to an external search provider. Untrusted content
would be steering an outbound request -- which is the definition of the
boundary this system exists to hold, whatever the content happened to say.

So the subject is extracted from the user's message, and a briefing for a
meeting whose subject the user did not name is a *calendar-only* briefing that
says so. Deriving the subject from the event and putting it to the user for
approval would be defensible and is written up as a deferred option; it is not
what this stage does.
"""

import re
from typing import NamedTuple, Optional, Tuple

from app.core.logging import get_logger
from app.integrations.search import MAX_QUERY_CHARS, normalise_query
from app.orchestration import calendar_language
from app.workflows.limits import (
    MAX_ARTIFACT_NAME_CHARS,
    MAX_REQUEST_CHARS,
    MAX_SUBJECT_CHARS,
)
from app.workflows.schemas import StepKind, WorkflowPlan, WorkflowStep

logger = get_logger(__name__)

#: How many events a briefing may read. Below the calendar tool's own bound.
BRIEFING_MAX_EVENTS = 10

# --- Grammar ----------------------------------------------------------------

#: Ways of asking to be brought up to speed.
#:
#: Every one is an explicit request directed at Mai. "A briefing was held"
#: and "briefings are useful" match none of them.
_BRIEFING_REQUEST = re.compile(
    r"(?:"
    r"brief\s+me|give\s+me\s+(?:a|an|some)?\s*(?:quick\s+|short\s+|brief\s+)?"
    r"(?:briefing|background|context|rundown|overview|prep)|"
    r"(?:a|an|some)\s+(?:quick\s+|short\s+)?briefing|"
    r"prepare\s+me|prep\s+me|get\s+me\s+(?:ready|up\s+to\s+speed)|"
    r"what\s+should\s+i\s+know|what\s+do\s+i\s+need\s+to\s+know|"
    r"fill\s+me\s+in|catch\s+me\s+up|bring\s+me\s+up\s+to\s+speed"
    r")",
    re.IGNORECASE,
)

#: The kinds of appointment a briefing can be *for*.
_MEETING_NOUN = (
    r"(?:meetings?|calls?|syncs?|catch-?ups?|interviews?|appointments?|"
    r"1:1s?|one-on-ones?|stand-?ups?|reviews?|demos?|pitches|sessions?|"
    r"presentations?|calendar|schedule)"
)
_MEETING = re.compile(rf"\b{_MEETING_NOUN}\b", re.IGNORECASE)

#: Text that discusses briefings rather than asking for one.
_NOT_A_REQUEST = re.compile(
    r"\b(?:what\s+is\s+(?:a|an)\b|what(?:'|’)?s\s+(?:a|an)\b|"
    r"explain|how\s+(?:do|does|to)\b|"
    r"don'?t|do\s+not|never|no\s+need|"
    r"i\s+wish|if\s+only|i'?d\s+love|"
    r"i\s+(?:already\s+)?(?:had|attended|prepared|briefed|went))\b",
    re.IGNORECASE,
)

#: A named subject after "with": "meeting with Acme", "call with the Acme team".
#:
#: Position is unambiguous here, so no capitalisation is required -- whatever
#: follows "with" is what the meeting is with. It is still bounded to a few
#: words and cut at the first clause word, because "meeting with Acme and then
#: write a doc" must yield "Acme" rather than the rest of the sentence.
_SUBJECT_WITH = re.compile(
    rf"\b{_MEETING_NOUN}\s+with\s+(?P<subject>[A-Za-z0-9&'’.\- ]{{1,80}})",
    re.IGNORECASE,
)

#: A named subject in front: "tomorrow's Acme meeting", "the Acme review".
#:
#: **Capitalisation is required here, and it is doing real work.** In this
#: position the preceding words are unbounded ordinary English -- "I have a
#: client meeting", "Give me a quick briefing for my meeting" -- and a pattern
#: that took whatever came before the noun produced subjects like "I have" and
#: "Give me briefing for", each of which would have been sent to a search
#: provider as a query.
#:
#: A proper noun is the only signal in English that separates "Acme meeting"
#: from "client meeting", and getting it wrong in the permissive direction
#: means searching the web for a fragment of the user's own sentence.
_SUBJECT_BEFORE = re.compile(
    r"(?:^|[,;.]\s*|\b(?:my|our|the|a|an|this|next|"
    r"tomorrow(?:'|’)?s|today(?:'|’)?s|friday(?:'|’)?s)\s+)"
    r"(?P<subject>(?:[A-Z][\w&'’.\-]*\s+){0,2}[A-Z][\w&'’.\-]*)"
    rf"\s+{_MEETING_NOUN}\b",
)

#: Words that end a subject phrase. Everything after one belongs to the rest
#: of the sentence, not to the name of what the meeting is with.
_CLAUSE_WORDS = frozenset({
    "and", "then", "so", "but", "or", "because", "before", "after", "to",
    "for", "about", "on", "at", "in", "please", "give", "write", "save",
    "create", "make", "brief", "prepare", "prep", "research", "i", "we",
    "you", "can", "could", "would", "will", "should", "need", "want",
})

#: An explicit research instruction with no subject of its own.
#:
#: "research the company", "look them up" -- the user asked for research but
#: named nothing, so there is nothing deterministic to search for.
_ANAPHORIC_RESEARCH = re.compile(
    r"\b(?:research|look\s+up|read\s+up\s+on|find\s+out\s+about)\s+"
    r"(?:the\s+)?(?:company|them|it|him|her|they|this|that|topic|subject|"
    r"client|customer|org|organisation|organization)\b",
    re.IGNORECASE,
)

#: Words that are never a research subject on their own.
#:
#: Possessives, articles, day words and the generic descriptions people put in
#: front of "meeting". "I have a client meeting tomorrow" names no company.
_SUBJECT_STOPWORDS = frozenset({
    "a", "an", "the", "my", "our", "your", "his", "her", "their", "this",
    "that", "these", "those", "next", "last", "first", "second", "final",
    "today", "tomorrow", "tonight", "yesterday", "morning", "afternoon",
    "evening", "week", "weekend", "month", "day", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "client", "customer", "team", "work", "project", "internal", "external",
    "important", "big", "quick", "short", "brief", "upcoming", "early",
    "late", "new", "regular", "weekly", "daily", "monthly", "board",
    "all", "some", "any", "another", "other", "s",
})

#: Asking for the briefing to be written to a file.
_WANTS_ARTIFACT = re.compile(
    r"\b(?:write|save|put|create|make|generate|produce)\b[^.?!]{0,40}?"
    r"\b(?:document|doc|file|note|notes|report|write-?up|briefing\s+doc\w*)\b",
    re.IGNORECASE,
)

_UNSAFE_NAME = re.compile(r"[^a-z0-9]+")

ARTIFACT_EXTENSION = ".txt"
DEFAULT_ARTIFACT_NAME = "briefing"


class BriefingRequest(NamedTuple):
    """What the message asked for. Application state, never model output."""

    #: The research subject the user named, or "" when they named none.
    subject: str = ""
    #: The window to read, as two RFC-3339 timestamps from the application
    #: clock in the configured zone.
    starts_at: str = ""
    ends_at: str = ""
    window_label: str = ""
    wants_artifact: bool = False
    #: The user asked for research but named nothing searchable.
    needs_subject: bool = False

    @property
    def wants_research(self) -> bool:
        return bool(self.subject)


def recognise(
    message: str, now=None, tz=None
) -> Optional[BriefingRequest]:
    """Read one message. None means "not a briefing request".

    None is the common answer and the safe one. Three conditions must all
    hold: an explicit request to be briefed, a meeting to be briefed about,
    and a time that resolves. Any one alone is ordinary conversation.
    """
    if not message or len(message) > MAX_REQUEST_CHARS:
        return None

    text = " ".join(message.split())

    if _NOT_A_REQUEST.search(text):
        return None
    if not _BRIEFING_REQUEST.search(text):
        return None
    if not _MEETING.search(text):
        # A briefing about something that is not an appointment is not a
        # calendar composition. "Brief me on the history of Rome" reaches the
        # ordinary research path, which is where it belongs.
        return None

    window = calendar_language.resolve_window(text, now=now, tz=tz)
    if window is None:
        # No resolvable time. Reading "some meeting" would mean choosing a
        # window the user did not name, over private data.
        return None

    starts_at, ends_at, label = window
    subject = _subject_of(text)

    return BriefingRequest(
        subject=subject,
        starts_at=starts_at,
        ends_at=ends_at,
        window_label=label,
        wants_artifact=bool(_WANTS_ARTIFACT.search(text)),
        needs_subject=(not subject) and bool(_ANAPHORIC_RESEARCH.search(text)),
    )


def build_plan(message: str, request: BriefingRequest) -> Optional[WorkflowPlan]:
    """Turn a recognised request into the bounded plan. Never raises.

    The step set is chosen here, from application state, and the plan's own
    validation refuses anything over the composition bounds -- so this
    function cannot produce a plan larger than Stage 4H permits even if it is
    wrong about what to include.
    """
    steps = [
        WorkflowStep(
            index=0,
            kind=StepKind.CALENDAR,
            arguments={
                "starts_at": request.starts_at,
                "ends_at": request.ends_at,
                "max_results": BRIEFING_MAX_EVENTS,
                # The schedule rendering: a briefing needs to know *which*
                # meeting, so titles and times are the point. Still the
                # minimised set -- no attendees, descriptions, links or
                # addresses, which `parse_events` drops before this.
                "intent": "calendar_schedule",
                "window_label": request.window_label,
            },
        ),
    ]

    index = 1
    if request.wants_research:
        try:
            query = normalise_query(request.subject)
        except ValueError:
            return None
        steps.append(
            WorkflowStep(
                index=index,
                kind=StepKind.RESEARCH,
                depends_on=(0,),
                arguments={"query": query[:MAX_QUERY_CHARS]},
            )
        )
        index += 1

    synthesis_index = index
    steps.append(
        WorkflowStep(
            index=synthesis_index,
            kind=StepKind.SYNTHESISE,
            depends_on=tuple(range(synthesis_index)),
        )
    )
    index += 1

    if request.wants_artifact:
        steps.append(
            WorkflowStep(
                index=index,
                kind=StepKind.ARTIFACT,
                depends_on=(synthesis_index,),
                arguments={"path": _artifact_name(request.subject)},
            )
        )

    try:
        return WorkflowPlan(request=message[:MAX_REQUEST_CHARS], steps=tuple(steps))
    except ValueError:
        # A plan over the composition bounds is refused, not trimmed.
        logger.warning("Briefing plan exceeded the composition bounds")
        return None


def _subject_of(text: str) -> str:
    """The research subject the user named, or "".

    Two shapes, in order: after "with", or immediately before the meeting
    noun. Whatever comes out is reduced to words that could name something
    searchable -- so "my client meeting" yields nothing, which makes it a
    calendar-only briefing rather than a search for the word "client".
    """
    for pattern in (_SUBJECT_WITH, _SUBJECT_BEFORE):
        match = pattern.search(text)
        if match is None:
            continue
        cleaned = _clean_subject(match.group("subject"))
        if cleaned:
            return cleaned
    return ""


def _clean_subject(raw: str) -> str:
    kept = []
    for word in re.split(r"\s+", (raw or "").strip()):
        bare = word.strip("'’.,-").lower()
        if not bare:
            continue
        if bare in _CLAUSE_WORDS:
            # The subject ended here; the rest is another clause.
            break
        if bare in _SUBJECT_STOPWORDS:
            continue
        kept.append(word)
        if len(kept) >= 4:
            break
    words = kept
    # A trailing possessive belongs to the meeting, not the company:
    # "tomorrow's Acme meeting" -> "Acme".
    subject = " ".join(words).strip(" .,-'’")
    subject = re.sub(r"(?:'s|’s)$", "", subject).strip()
    if len(subject) < 2:
        return ""
    return subject[:MAX_SUBJECT_CHARS]


def _artifact_name(subject: str) -> str:
    """A workspace-relative filename. Always safe, always `.txt`.

    The same reduction Stage 4F-E uses: the output alphabet is `[a-z0-9-]`, so
    traversal is unrepresentable rather than merely refused.
    """
    slug = _UNSAFE_NAME.sub("-", (subject or "").lower()).strip("-")
    slug = slug[:MAX_ARTIFACT_NAME_CHARS].strip("-")
    if slug.endswith("-txt"):
        slug = slug[: -len("-txt")]
    base = f"{slug}-briefing" if slug else DEFAULT_ARTIFACT_NAME
    return f"{base[:MAX_ARTIFACT_NAME_CHARS]}{ARTIFACT_EXTENSION}"


def known_trigger_shapes() -> Tuple[str, ...]:
    """The patterns this module recognises. For tests and for auditing."""
    return (_BRIEFING_REQUEST.pattern, _MEETING.pattern)


__all__ = [
    "BRIEFING_MAX_EVENTS",
    "BriefingRequest",
    "build_plan",
    "known_trigger_shapes",
    "recognise",
]
