"""What may become an assistant message, and what may not.

Mai holds several internal representations at once -- tool calls, action
proposals, execution records, workflow results, research results, provider
envelopes -- and exactly one of them is the thing a person reads. This module
is the boundary between the two.

The failure it exists for
------------------------

The synthesis model sometimes answers a research turn with this:

    {
      "tool": "web_search",
      "action": "search",
      "parameters": {"query": "latest Nvidia GPU"}
    }

It is not an answer. It is the model trying to call a tool at a point where
the application wanted prose -- and before this module existed it was stored
verbatim as the assistant's message. Two things followed. The user saw JSON
where an answer should be. And the blob entered conversation history, so on
the next turn the model read its own output as an example of how Mai replies
and produced another one. A single malformed response became a pattern.

Not a JSON stripper
-------------------

The naive fix -- delete anything that looks like JSON -- would break every
legitimate answer that contains JSON, and users ask for JSON constantly. What
matters is not whether the text contains braces but **what kind of thing the
response is**:

    prose                a reply. Store it.
    tool call            the model tried to act. Never an answer.
    internal structure   an execution or authorization envelope. Never an answer.
    empty                nothing was generated. Not the same as "nothing found".

So a response is refused only when it is *entirely* one of those objects, with
no prose around it. An answer that explains a tool call and shows one is an
answer; an answer that is nothing but the call is not.

It grants nothing
-----------------

Classifying a response as a tool call does **not** run the tool. There is no
path from this module to the dispatcher, the integrations, or any registry,
and a test asserts it. A model that emits `{"tool": "create_text_file",
"arguments": {"path": "secrets.txt"}}` has written a string; what it gets is a
rejection, not a file.
"""

import enum
import json
import re
from typing import Any, Dict, NamedTuple, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Longest response examined. Beyond it, the text is treated as prose without
#: parsing: a megabyte of JSON is not a tool call anyone meant to make, and
#: parsing it would be work with no decision at the end.
MAX_EXAMINED_CHARS = 200_000

#: Most recovery attempts after a refused response. Exactly one.
#:
#: A second would double the cost of every bad turn for a model that has
#: already demonstrated it is not following the contract, and an unbounded
#: loop is a denial of service triggered by the model's own output.
MAX_RECOVERY_ATTEMPTS = 1


class ResponseKind(str, enum.Enum):
    """What the model actually produced."""

    #: A reply. The only kind that may become an assistant message.
    PROSE = "prose"
    #: An attempt to invoke a tool where prose was required.
    TOOL_CALL = "tool_call"
    #: An execution, authorization or approval envelope.
    INTERNAL_STRUCTURE = "internal_structure"
    #: Nothing usable was generated.
    EMPTY = "empty"


#: The one kind that may cross into conversation history.
#:
#: A single member rather than "not in REFUSED", so a kind added later is
#: refused by default -- the same reasoning `ExternalResultState` uses.
ACCEPTED_KIND = ResponseKind.PROSE


class AssistantResponse(NamedTuple):
    """The canonical assistant reply, and whether it may be stored.

    `text` is meaningful only when `accepted`. A refused response carries the
    offending content nowhere: the caller receives the kind and a reason, and
    writes its own truthful message.
    """

    text: str = ""
    kind: ResponseKind = ResponseKind.EMPTY
    accepted: bool = False
    #: A short application reason code. Never the model's output.
    reason: str = ""

    @property
    def is_tool_call(self) -> bool:
        return self.kind is ResponseKind.TOOL_CALL


# --- Shapes ------------------------------------------------------------------

#: Keys that mark an object as an attempt to call a tool.
#:
#: Drawn from the shapes providers actually emit, so the check is not specific
#: to one of them: OpenAI-style `{"name", "arguments"}` and `{"tool_calls"}`,
#: Anthropic-style `{"type": "tool_use"}` and its `{"name", "input"}` body,
#: the ReAct/LangChain `{"action", "action_input"}` convention, and the loose
#: `{"tool", "query"}` shape models improvise when they have been shown tools
#: in a prompt.
#:
#: The vocabulary is closed and grows only on evidence. Two of these families
#: are here because this system emitted them: `{"tool", "action",
#: "parameters"}` is the blob Stage 5A.2 was opened to contain, and
#: `{"action", "action_input"}` was observed in live verification *after* the
#: contract was in place -- it passed as an answer because no keyset matched.
#: `tests/security/test_response_contract_security.py` keeps both as a
#: regression corpus, so neither shape can be lost to a later edit.
#:
#: Single-key entries are deliberate. `action_input`, `tool_input`, `tool` and
#: `tool_name` are not fields a person asks for in an answer; they exist only
#: to name a call, so their presence alone is the signal.
_TOOL_CALL_KEYSETS: Tuple[frozenset, ...] = (
    frozenset({"tool"}),
    frozenset({"tool_name"}),
    frozenset({"tool_calls"}),
    frozenset({"tool_use"}),
    frozenset({"tool_input"}),
    frozenset({"action_input"}),
    frozenset({"function_call"}),
    frozenset({"name", "arguments"}),
    frozenset({"name", "parameters"}),
    frozenset({"name", "input"}),
    frozenset({"function", "arguments"}),
    frozenset({"function", "parameters"}),
    frozenset({"action", "parameters"}),
    frozenset({"action", "arguments"}),
    frozenset({"recipient_name", "parameters"}),
)

#: Values of a `type` field that mark a tool call whatever else is present.
_TOOL_CALL_TYPES = frozenset({"function", "tool_use", "tool_call"})

#: Keys that mark an object as an internal record rather than an answer.
#:
#: An execution, an approval or an authorization decision. A model emitting
#: one is claiming an outcome it has no way to know, which is the Stage 4E.1
#: failure -- so it is refused for the same reason a tool call is.
_INTERNAL_KEYSETS: Tuple[frozenset, ...] = (
    frozenset({"approved"}),
    frozenset({"authorization"}),
    frozenset({"authorized"}),
    frozenset({"execution_id"}),
    frozenset({"approval_fingerprint"}),
    frozenset({"fingerprint", "tool"}),
    frozenset({"outcome", "tool_name"}),
    frozenset({"executed"}),
)

#: A response that is nothing but one fenced block.
_ONLY_FENCE = re.compile(
    r"\A\s*```(?:json|JSON|javascript|js)?\s*\n?(?P<body>.*?)\n?\s*```\s*\Z",
    re.DOTALL,
)


def validate(content: Optional[str]) -> AssistantResponse:
    """Decide whether this model output may become an assistant message.

    Never raises. The default is refusal: a response this module cannot
    understand is not stored, because the cost of storing the wrong thing is a
    poisoned conversation and the cost of refusing is one honest sentence.
    """
    if content is None:
        return AssistantResponse(kind=ResponseKind.EMPTY, reason="no_content")

    text = content.strip()
    if not text:
        return AssistantResponse(kind=ResponseKind.EMPTY, reason="blank")

    if len(text) > MAX_EXAMINED_CHARS:
        # Too large to be a tool call anyone meant. Accepted as prose rather
        # than parsed -- the bound is on the work, not on the answer.
        return AssistantResponse(
            text=text, kind=ResponseKind.PROSE, accepted=True, reason="oversized"
        )

    payload = _structured_payload(text)
    if payload is None:
        # Not structured, or prose that merely *contains* something
        # structured. Both are answers -- an explanation with a JSON example
        # in it is exactly what a person would want.
        return AssistantResponse(
            text=text, kind=ResponseKind.PROSE, accepted=True, reason="prose"
        )

    kind = _kind_of(payload)
    if kind is ResponseKind.PROSE:
        # Structured, and structured as nothing Mai recognises -- a list, or
        # an object the user asked for. Their answer, in the shape they asked
        # for it.
        return AssistantResponse(
            text=text, kind=ResponseKind.PROSE, accepted=True, reason="structured_answer"
        )

    logger.warning(
        "Refused a model response that was not an answer",
        # The kind and a length. Never the content: it is model output on a
        # turn that may have carried private calendar or mail data into the
        # prompt, and a rejected response is exactly the thing not to copy
        # into a log line.
        extra={"kind": kind.value, "response_chars": len(text)},
    )
    return AssistantResponse(kind=kind, accepted=False, reason=kind.value)


def _structured_payload(text: str) -> Optional[Any]:
    """Parse the response as JSON if it is *entirely* JSON, else None.

    "Entirely" is what makes this not a JSON stripper, and `json.loads` gives
    it for free: text with anything before or after the object fails to parse
    as a whole, so an explanation containing a tool-call example comes back
    None and is treated as the answer it is.

    An earlier version also returned a `whole` flag to express that. It was
    dead -- every path that set it False also returned None -- and mutation
    testing found the branch unreachable. Removed rather than kept:
    unreachable code advertises a distinction that is not operating.
    """
    candidate = text
    fenced = _ONLY_FENCE.match(text)
    if fenced:
        candidate = (fenced.group("body") or "").strip()

    # A fast path, not a guard. `_kind_of` returns PROSE for every JSON scalar,
    # so parsing "42" or "true" would reach the same verdict -- this just
    # avoids running the parser over every ordinary prose reply. A test pins
    # the equivalence so the comment cannot rot into a false claim.
    if not candidate.startswith(("{", "[")):
        return None

    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return None


def _kind_of(payload: Any) -> ResponseKind:
    """Classify a parsed payload. Prose unless it is recognisably internal."""
    if isinstance(payload, list):
        # A list of tool calls is still a tool call. A list of anything else
        # is an answer the user asked for.
        if payload and all(
            isinstance(item, dict) and _kind_of(item) is not ResponseKind.PROSE
            for item in payload
        ):
            return ResponseKind.TOOL_CALL
        return ResponseKind.PROSE

    if not isinstance(payload, dict):
        return ResponseKind.PROSE

    keys = {str(key).lower() for key in payload}

    declared = payload.get("type")
    if isinstance(declared, str) and declared.lower() in _TOOL_CALL_TYPES:
        return ResponseKind.TOOL_CALL

    for keyset in _TOOL_CALL_KEYSETS:
        if keyset <= keys:
            return ResponseKind.TOOL_CALL

    for keyset in _INTERNAL_KEYSETS:
        if keyset <= keys:
            return ResponseKind.INTERNAL_STRUCTURE

    return ResponseKind.PROSE


def known_tool_call_shapes() -> Tuple[frozenset, ...]:
    """The key sets this module recognises. For tests and for auditing."""
    return _TOOL_CALL_KEYSETS + _INTERNAL_KEYSETS


__all__ = [
    "ACCEPTED_KIND",
    "MAX_EXAMINED_CHARS",
    "MAX_RECOVERY_ATTEMPTS",
    "AssistantResponse",
    "ResponseKind",
    "known_tool_call_shapes",
    "validate",
]
