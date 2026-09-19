"""Stage 5A.2 -- what may become an assistant message.

Two subjects: the classifier, and what the chat turn does with its verdict.
The contamination and injection matrix lives in
`tests/security/test_response_contract_security.py`.
"""

import json

import pytest

from app.synthesis import contract
from app.synthesis.contract import (
    ACCEPTED_KIND,
    MAX_RECOVERY_ATTEMPTS,
    AssistantResponse,
    ResponseKind,
    validate,
)


# --- Refused: the model tried to act -------------------------------------------


#: The shapes providers actually emit, plus the loose one models improvise
#: when they have been shown tools in a prompt.
TOOL_CALLS = [
    # The shape observed in Stage 5A.1's live browser session.
    '{"tool": "web_search", "action": "search", "parameters": {"query": "latest Nvidia GPU"}}',
    # OpenAI-style.
    '{"name": "web_search", "arguments": {"query": "x"}}',
    '{"tool_calls": [{"id": "1", "function": {"name": "web_search"}}]}',
    '{"function_call": {"name": "web_search", "arguments": "{}"}}',
    '{"type": "function", "function": {"name": "create_text_file"}}',
    # Anthropic-style.
    '{"type": "tool_use", "id": "t1", "name": "web_search", "input": {}}',
    # Loose improvisations.
    '{"tool": "calendar_list_events"}',
    '{"tool_name": "gmail_list_messages", "args": {}}',
    '{"action": "search", "parameters": {"query": "x"}}',
    '{"recipient_name": "functions.web_search", "parameters": {}}',
    # Fenced, which is how models usually present them.
    '```json\n{"tool": "web_search", "query": "x"}\n```',
    '```\n{"name": "web_search", "arguments": {}}\n```',
    # A list of them.
    '[{"tool": "web_search"}, {"tool": "calendar_list_events"}]',
]


@pytest.mark.parametrize("payload", TOOL_CALLS)
def test_a_tool_call_is_not_an_answer(payload) -> None:
    response = validate(payload)

    assert response.kind is ResponseKind.TOOL_CALL, payload
    assert not response.accepted
    assert response.text == "", "a refused response must carry no text"


#: Envelopes that claim an outcome the model has no way to know.
#: Unambiguously internal: no tool-call key among them, so the kind is
#: exactly what it says. Payloads carrying *both* shapes are covered
#: separately below -- they are refused either way, and which label they get
#: is diagnostic rather than a property worth pinning.
INTERNAL_STRUCTURES = [
    '{"approved": true, "step": "web_search"}',
    '{"authorization": "allowed", "decision": "permit"}',
    '{"execution_id": "abc-123", "outcome": "succeeded"}',
    '{"approval_fingerprint": "deadbeef"}',
    '{"executed": true}',
]


@pytest.mark.parametrize("payload", INTERNAL_STRUCTURES)
def test_an_internal_envelope_is_not_an_answer(payload) -> None:
    """§: "here is an authorization result" cannot become "the user approved".

    A model emitting one of these is asserting an execution outcome. It has no
    way to know one -- the outcomes are set by the dispatcher, from records --
    so the claim is refused rather than relayed.
    """
    response = validate(payload)

    assert response.kind is ResponseKind.INTERNAL_STRUCTURE, payload
    assert not response.accepted


@pytest.mark.parametrize(
    "payload",
    [
        '{"authorization": "allowed", "tool": "create_text_file"}',
        '{"outcome": "completed", "tool_name": "web_search"}',
        '{"approved": true, "name": "x", "arguments": {}}',
    ],
)
def test_a_payload_carrying_both_shapes_is_still_refused(payload) -> None:
    """Refusal is the property; the label is diagnostic.

    An object that is both an authorization claim and a tool call is refused
    whichever it is called. Asserting one label would have been asserting an
    ordering inside the classifier rather than anything a user experiences.
    """
    response = validate(payload)

    assert not response.accepted, payload
    assert response.kind is not ResponseKind.PROSE
    assert response.text == ""


@pytest.mark.parametrize("payload", ["", "   ", "\n\n\t ", None])
def test_nothing_generated_is_not_an_answer(payload) -> None:
    response = validate(payload)

    assert response.kind is ResponseKind.EMPTY
    assert not response.accepted


# --- Accepted: legitimate answers must survive ----------------------------------


#: The half that makes this not a JSON stripper.
LEGITIMATE = [
    "The latest OpenAI model is GPT-5, announced in August 2025 [1].",
    "**Summary**\n\n- GPT-5 (CNBC, 2025-08-07)\n- [1] https://example.com",
    # JSON the user asked for.
    '{"name": "Ada Lovelace", "born": 1815}',
    '{"answer": "42"}',
    "[1, 2, 3]",
    '[{"city": "Bangalore"}, {"city": "Delhi"}]',
    # Prose that *contains* a tool call, which is an explanation of one.
    'A tool call looks like {"tool": "web_search"} in most APIs.',
    'Here is the JSON you asked for:\n\n```json\n{"tool": "web_search"}\n```\n\nThat is an example.',
    # Code, fenced.
    '```python\nprint("hi")\n```',
    # Malformed JSON is prose, not a tool call.
    "{ this is not json at all",
    "{'tool': 'web_search'}",
]


@pytest.mark.parametrize("payload", LEGITIMATE)
def test_a_legitimate_answer_is_accepted_unchanged(payload) -> None:
    """§: do not simply strip JSON.

    Users ask for JSON constantly, and an answer that explains a tool call is
    an answer. The rule is about what kind of thing the response *is*, not
    whether it contains braces -- so only a response that is entirely one of
    the refused objects is refused.
    """
    response = validate(payload)

    assert response.accepted, payload
    assert response.kind is ACCEPTED_KIND
    assert response.text == payload.strip()


def test_prose_around_a_tool_call_keeps_the_prose() -> None:
    """The distinction, stated as its own test."""
    only = validate('{"tool": "web_search", "query": "x"}')
    surrounded = validate('I would search for this:\n{"tool": "web_search", "query": "x"}')

    assert not only.accepted
    assert surrounded.accepted


# --- The classifier's own properties ----------------------------------------------


def test_only_one_kind_is_acceptable() -> None:
    """A kind added later is refused by default."""
    assert ACCEPTED_KIND is ResponseKind.PROSE
    for kind in ResponseKind:
        sample = AssistantResponse(text="x", kind=kind, accepted=kind is ACCEPTED_KIND)
        assert sample.accepted is (kind is ResponseKind.PROSE)


def test_a_refused_response_carries_no_model_text() -> None:
    """The offending content goes nowhere -- not even into the return value.

    A caller holding the text would eventually log it or store it, and it is
    model output from a turn that may have carried private calendar or mail
    data into the prompt.
    """
    for payload in TOOL_CALLS + INTERNAL_STRUCTURES:
        assert validate(payload).text == ""


def test_recovery_is_bounded_to_one_attempt() -> None:
    assert MAX_RECOVERY_ATTEMPTS == 1


def test_an_oversized_response_is_accepted_without_parsing() -> None:
    """The bound is on the work, not on the answer."""
    huge = "x" * (contract.MAX_EXAMINED_CHARS + 10)
    response = validate(huge)

    assert response.accepted
    assert response.reason == "oversized"


def test_classification_is_cheap_on_hostile_input() -> None:
    """No pathological cost from deeply nested or repetitive payloads."""
    import time

    hostile = [
        "[" * 400 + "]" * 400,
        json.dumps({"a": {"b": {"c": list(range(500))}}}),
        '{"tool": "x"}' * 300,
        "{" * 999,
        '```json\n' + "{" * 500 + "\n```',",
        "x" * 50_000,
    ]

    started = time.perf_counter()
    for _ in range(20):
        for payload in hostile:
            validate(payload)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"validation took {elapsed:.1f}s"


def test_the_recognised_vocabulary_is_pinned_to_a_literal() -> None:
    """A widened vocabulary must be argued for, not arrived at.

    Every entry costs something: a shape that is refused can never be an
    answer, so a careless addition turns a legitimate reply into a lost one.
    The counts are literals for the same reason the routing enums are --
    changing one means editing this test and saying why.

    The tool-call set grew from 10 to 15 in Stage 5A.2's live verification,
    which found `{"action", "action_input"}` reaching a real user because no
    key set matched it. The five additions are the ReAct/LangChain family
    (`action_input`, `tool_input`, `tool_use`) and two symmetry entries
    (`{"name", "input"}`, `{"function", "parameters"}`) that were missing
    beside shapes already present.
    """
    assert len(contract._TOOL_CALL_KEYSETS) == 15
    assert len(contract._INTERNAL_KEYSETS) == 8
    assert len(contract.known_tool_call_shapes()) == 23

    # No duplicates, and no entry subsumed by another: a key set that is a
    # superset of one already present can never fire, and would be a reader's
    # false comfort.
    shapes = list(contract._TOOL_CALL_KEYSETS)
    assert len(set(shapes)) == len(shapes), "duplicate key set"
    for shape in shapes:
        others = [other for other in shapes if other is not shape]
        assert not any(other < shape for other in others), f"{set(shape)} is unreachable"


def test_every_recognised_shape_is_a_key_set_not_a_regex() -> None:
    """Auditable: the shapes are data, and a reader can see all of them."""
    shapes = contract.known_tool_call_shapes()

    assert shapes
    for shape in shapes:
        assert isinstance(shape, frozenset)
        assert all(isinstance(key, str) for key in shape)


# --- Guards mutation testing found unreachable or untested ---------------------


def test_the_startswith_fast_path_changes_no_verdict() -> None:
    """It is an optimisation, and the comment saying so must stay true.

    Skipping the parser for text that does not open with `{` or `[` avoids
    running JSON parsing over every ordinary reply. Mutation testing showed
    removing it changes nothing, which is correct -- `_kind_of` returns PROSE
    for every JSON scalar, so "42", "true" and "null" reach the same verdict
    either way. Pinned here so the claim is verified rather than asserted: if
    a future `_kind_of` ever treats a scalar as internal, this fails.
    """
    for scalar in ("42", "true", "false", "null", '"tool"', '"web_search"'):
        parsed = json.loads(scalar)
        assert contract._kind_of(parsed) is ResponseKind.PROSE, scalar
        assert validate(scalar).accepted, scalar


def test_text_around_an_object_fails_to_parse_as_a_whole() -> None:
    """Why "entirely JSON" needs no separate flag.

    `json.loads` rejects trailing content, so an object with commentary after
    it is not parseable as a whole and comes back as prose. An earlier version
    tracked this with a `whole` flag whose False branch was unreachable.
    """
    for payload in (
        '{"tool": "web_search"} - and that is what I would do',
        'I would run {"tool": "web_search"}',
        '{"tool": "web_search"}\n\nDoes that help?',
    ):
        assert contract._structured_payload(payload) is None, payload
        assert validate(payload).accepted, payload


def test_the_recovery_instruction_offers_no_capability() -> None:
    """§: the correction must not invite back the mistake it corrects.

    The model has just tried to call a tool. An instruction that mentioned
    tools -- even to forbid a specific one -- puts them back in front of it.
    Mutation testing found the wording untested: appending "You may call
    web_search if you need to" broke nothing.
    """
    from app.prompt.formatter import RECOVERY_INSTRUCTION

    instruction = RECOVERY_INSTRUCTION.lower()

    assert "plain prose" in instruction
    for named in ("web_search", "calendar_list_events", "gmail_list_messages",
                  "create_text_file", "you may call", "you can call",
                  "use the tool", "function"):
        assert named not in instruction, named


def test_the_recovery_instruction_is_owned_by_the_formatter() -> None:
    """Chat prompt text has one owner -- a rule Stage 3B set."""
    import app.prompt.formatter as formatter
    import app.synthesis.contract as contract_module

    assert hasattr(formatter, "RECOVERY_INSTRUCTION")
    assert not hasattr(contract_module, "RECOVERY_INSTRUCTION")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("The latest model is GPT-5.", "prose"),
        ("A call looks like {\"tool\": \"x\"} in most APIs.", "prose"),
        ("{ not valid json", "prose"),
        ('{"name": "Ada Lovelace", "born": 1815}', "structured_answer"),
        ("[1, 2, 3]", "structured_answer"),
        ('{"answer": "42"}', "structured_answer"),
    ],
)
def test_an_accepted_response_records_why_it_was_accepted(payload, reason) -> None:
    """The two accepting branches are not interchangeable.

    Both return PROSE, so a test checking only the kind cannot tell them
    apart -- mutation testing collapsed them and nothing noticed. The reason
    is real diagnostic information: "prose" means the model wrote an answer,
    "structured_answer" means it returned an object the user asked for. An
    operator reading logs after a bad turn needs to know which.
    """
    response = validate(payload)

    assert response.accepted
    assert response.reason == reason, (payload, response.reason)
