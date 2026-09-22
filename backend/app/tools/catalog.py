"""The tool catalogue: every capability the application declares.

This module is the *only* place `register` is called. Tool classes are
imported by name at module scope -- there is no dynamic import, no lookup of a
class by string, and no plugin loading. Adding a tool means editing this file.

Most of this file is **declarations**: a name, a category, a risk level and
an approval requirement. The `future_*` entries exist so the risk model can be
exercised end to end against realistic metadata, and every one of them is
refused or gated by policy today.

Three entries are different. `create_text_file`, `read_text_file` and
`list_workspace_files` have implementations, added in Stage 4E, and those
implementations live in `app.execution.tools` -- not here. What this file
declares about them is still only metadata and an argument schema; the
capability to run them exists in one dispatcher, behind an approval bound to
the exact payload, and only when execution is switched on.
"""

import enum
from typing import Optional, Tuple, Type

from app.integrations.gmail_schemas import MAX_BODIES as GMAIL_MAX_BODIES

from pydantic import Field

from app.core.logging import get_logger
from app.tools.base import Tool, ToolArguments
from app.tools.registry import ToolRegistry, get_registry
from app.tools.schemas import (
    ExecutionMode,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
)

logger = get_logger(__name__)


# --- The one inert tool -----------------------------------------------------


class EchoArguments(ToolArguments):
    """Arguments for `echo`. One bounded string, nothing else."""

    text: str = Field(..., min_length=1, max_length=500)


class EchoTool(Tool):
    """A deliberately inert tool, present only to exercise the framework.

    It has no side effects because it has no behaviour at all: like every
    Stage 4C tool it declares metadata and an argument schema and stops. There
    is no file access, no network call, no database write and no code
    execution, because there is no method through which any of those could
    happen.

    It exists so the framework's happy path is testable: a registered,
    enabled, low-risk tool that policy does not forbid, which is what makes
    the `ALLOWED` state reachable and therefore meaningful.
    """

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="echo",
            description="Returns its input unchanged. Framework test tool.",
            category=ToolCategory.DIAGNOSTIC,
            risk_level=RiskLevel.LOW,
            # The only registered tool that does not require approval, so the
            # ALLOWED branch of policy has something to demonstrate.
            requires_approval=False,
        )

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        return EchoArguments


# --- Executable workspace tools (Stage 4E) ----------------------------------
# The first three tools in Mai with an implementation behind them. Everything
# they can touch lives under the configured workspace root, and every one of
# them requires an explicit human approval bound to the exact payload.
#
# Their argument schemas are defined **here**, in the declaration layer, and
# imported by the executor. That is not tidiness: authorization validates a
# proposal against the declared schema, and the dispatcher validates the same
# arguments again before running. If those were two classes they could drift,
# and the gap between them would be a payload that passes authorization and
# then runs as something else. One class per tool makes the gap impossible.


class CreateTextFileArguments(ToolArguments):
    """Arguments for `create_text_file`."""

    path: str = Field(..., min_length=1, max_length=400)
    content: str = Field(..., max_length=100_000)
    #: Overwriting is a materially different action from creating, so it is a
    #: separate parameter -- and because it is part of the payload, an
    #: approval for `overwrite=False` does not approve `overwrite=True`.
    overwrite: bool = False


class ReadTextFileArguments(ToolArguments):
    """Arguments for `read_text_file`."""

    path: str = Field(..., min_length=1, max_length=400)


class ListWorkspaceFilesArguments(ToolArguments):
    """Arguments for `list_workspace_files`."""

    #: Optional subdirectory, itself resolved inside the workspace.
    path: Optional[str] = Field(default=None, max_length=400)


class WebSearchArguments(ToolArguments):
    """Arguments for `web_search`.

    A query and two bounded knobs. Note what is absent: no `url`, no
    `endpoint`, no `method`, no `headers`, no `api_key`. A user cannot choose
    a destination and cannot supply a credential, because there is no field
    for either -- the same "a model cannot set what it cannot name" reasoning
    the earlier stages used, applied to the network.
    """

    query: str = Field(..., min_length=1, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)
    #: Defaults to on. A research assistant has no reason to default to
    #: fewer filters, and turning it off is part of the approved payload.
    safe_search: bool = True
    #: Stage 5E.2. Whether the *application* judged this question to need
    #: current information, from `app.orchestration.freshness`.
    #:
    #: A boolean, not a date range and not a provider parameter name: the
    #: user approves a payload they can read, and "recent" is a thing a person
    #: can agree to in a way that `{"topic": "news", "days": 30}` is not. The
    #: integration decides how to express it, which also keeps provider
    #: vocabulary out of the approved payload.
    prefer_recent: bool = False


class CalendarReadIntent(str, enum.Enum):
    """Why the calendar is being read. A closed set, chosen by the application.

    Mirrors `app.orchestration.calendar_language.CalendarIntent`, and is
    deliberately a separate type: that one is what the recogniser produces,
    this one is what crosses the tool boundary. A test pins the two to the
    same member values, so they cannot drift apart in silence.
    """

    SCHEDULE = "calendar_schedule"
    AVAILABILITY = "calendar_availability"
    NEXT_EVENT = "calendar_next_event"


class CalendarListEventsArguments(ToolArguments):
    """Arguments for `calendar_list_events`.

    A time window and a count. Deliberately no calendar identifier, no query,
    no field selector and no URL: the integration builds the request, and
    there is nothing here through which a caller could reach a different
    endpoint or a different calendar.
    """

    #: RFC-3339 timestamps, computed by the application from the user's
    #: question -- never supplied by a model.
    starts_at: str = Field(..., min_length=10, max_length=40)
    ends_at: str = Field(..., min_length=10, max_length=40)
    max_results: int = Field(default=10, ge=1, le=25)

    #: What the answer is for, which decides how much of each event is kept.
    #:
    #: A closed set of application-chosen names, not free text: it selects
    #: between two renderings that already exist in the integration, and
    #: `calendar_availability` is the *narrower* one -- it sends interval
    #: times only, with no title, location or organiser. Widening the read is
    #: not among the things this field can do.
    #:
    #: It is part of the arguments, so it is part of the approval fingerprint
    #: and the audit record: a read approved as an availability check cannot
    #: be re-run as a full schedule read.
    intent: CalendarReadIntent = CalendarReadIntent.SCHEDULE

    #: The user's own words for the window -- "tomorrow afternoon".
    #:
    #: Echoed into the availability block so it names the period the user
    #: named. Bounded and flattened; it is the user's own text, so it is not
    #: a new source of anything, but it is not a place to put anything else
    #: either.
    window_label: str = Field(default="", max_length=60)


class _ExecutableDeclaration(Tool):
    """A declaration whose implementation lives in `app.execution.tools`.

    Still no `execute` method -- this class is the *description*, and Stage
    4C's guarantee that a `Tool` cannot be run is untouched. The executor is a
    separate object in a separate package, reached only by the dispatcher, and
    the two are matched by name.
    """

    _definition: ToolDefinition
    _arguments: Type[ToolArguments]

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        return self._arguments


def _declare_executable(
    name: str,
    description: str,
    risk_level: RiskLevel,
    arguments: Type[ToolArguments],
    category: ToolCategory = ToolCategory.FILE_OPERATION,
) -> Tool:
    """Declare a capability that Stage 4E can actually perform.

    `requires_approval=True` on every one, including the read-only tools.
    Reading is lower risk than writing, not zero risk -- it is the step that
    moves file contents into a prompt -- and Stage 4E's rule is that anything
    with a side effect is approved explicitly, per payload. The risk ladder
    still distinguishes them; the approval requirement does not.
    """
    tool = _ExecutableDeclaration()
    tool._definition = ToolDefinition(
        name=name,
        description=description,
        category=category,
        risk_level=risk_level,
        # Approval is retained for search, not waived for convenience. It is
        # read-only, but it is the one tool that sends what the user asked
        # about to someone else -- and a per-query approval is exactly where
        # a person gets to decide whether that is acceptable for this query.
        requires_approval=True,
        execution_mode=ExecutionMode.SYNCHRONOUS,
        enabled=True,
    )
    tool._arguments = arguments
    return tool


# --- Declared future capabilities -------------------------------------------
# Metadata only. None of these has an implementation, and each is refused or
# gated by policy today. They are registered so the risk ladder is exercised
# against realistic names rather than against invented test fixtures.


class _DeclaredTool(Tool):
    """A capability that is described but does not exist.

    Subclasses supply metadata. None supplies behaviour -- there is no method
    on `Tool` through which behaviour could be supplied.
    """

    _definition: ToolDefinition

    @property
    def definition(self) -> ToolDefinition:
        return self._definition


def _declare(
    name: str,
    description: str,
    category: ToolCategory,
    risk_level: RiskLevel,
    requires_approval: bool = True,
    enabled: bool = True,
) -> Tool:
    """Declare a capability that does not exist.

    `enabled` and `execution_mode` answer different questions, and Stage 4D
    is where the difference starts to matter:

    - `enabled` is the **operator's** switch: would we permit this capability?
    - `execution_mode` is the **application's** statement of fact: can it run?
      It is `unavailable` for every tool declared *here*, and a definition may
      only say otherwise where an executor genuinely exists -- which for these
      it does not.

    Stage 4C set `enabled=False` on all of these because nothing consumed the
    registry, so the switch had no meaning. Stage 4D introduces the consumer,
    and leaving every switch off would make `APPROVAL_REQUIRED` unreachable --
    collapsing a required outcome into a vestigial one and hiding the
    difference between "we do not permit this" and "a human would have to
    confirm it".

    So the operator switch now reflects a real position, and nothing about
    executability changed: every tool declared through this helper is still
    `unavailable`, still has no implementation, and still cannot run. Stage 4E
    did not change that -- it added three *separate* entries above with real
    executors, and deliberately left `future_send_email` and
    `future_delete_file` exactly as they were.
    """
    tool = _DeclaredTool()
    tool._definition = ToolDefinition(
        name=name,
        description=description,
        category=category,
        risk_level=risk_level,
        requires_approval=requires_approval,
        enabled=enabled,
    )
    return tool


def _declare_calendar_read() -> Tool:
    """Declare `calendar_list_events`. The one tool that needs no approval.

    Every other executable tool in this catalogue carries
    `requires_approval=True`, and this one deliberately does not. The
    reasoning, and the argument against it, both belong in the record:

    **For.** It is read-only and changes nothing, anywhere. The user granted
    read access explicitly, in Google's own consent screen, in a browser --
    a stronger and more informed act than typing "yes" in a chat. And a
    prompt after every "what's on my calendar?" would train someone to
    confirm without reading, which is how a confirmation stops being one.

    **Against.** A calendar read moves private personal data -- sometimes
    about third parties -- into a prompt sent to an external LLM provider.
    That is a real disclosure, and Stage 4F-D requires consent for sending a
    *search query* to a third party, which is less than this.

    **Resolution.** The disclosure is made once, at connection time, where it
    can be read properly: the connect flow states that calendar contents will
    be sent to the configured model provider to answer questions. Per-use
    approval is not required, but three other gates remain -- execution must
    be switched on, the integration must be connected, and the natural-
    language recogniser must actually identify a calendar question. The
    alternative, a prompt on every query, buys a click rather than a decision.

    Risk stays MEDIUM rather than LOW. It reads private data, and the ladder
    should say so even where approval does not.
    """
    tool = _ExecutableDeclaration()
    tool._definition = ToolDefinition(
        name="calendar_list_events",
        description="Read events from the user's Google Calendar. Read-only.",
        category=ToolCategory.INFORMATION,
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        execution_mode=ExecutionMode.SYNCHRONOUS,
        enabled=True,
    )
    tool._arguments = CalendarListEventsArguments
    return tool


class GmailListMessagesArguments(ToolArguments):
    """Arguments for `gmail_list_messages`.

    A typed, bounded description of *which* messages to look at. Deliberately
    no `q`, no `labelIds`, no `pageToken`, no `includeSpamTrash` and no URL:
    the integration renders Gmail's query language from these fields, so raw
    Gmail syntax has no way in. `extra="forbid"` means a caller adding one of
    those names is refused rather than ignored.
    """

    #: Part of a sender address or domain -- "netflix", "john@acme.com".
    sender: str = Field(default="", max_length=96)
    #: Words that must appear in the subject, and anywhere, respectively.
    subject_terms: Tuple[str, ...] = ()
    text_terms: Tuple[str, ...] = ()
    unread_only: bool = False
    newer_than_days: Optional[int] = Field(default=None, ge=1, le=30)
    max_results: int = Field(default=5, ge=1, le=10)
    #: How many of the returned messages to read the body of.
    #:
    #: Zero for "what came in today?", which needs only senders and subjects.
    #: The bodies of messages nobody asked about never cross the network.
    #:
    #: Bounded by `MAX_BODIES` rather than by a literal of its own. Two
    #: numbers for one property drift, and mutation testing showed which way:
    #: with a separate literal here, the integration's bound became
    #: unreachable and could be raised without any test noticing.
    body_count: int = Field(default=0, ge=0, le=GMAIL_MAX_BODIES)


class GmailGetMessageArguments(ToolArguments):
    """Arguments for `gmail_get_message`. One id, from a listing Mai made."""

    message_id: str = Field(..., min_length=1, max_length=128)


def _declare_gmail_read(name: str, description: str, arguments) -> Tool:
    """Declare one of the two Gmail reads.

    **`requires_approval=True`, unlike the calendar read.** The calendar's
    exemption was argued on the grounds that an event title is low-sensitivity
    and the question is asked often; neither transfers here.

    `gmail.readonly` is a Google *restricted* scope covering the whole
    mailbox, and a message body is the most sensitive personal data Mai
    touches -- frequently about third parties who never consented to anything.
    Sending it to an external model provider is a real disclosure, and Stage
    4F-D already requires consent for sending a *search query* to a third
    party, which is far less. So Gmail follows `web_search`: the user is told
    what will be read, and says yes.

    Risk is HIGH rather than MEDIUM for the same reason. Nothing is destroyed,
    but the ladder should reflect what is exposed, not only what is changed.
    """
    tool = _ExecutableDeclaration()
    tool._definition = ToolDefinition(
        name=name,
        description=description,
        category=ToolCategory.INFORMATION,
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
        execution_mode=ExecutionMode.SYNCHRONOUS,
        enabled=True,
    )
    tool._arguments = arguments
    return tool


def build_catalog(registry: Optional[ToolRegistry] = None) -> ToolRegistry:
    """Register every declared tool. Idempotent per registry instance.

    Called once at import time for the process registry, and callable with a
    fresh `ToolRegistry()` from tests so they never mutate module state.
    """
    target = registry if registry is not None else get_registry()

    target.register(EchoTool())

    for tool in (
        _declare_executable(
            "create_text_file",
            "Create or overwrite a text file inside the Mai workspace.",
            RiskLevel.MEDIUM,
            CreateTextFileArguments,
        ),
        _declare_executable(
            "read_text_file",
            "Read a text file from inside the Mai workspace.",
            RiskLevel.LOW,
            ReadTextFileArguments,
        ),
        _declare_executable(
            "list_workspace_files",
            "List files inside the Mai workspace.",
            RiskLevel.LOW,
            ListWorkspaceFilesArguments,
        ),
        # Stage 4F-G. Read-only, and the one tool that does not require
        # per-use approval -- see `_declare_calendar_read` for why.
        _declare_calendar_read(),
        # Stage 5B. Two reads, both requiring approval -- see
        # `_declare_gmail_read`. There is deliberately no third: no send, no
        # reply, no trash, no label, no draft, no modify.
        _declare_gmail_read(
            "gmail_list_messages",
            "List bounded metadata for the user's Gmail messages. Read-only.",
            GmailListMessagesArguments,
        ),
        _declare_gmail_read(
            "gmail_get_message",
            "Read one selected Gmail message, bounded. Read-only.",
            GmailGetMessageArguments,
        ),
        _declare_executable(
            "web_search",
            "Search the public web for a query and return sources.",
            # Read-only, and still MEDIUM rather than LOW: a search sends the
            # user's question to a third party, which the three filesystem
            # tools do not do. The risk is a privacy one, not a destruction
            # one, and the ladder should say so.
            RiskLevel.MEDIUM,
            WebSearchArguments,
            category=ToolCategory.INFORMATION,
        ),
    ):
        target.register(tool)

    for tool in (
        _declare(
            "future_web_search",
            "Search the public web. Not implemented.",
            ToolCategory.INFORMATION,
            RiskLevel.LOW,
        ),
        _declare(
            "future_generate_document",
            "Produce a document from structured input. Not implemented.",
            ToolCategory.CREATIVE,
            RiskLevel.MEDIUM,
        ),
        _declare(
            "future_send_email",
            "Send an email on the user's behalf. Not implemented.",
            ToolCategory.COMMUNICATION,
            RiskLevel.HIGH,
        ),
        _declare(
            "future_delete_file",
            "Delete a file from local storage. Not implemented.",
            ToolCategory.FILE_OPERATION,
            RiskLevel.CRITICAL,
            # Refused twice over: the operator switch is off *and* the risk
            # level is above the ceiling. Either alone would forbid it; both
            # is deliberate, so removing one does not quietly permit it.
            enabled=False,
        ),
    ):
        target.register(tool)

    return target


#: Build the process registry at import time.
build_catalog()


__all__ = [
    "CalendarListEventsArguments",
    "CreateTextFileArguments",
    "WebSearchArguments",
    "EchoArguments",
    "EchoTool",
    "ListWorkspaceFilesArguments",
    "ReadTextFileArguments",
    "build_catalog",
]
