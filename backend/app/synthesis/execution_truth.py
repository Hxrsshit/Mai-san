"""Stage 5D.1: what actually happened, and what the model may say happened.

Stage 5A.2 established that the model's output is a *candidate*, not an answer,
and validated its **shape**. This module validates its **claims**.

The vulnerability it closes was reproduced live. "Tell me about Fable." /
"Search it." / "yes" produced a formatted table headed *"Web Search Results for
Fable"* with titles, snippets and Wikipedia URLs -- while the authoritative
record said `research_outcome=not_research`, `request_path_research_calls=0`,
`actions_executed=0` and the logs showed no outbound call. Nothing was
searched. The model invented the results, the sources and the URLs.

It happened because "Search it." is not recognised as a research request, so no
proposal and no consent gate existed -- but the chat model, which does see
conversation history, offered a search anyway, the user said "yes", and the
model delivered on a promise the application had never registered.

### The rule

    Execution truth comes from execution.
    The model may *describe* execution. It may not *declare* it.

Two layers, because either alone is insufficient:

1. **Preventive.** Synthesis is told the turn's execution facts explicitly,
   including the negative case -- previously the prompt said nothing at all
   when nothing had run, which is precisely the silence the model filled.
2. **Detective.** This module re-reads the generated prose and refuses any
   claim of an external action the authoritative record does not support. A
   prompt is an instruction; instructions are not a security control.

### What is *not* a claim

A crude keyword blocker would be worse than nothing: it would refuse "I can
search the web for you", which is the single most useful sentence Mai says on a
research turn. The detector therefore works per clause and distinguishes

- *assertion*   -- "I searched the web and found..."      → a claim
- *offer*       -- "I can search the web for you"         → not a claim
- *negation*    -- "I haven't searched it yet"            → not a claim
- *discussion*  -- "here is how web search works"         → not a claim
- *attribution* -- "you asked me to search Fable"         → not a claim
"""

import enum
import re
from typing import Any, FrozenSet, NamedTuple, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Longest response examined. Past this the text is accepted without scanning:
#: the check is linear, but an unbounded scan on every turn is a cost with no
#: decision at the end. Matches `app.synthesis.contract`'s reasoning.
MAX_EXAMINED_CHARS = 200_000

#: Most execution-truth recovery attempts. Exactly one, for the same reason
#: the response contract allows exactly one: a model that has just fabricated
#: an execution has demonstrated it is not following the contract, and looping
#: would be a denial of service it triggers against itself.
MAX_RECOVERY_ATTEMPTS = 1


class ExecutionState(str, enum.Enum):
    """Whether an external action actually happened on this turn.

    Five states, deliberately not collapsed. The distinction between "you
    declined" and "it failed" and "I cannot tell" is the whole point: each
    calls for a different sentence, and **UNKNOWN must never become success**.
    """

    #: Nothing about this turn concerned the channel.
    NOT_REQUESTED = "not_requested"
    #: A proposal exists -- possibly awaiting consent, declined, abandoned or
    #: unconfigured -- and nothing was executed.
    PROPOSED_NOT_EXECUTED = "proposed_not_executed"
    #: The action ran and returned. The only state that licenses a claim.
    EXECUTED_SUCCESSFULLY = "executed_successfully"
    #: The action was attempted and failed.
    EXECUTED_FAILED = "executed_failed"
    #: The record cannot be read. Treated as "did not happen" for claims.
    UNKNOWN = "unknown"


class Channel(str, enum.Enum):
    """An external capability a response can claim to have used."""

    WEB = "web"
    MAIL = "mail"
    CALENDAR = "calendar"


#: The one state that licenses a claim.
#:
#: Written as membership rather than "not a failure", so a state added later
#: licenses nothing until someone decides it should.
CLAIMABLE_STATES = frozenset({ExecutionState.EXECUTED_SUCCESSFULLY})


class ExecutionRecord(NamedTuple):
    """The authoritative per-turn execution facts.

    Built from the existing outcome enums -- `ResearchOutcome`,
    `MailOutcome`, `CalendarOutcome` -- rather than from a new authority.
    Those enums are already the system's record of what ran; a second one
    would be a second thing to keep true.

    Never built from model output. That is the entire point.
    """

    web: ExecutionState = ExecutionState.NOT_REQUESTED
    mail: ExecutionState = ExecutionState.NOT_REQUESTED
    calendar: ExecutionState = ExecutionState.NOT_REQUESTED

    #: How many messages the mail read actually returned.
    #:
    #: `None` when no mail read happened, which is different from zero: zero
    #: is a fact about the mailbox, `None` is the absence of a fact. Only the
    #: application sets it, from `MailResult.message_count`.
    mail_count: Optional[int] = None

    def state_for(self, channel: Channel) -> ExecutionState:
        return {
            Channel.WEB: self.web,
            Channel.MAIL: self.mail,
            Channel.CALENDAR: self.calendar,
        }[channel]

    def may_claim(self, channel: Channel) -> bool:
        return self.state_for(channel) in CLAIMABLE_STATES

    @property
    def anything_executed(self) -> bool:
        return any(self.may_claim(channel) for channel in Channel)


class TruthVerdict(NamedTuple):
    """Whether the response's claims match what happened."""

    ok: bool = True
    #: Channels claimed without an execution to back them.
    violations: Tuple[Channel, ...] = ()
    #: A short application reason code. Never the offending text.
    reason: str = ""


# --- Mapping outcomes onto execution states ----------------------------------
#
# Explicit and total. An outcome that is not named maps to UNKNOWN, so adding
# a state to any of those enums cannot silently license a claim.

_RESEARCH_STATES = {
    "not_research": ExecutionState.NOT_REQUESTED,
    "completed": ExecutionState.EXECUTED_SUCCESSFULLY,
    "failed": ExecutionState.EXECUTED_FAILED,
    "awaiting_confirmation": ExecutionState.PROPOSED_NOT_EXECUTED,
    "declined": ExecutionState.PROPOSED_NOT_EXECUTED,
    "abandoned": ExecutionState.PROPOSED_NOT_EXECUTED,
    "disabled": ExecutionState.PROPOSED_NOT_EXECUTED,
    "not_configured": ExecutionState.PROPOSED_NOT_EXECUTED,
    "needs_clarification": ExecutionState.PROPOSED_NOT_EXECUTED,
}

_MAIL_STATES = {
    "not_mail": ExecutionState.NOT_REQUESTED,
    "completed": ExecutionState.EXECUTED_SUCCESSFULLY,
    "failed": ExecutionState.EXECUTED_FAILED,
    "awaiting_confirmation": ExecutionState.PROPOSED_NOT_EXECUTED,
    "declined": ExecutionState.PROPOSED_NOT_EXECUTED,
    "abandoned": ExecutionState.PROPOSED_NOT_EXECUTED,
    "disabled": ExecutionState.PROPOSED_NOT_EXECUTED,
    "not_configured": ExecutionState.PROPOSED_NOT_EXECUTED,
    "not_connected": ExecutionState.PROPOSED_NOT_EXECUTED,
    "reauthorisation_required": ExecutionState.PROPOSED_NOT_EXECUTED,
    "clarification_needed": ExecutionState.PROPOSED_NOT_EXECUTED,
}

_CALENDAR_STATES = {
    "not_calendar": ExecutionState.NOT_REQUESTED,
    "completed": ExecutionState.EXECUTED_SUCCESSFULLY,
    "failed": ExecutionState.EXECUTED_FAILED,
    "disabled": ExecutionState.PROPOSED_NOT_EXECUTED,
    "not_configured": ExecutionState.PROPOSED_NOT_EXECUTED,
    "not_connected": ExecutionState.PROPOSED_NOT_EXECUTED,
    "reauthorisation_required": ExecutionState.PROPOSED_NOT_EXECUTED,
    "write_not_supported": ExecutionState.PROPOSED_NOT_EXECUTED,
    "clarification_needed": ExecutionState.PROPOSED_NOT_EXECUTED,
}


def _state(result: Any, table: dict, evidence_attr: str) -> ExecutionState:
    """Read one layer's outcome into an execution state.

    `evidence_attr` is a second, independent condition: a channel counts as
    executed only when its outcome says so **and** the layer actually produced
    the content that outcome implies. An outcome of `completed` with no block
    is a contradiction, and resolving it towards "did not happen" is the safe
    direction.
    """
    if result is None:
        return ExecutionState.NOT_REQUESTED

    outcome = getattr(result, "outcome", None)
    value = getattr(outcome, "value", outcome)
    if not isinstance(value, str):
        return ExecutionState.UNKNOWN

    state = table.get(value, ExecutionState.UNKNOWN)

    if state is ExecutionState.EXECUTED_SUCCESSFULLY:
        if not getattr(result, evidence_attr, ""):
            # Said it completed but produced nothing. Do not license a claim
            # on an outcome the layer's own output does not corroborate.
            return ExecutionState.UNKNOWN
    return state


def record_for_turn(
    research: Any = None,
    mail: Any = None,
    calendar: Any = None,
    workflow: Any = None,
) -> ExecutionRecord:
    """Build the authoritative record from this turn's layer results.

    A workflow can perform the same reads through its own path, so its
    evidence is folded into the same channels -- one truth per channel, not
    one per code path.
    """
    web = _state(research, _RESEARCH_STATES, "results_block")
    if web is not ExecutionState.EXECUTED_SUCCESSFULLY and workflow is not None:
        if getattr(workflow, "researched", False) and getattr(
            workflow, "research_block", ""
        ):
            web = ExecutionState.EXECUTED_SUCCESSFULLY

    calendar_state = _state(calendar, _CALENDAR_STATES, "events_block")
    if calendar_state is not ExecutionState.EXECUTED_SUCCESSFULLY and workflow is not None:
        if getattr(workflow, "calendar_block", ""):
            calendar_state = ExecutionState.EXECUTED_SUCCESSFULLY

    mail_state = _state(mail, _MAIL_STATES, "messages_block")

    # The count, only when a read actually succeeded. Read off the layer
    # result -- the application's own record of what came back -- never off
    # the prose, which is the thing being checked.
    mail_count: Optional[int] = None
    if mail_state is ExecutionState.EXECUTED_SUCCESSFULLY and mail is not None:
        candidate = getattr(mail, "message_count", None)
        if isinstance(candidate, int) and candidate >= 0:
            mail_count = candidate

    return ExecutionRecord(
        web=web,
        mail=mail_state,
        calendar=calendar_state,
        mail_count=mail_count,
    )


# --- Claim detection -----------------------------------------------------------

#: Splits prose into clauses.
#:
#: Per clause rather than per response, because one reply legitimately mixes
#: both kinds: "I haven't searched yet, but I can search now" must not be read
#: as a claim, and "I searched the web. I haven't checked your email." must
#: flag the web and not the mail.
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+|\s+—\s+|\s+--\s+")

#: A clause that offers, plans, asks about or refuses an action rather than
#: reporting one. Checked first; anything matching cannot be a claim.
_NOT_AN_ASSERTION = re.compile(
    r"\b(?:can|could|would|will|shall|may|might|should|"
    r"want\s+me|like\s+me|need\s+me|able\s+to|going\s+to|about\s+to|"
    r"let\s+me|i'?ll|i'?d|happy\s+to|ready\s+to|offer\s+to|permission|"
    r"not|never|no\b|none|haven'?t|hasn'?t|hadn'?t|didn'?t|don'?t|doesn'?t|"
    r"won'?t|cannot|can'?t|unable|without|instead\s+of|rather\s+than|"
    r"if|once|unless|before|whether|asked\s+me\s+to|you\s+asked|"
    r"would\s+you|shall\s+i|do\s+you\s+want)\b",
    re.IGNORECASE,
)

#: "I did", in the forms people actually write it.
#:
#: Factored out because it was wrong three times over in three places: written
#: inline as `\bi\s+(?:have\s+|'ve\s+)*`, it required whitespace after the
#: "i" and so missed every `I've` contraction -- the single most common way an
#: assistant reports a completed action.
_I_DID = r"\bi(?:'ve|\s+have|\s+had)?\s+(?:just\s+|already\s+)?"

#: Assertions that an action was carried out, per channel.
_CLAIMS = {
    Channel.WEB: re.compile(
        "(?:"
        + _I_DID + r"(?:searched|googled|browsed)\b"
        r"|" + _I_DID + r"(?:ran|run|performed|did)\s+(?:a|the)\s+(?:web\s+)?search\b"
        r"|" + _I_DID + r"looked\s+(?:it|this|that|them)?\s*up\b"
        r"|\bhere\s+(?:are|is)\s+(?:the\s+)?(?:top\s+)?(?:\d+\s+)?"
        r"(?:web\s+)?(?:search\s+)?results\b"
        r"|\b(?:web\s+)?search\s+results\s+for\b"
        r"|\bfrom\s+(?:the|a|my)\s+(?:web\s+)?search\b"
        r"|\b(?:according\s+to|based\s+on)\s+(?:the|my|a)\s+(?:web\s+)?search\b"
        r"|\bi\s+found\s+(?:these|the\s+following)\s+"
        r"(?:results|sources|articles|pages|links)\b"
        r"|\bthe\s+search\s+(?:returned|found|turned\s+up|shows?|says?)\b"
        r")",
        re.IGNORECASE,
    ),
    Channel.MAIL: re.compile(
        "(?:"
        + _I_DID + r"(?:checked|read|opened|retrieved|reviewed|scanned|"
        r"went\s+through|looked\s+(?:at|through))\s+"
        r"(?:your\s+|the\s+)?(?:gmail|e-?mails?|inbox|mail|messages)\b"
        r"|\bin\s+your\s+(?:inbox|mailbox)\s*,?\s*(?:i\s+)?(?:found|see|there)\b"
        r"|\byou\s+have\s+\d+\s+(?:new\s+|unread\s+)?(?:e-?mails?|messages)\b"
        r"|\byour\s+(?:latest|recent|most\s+recent)\s+e-?mails?\s+(?:are|include)\b"
        r")",
        re.IGNORECASE,
    ),
    Channel.CALENDAR: re.compile(
        "(?:"
        + _I_DID + r"(?:checked|read|opened|retrieved|reviewed|"
        r"looked\s+(?:at|through))\s+(?:your\s+|the\s+)?calendar\b"
        r"|\b(?:on\s+)?your\s+calendar\s*,?\s*(?:you\s+have|there\s+(?:is|are))\b"
        r"|\byou\s+have\s+.{0,40}\bscheduled\b"
        r"|\byour\s+(?:schedule|calendar)\s+(?:for\s+\w+\s+)?"
        r"(?:is|shows?|contains?|has)\b"
        r")",
        re.IGNORECASE,
    ),
}

#: A markdown table whose header names a source column.
#:
#: The observed fabrication rendered exactly this. It is a *corroborating*
#: signal, used only for the web channel and only alongside a URL, because a
#: table with a "Source" column is otherwise a perfectly ordinary thing to
#: write about knowledge the model already has.
_SOURCE_TABLE = re.compile(r"^\s*\|.*\bsources?\b.*\|\s*$", re.IGNORECASE | re.MULTILINE)
_URL = re.compile(r"https?://\S+")

#: How many URLs, alongside a source table, read as presented search results.
_FABRICATED_CITATION_URLS = 2


def claims(text: str) -> FrozenSet[Channel]:
    """Which external actions this text asserts were carried out.

    Empty for the overwhelming majority of responses, which is the point: a
    check that fires often would be a check nobody could keep enabled.
    """
    if not text or not text.strip():
        return frozenset()
    if len(text) > MAX_EXAMINED_CHARS:
        return frozenset()

    found = set()
    for clause in _CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if not clause or clause.endswith("?"):
            # A question is not a report. "Shall I search the web?" asserts
            # nothing about what has happened.
            continue
        if _NOT_AN_ASSERTION.search(clause):
            continue
        for channel, pattern in _CLAIMS.items():
            if pattern.search(clause):
                found.add(channel)

    # A rendered results table with real-looking links is a claim even when
    # the prose around it is carefully hedged.
    if Channel.WEB not in found and _SOURCE_TABLE.search(text):
        if len(_URL.findall(text)) >= _FABRICATED_CITATION_URLS:
            found.add(Channel.WEB)

    return frozenset(found)


#: A stated number of messages.
#:
#: Deliberately narrow. It matches a digit or a small number-word immediately
#: qualifying a mail noun -- "3 unread emails", "you have two messages" -- and
#: not prose that merely contains a number near the word "email". A detector
#: that over-matches would reject true answers, and a truth check that cries
#: wolf gets the budget spent on regenerating correct text.
_COUNT_CLAIM = re.compile(
    r"\b(?P<count>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:new\s+|unread\s+|recent\s+|important\s+|urgent\s+){0,2}"
    r"(?:e-?mails?|messages)\b",
    re.IGNORECASE,
)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def stated_counts(text: str) -> FrozenSet[int]:
    """Every message count the text asserts. Data, never a claim of truth."""
    found = set()
    for match in _COUNT_CLAIM.finditer(text or ""):
        raw = match.group("count").lower()
        if raw.isdigit():
            # A three-digit cap is already in the pattern; this guards the
            # int() against nothing surprising, and keeps the set small.
            found.add(int(raw))
        elif raw in _NUMBER_WORDS:
            found.add(_NUMBER_WORDS[raw])
    return frozenset(found)


def validate(text: str, record: ExecutionRecord) -> TruthVerdict:
    """Check the response's claims against what actually ran."""
    asserted = claims(text)
    if not asserted:
        return TruthVerdict(ok=True, reason="no_execution_claim")

    violations = tuple(
        channel
        for channel in Channel
        if channel in asserted and not record.may_claim(channel)
    )
    if not violations:
        miscount = _count_violation(text, record)
        if miscount is not None:
            return miscount
        return TruthVerdict(ok=True, reason="claims_supported")

    logger.warning(
        "Refused a response claiming an action that did not happen",
        # Channels and states only. Never the text: a fabricated response on a
        # turn carrying calendar or mail data into the prompt is exactly the
        # thing not to copy into a log.
        extra={
            "channels": ",".join(channel.value for channel in violations),
            "states": ",".join(
                record.state_for(channel).value for channel in violations
            ),
            "response_chars": len(text),
        },
    )
    return TruthVerdict(
        ok=False,
        violations=violations,
        reason="unsupported_execution_claim",
    )


def _count_violation(text: str, record: ExecutionRecord) -> Optional[TruthVerdict]:
    """Refuse an answer that states a message count the read did not produce.

    The channel check above proves a mail read *happened*. It says nothing
    about the number, and "you have 5 unread emails" over a window of 2 is a
    fabrication of exactly the kind this layer exists to stop -- a true claim
    about the action wrapped around a false claim about the result.

    Only applies when a read succeeded and a count is known. A turn with no
    mail, or one whose count was never recorded, is not second-guessed here;
    and a stated count that *matches* is left alone, as is prose with no
    count in it at all.
    """
    if record.mail_count is None or not record.may_claim(Channel.MAIL):
        return None

    stated = stated_counts(text)
    if not stated or record.mail_count in stated:
        return None

    logger.warning(
        "Refused a response stating a message count the read did not produce",
        # Numbers, never the text or any part of a message.
        extra={
            "retrieved": record.mail_count,
            "stated": ",".join(str(value) for value in sorted(stated)),
            "response_chars": len(text),
        },
    )
    return TruthVerdict(
        ok=False,
        violations=(Channel.MAIL,),
        reason="mail_count_mismatch",
    )


#: How each channel is described to the model, per state.
#:
#: Written out per state rather than as "did/did not", because the difference
#: between "you declined" and "it failed" and "nothing was asked" is exactly
#: what the model needs in order to say something true about it.
_CHANNEL_NOUNS = {
    Channel.WEB: "web search",
    Channel.MAIL: "email read",
    Channel.CALENDAR: "calendar read",
}

_STATE_PHRASES = {
    ExecutionState.NOT_REQUESTED: "was not requested and did NOT happen",
    ExecutionState.PROPOSED_NOT_EXECUTED: (
        "was discussed or proposed but did NOT happen"
    ),
    ExecutionState.EXECUTED_SUCCESSFULLY: "DID happen; its results are shown above",
    ExecutionState.EXECUTED_FAILED: "was attempted and FAILED; there are no results",
    ExecutionState.UNKNOWN: "cannot be confirmed, so treat it as having NOT happened",
}

#: The instruction accompanying the facts.
#:
#: Deliberately about *reporting*, not about behaviour in general: the model is
#: told it may still answer from its own knowledge, because the failure to
#: avoid is a model that refuses to answer at all once it learns it has not
#: searched.
_EXECUTION_NOTE_RULE = (
    "Do not state or imply that any action above happened unless it is marked "
    "as having happened. Do not present results, sources, URLs, titles or "
    "snippets as though they came from an action that did not run. You may "
    "still answer from your own knowledge — say so plainly, and offer to run "
    "the action if the user wants current or personal information."
)


def render_note(record: ExecutionRecord) -> str:
    """The authoritative facts, as application text for the prompt.

    Always lists every channel, including the ones that did nothing. The
    negative case is the one that matters: before Stage 5D.1 the prompt said
    nothing at all when nothing had run, and the model filled the silence.
    """
    lines = [
        f"- {_CHANNEL_NOUNS[channel]}: {_STATE_PHRASES[record.state_for(channel)]}"
        for channel in Channel
    ]
    return "\n".join(lines) + "\n\n" + _EXECUTION_NOTE_RULE


def truthful_reply(record: ExecutionRecord, violations: Tuple[Channel, ...]) -> str:
    """What the application says when the model claimed something untrue.

    Written here, from the record, so it is true by construction -- there is
    no model in the loop to rephrase "I have not searched" into "I searched".
    """
    named = [_CHANNEL_NOUNS[channel] for channel in violations] or ["action"]
    listed = named[0] if len(named) == 1 else (
        ", ".join(named[:-1]) + " and " + named[-1]
    )

    # A channel that *is* claimable but still appears in the violations did
    # happen -- what was wrong was the number. Saying "I have not read your
    # email" here would be its own untruth, which is the fault this whole
    # module exists to prevent, so the miscount gets its own sentence with
    # the real figure in it.
    miscounted = [channel for channel in violations if record.may_claim(channel)]
    if miscounted and record.mail_count is not None and Channel.MAIL in miscounted:
        if record.mail_count == 0:
            return (
                "I did read your mail, but I had the number wrong a moment "
                "ago: nothing matched what you asked for. I would rather say "
                "that than invent messages."
            )
        one = record.mail_count == 1
        return (
            f"I did read your mail and found {record.mail_count} "
            f"message{'' if one else 's'}, but I could not describe "
            f"{'it' if one else 'them'} accurately just then. Ask me again "
            "and I will answer from what actually came back."
        )

    proposed = [
        channel for channel in violations
        if record.state_for(channel) is ExecutionState.PROPOSED_NOT_EXECUTED
    ]
    failed = [
        channel for channel in violations
        if record.state_for(channel) is ExecutionState.EXECUTED_FAILED
    ]

    if failed:
        return (
            f"I tried to do that, but the {listed} failed, so I have no real "
            "results to show you. I would rather tell you that than make "
            "something up. Ask me again and I will retry it."
        )
    if proposed:
        return (
            f"I have not actually done the {listed} yet — it was proposed but "
            "never ran, so I have no real results to show you. Ask me to go "
            "ahead and I will run it and answer from what actually comes back."
        )
    return (
        f"I started to answer as though I had done a {listed}, but I have not "
        "— so anything I showed you would have been invented rather than "
        "retrieved. Ask me to run it and I will answer from the real results."
    )


def known_claim_channels() -> Tuple[Channel, ...]:
    """The channels this module can adjudicate. For tests and auditing."""
    return tuple(Channel)


__all__ = [
    "stated_counts",
    "CLAIMABLE_STATES",
    "MAX_EXAMINED_CHARS",
    "MAX_RECOVERY_ATTEMPTS",
    "Channel",
    "ExecutionRecord",
    "ExecutionState",
    "TruthVerdict",
    "claims",
    "known_claim_channels",
    "record_for_turn",
    "render_note",
    "truthful_reply",
    "validate",
]
