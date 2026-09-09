"""Stage 4C: tool definitions and the registry.

The registry decides what exists. These tests cover the two properties that
makes worth having: it is closed (nothing outside application code can add to
it) and it is honest (what it hands out cannot be edited into something else).
"""

import pytest
from pydantic import ValidationError

from app.tools.base import ArgumentValidationError, Tool, ToolArguments
from app.tools.catalog import EchoArguments, EchoTool, build_catalog
from app.tools.registry import DuplicateToolError, ToolRegistry, get_registry
from app.tools.schemas import (
    ExecutionMode,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
    risk_rank,
)


@pytest.fixture
def registry() -> ToolRegistry:
    """A fresh registry, so tests never mutate process state."""
    return build_catalog(ToolRegistry())


def definition(**overrides) -> ToolDefinition:
    payload = {
        "name": "sample",
        "description": "A sample tool.",
        "category": ToolCategory.DIAGNOSTIC,
        "risk_level": RiskLevel.LOW,
    }
    payload.update(overrides)
    return ToolDefinition(**payload)


class _Sample(Tool):
    def __init__(self, **overrides) -> None:
        self._definition = definition(**overrides)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition


# --- Definitions ------------------------------------------------------------


@pytest.mark.parametrize("category", list(ToolCategory))
def test_every_category_is_accepted(category) -> None:
    assert definition(category=category).category is category


@pytest.mark.parametrize(
    "category", ["shell", "arbitrary", "", None, 1, "INFORMATION "]
)
def test_an_invalid_category_is_rejected(category) -> None:
    with pytest.raises(ValidationError):
        definition(category=category)


@pytest.mark.parametrize("risk", list(RiskLevel))
def test_every_risk_level_is_accepted(risk) -> None:
    assert definition(risk_level=risk).risk_level is risk


@pytest.mark.parametrize("risk", ["none", "safe", "", None, 0, "LOW "])
def test_an_invalid_risk_level_is_rejected(risk) -> None:
    with pytest.raises(ValidationError):
        definition(risk_level=risk)


def test_risk_levels_are_totally_ordered() -> None:
    """Policy compares them, so the order must be well defined."""
    ranks = [risk_rank(level) for level in RiskLevel]
    assert ranks == sorted(ranks)
    assert risk_rank(RiskLevel.LOW) < risk_rank(RiskLevel.CRITICAL)


def test_names_are_canonicalised() -> None:
    assert definition(name="  Future_Web_Search  ").name == "future_web_search"


@pytest.mark.parametrize(
    "name",
    ["", "   ", "has spaces", "semi;colon", "slash/name", "dot.name",
     "quote'name", "<script>", "a" * 65, "rm -rf /"],
)
def test_an_invalid_tool_name_is_rejected(name) -> None:
    with pytest.raises(ValidationError):
        definition(name=name)


def test_a_definition_is_frozen() -> None:
    """Registry metadata cannot be edited through a handle to it."""
    item = definition()
    for field, value in (
        ("risk_level", RiskLevel.LOW),
        ("requires_approval", False),
        ("description", "something else"),
        ("category", ToolCategory.SYSTEM),
        ("enabled", True),
    ):
        with pytest.raises(ValidationError):
            setattr(item, field, value)


def test_a_definition_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ToolDefinition(
            name="x", description="y", category=ToolCategory.DIAGNOSTIC,
            risk_level=RiskLevel.LOW, approved=True, bypass_policy=True,
        )


def test_a_tool_may_not_declare_a_mode_the_application_cannot_honour() -> None:
    """The structural half of the execution guarantee, in the data.

    Stage 4C's version of this test asserted that *no* mode but UNAVAILABLE
    was accepted, because nothing could run. Stage 4E built a synchronous
    executor, so SYNCHRONOUS became true and the assertion narrowed to what is
    still false.

    BACKGROUND is still refused, and that is the part worth keeping: it
    describes an action running with nobody waiting on it, which is an
    autonomous loop by another name. Stage 4E forbids those, so no definition
    may claim one.
    """
    assert definition().execution_mode is ExecutionMode.UNAVAILABLE
    assert (
        definition(execution_mode=ExecutionMode.SYNCHRONOUS).execution_mode
        is ExecutionMode.SYNCHRONOUS
    )
    with pytest.raises(ValidationError):
        definition(execution_mode=ExecutionMode.BACKGROUND)


def test_approval_defaults_to_required() -> None:
    """The safe default. A tool must opt *out*, in application code."""
    assert definition().requires_approval is True


# --- Registry ---------------------------------------------------------------


def test_a_tool_can_be_registered_and_retrieved() -> None:
    registry = ToolRegistry()
    registry.register(_Sample(name="alpha"))

    assert registry.contains("alpha")
    assert registry.get("alpha").definition.name == "alpha"
    assert len(registry) == 1


def test_duplicate_registration_is_rejected() -> None:
    """Refused, not overwritten: silent replacement is how a low-risk
    declaration takes over a high-risk name."""
    registry = ToolRegistry()
    registry.register(_Sample(name="alpha", risk_level=RiskLevel.CRITICAL))

    with pytest.raises(DuplicateToolError):
        registry.register(_Sample(name="alpha", risk_level=RiskLevel.LOW))

    assert registry.definition("alpha").risk_level is RiskLevel.CRITICAL


def test_an_unknown_tool_returns_nothing() -> None:
    registry = ToolRegistry()
    assert registry.get("nope") is None
    assert registry.definition("nope") is None
    assert registry.contains("nope") is False


@pytest.mark.parametrize(
    "lookup", ["echo", "ECHO", "  echo  ", "Echo", "\techo\n"]
)
def test_lookup_normalises_case_and_whitespace(registry, lookup) -> None:
    """The only normalisation performed; neither can change which tool is meant."""
    assert registry.get(lookup).definition.name == "echo"


@pytest.mark.parametrize(
    "lookup",
    ["ech", "echo2", "echoo", "ec ho", "e-c-h-o", "echo_tool", "future_delete",
     "delete_all_files", "send_email",
     # `web_search` became real in Stage 4F-B, so it moved out of this list.
     # Its near-misses replace it: the property being protected is that
     # adjacency never resolves, and a registered name makes that testable
     # from both sides.
     "websearch", "web-search", "search_web", "web_search2", "web_searches"],
)
def test_lookup_is_never_fuzzy(registry, lookup) -> None:
    """`delete_all_files` must not find `future_delete_file`."""
    assert registry.get(lookup) is None


def test_listing_is_deterministic(registry) -> None:
    assert registry.list_registered() == registry.list_registered()
    assert registry.names() == tuple(sorted(registry.names()))


def test_listing_exposes_no_mutable_internal_collection(registry) -> None:
    """A caller cannot change the registry by changing what it was handed."""
    listed = registry.list_registered()
    assert isinstance(listed, tuple)

    with pytest.raises(AttributeError):
        listed.append(definition())

    before = registry.names()
    with pytest.raises(ValidationError):
        listed[0].risk_level = RiskLevel.LOW
    assert registry.names() == before


def test_mutating_a_retrieved_definition_cannot_change_the_registry(
    registry,
) -> None:
    retrieved = registry.definition("future_send_email")
    assert retrieved.risk_level is RiskLevel.HIGH

    with pytest.raises(ValidationError):
        retrieved.risk_level = RiskLevel.LOW
    with pytest.raises(ValidationError):
        retrieved.requires_approval = False
    with pytest.raises(ValidationError):
        retrieved.execution_mode = ExecutionMode.SYNCHRONOUS

    fresh = registry.definition("future_send_email")
    assert fresh.risk_level is RiskLevel.HIGH
    assert fresh.requires_approval is True
    assert fresh.execution_mode is ExecutionMode.UNAVAILABLE


# --- The catalogue ----------------------------------------------------------


def test_the_catalogue_registers_the_expected_tools(registry) -> None:
    """Exact, so a tool cannot appear without this list being updated."""
    assert registry.names() == (
        "calendar_list_events",
        "create_text_file",
        "echo",
        "future_delete_file",
        "future_generate_document",
        "future_send_email",
        "future_web_search",
        "list_workspace_files",
        "read_text_file",
        "web_search",
    )


def test_only_the_expected_tools_are_executable(registry) -> None:
    """Stage 4E added three; Stage 4F-B added a fourth, and named it.

    The pair of assertions matters more than either alone: the first pins the
    set, the second pins the complement. Adding a fourth executable tool fails
    this test, which is the point -- executability is not something a future
    edit should be able to acquire quietly.
    """
    executable = tuple(
        name
        for name in registry.names()
        if registry.definition(name).execution_mode is not ExecutionMode.UNAVAILABLE
    )
    assert executable == (
        "calendar_list_events", "create_text_file", "list_workspace_files",
        "read_text_file", "web_search",
    )

    for name in registry.names():
        if name in executable:
            continue
        assert (
            registry.definition(name).execution_mode is ExecutionMode.UNAVAILABLE
        ), name


def test_every_executable_tool_requires_approval(registry) -> None:
    """Including the read-only ones. Reading is lower risk, not no risk.

    `web_search` retains approval too, and that was a decision rather than an
    oversight: it is read-only, but it is the one tool that sends what the
    user asked about to a third party. A per-query approval is exactly where
    a person gets to decide whether that is acceptable for this query.
    """
    for name in (
        "create_text_file", "read_text_file", "list_workspace_files", "web_search",
    ):
        assert registry.definition(name).requires_approval is True, name


def test_the_dangerous_declared_tools_were_left_alone(registry) -> None:
    """Stage 4E built an executor and pointed it at nothing dangerous.

    `future_send_email` and `future_delete_file` were the two capabilities the
    stage specification named as forbidden. Neither gained an execution mode,
    and the critical one is still disabled outright.
    """
    email = registry.definition("future_send_email")
    delete = registry.definition("future_delete_file")

    assert email.execution_mode is ExecutionMode.UNAVAILABLE
    assert delete.execution_mode is ExecutionMode.UNAVAILABLE
    assert delete.enabled is False


def test_every_declared_future_tool_still_requires_approval(registry) -> None:
    """None is an implementation, so none may pass without a human.

    Stage 4D turned the operator switch on for three of these so that
    `APPROVAL_REQUIRED` is reachable. `enabled` and `execution_mode` answer
    different questions: the first is "would we permit this?", the second is
    "can it run?". Only the first changed.
    """
    for name in registry.names():
        if not name.startswith("future_"):
            continue
        definition_ = registry.definition(name)
        assert definition_.requires_approval is True, name
        assert definition_.execution_mode is ExecutionMode.UNAVAILABLE, name


def test_the_critical_tool_is_refused_twice_over(registry) -> None:
    """Belt and braces: removing either guard does not quietly permit it."""
    definition_ = registry.definition("future_delete_file")
    assert definition_.enabled is False
    assert definition_.risk_level is RiskLevel.CRITICAL


#: Every tool permitted to skip approval, and why.
#:
#: An enabled tool that also opts out of approval is one nothing stops once
#: execution is switched on, so the set is enumerated rather than described by
#: a rule. Stage 4F-G added the second entry, and that was a deliberate
#: decision with a documented argument against it -- see
#: `app.tools.catalog._declare_calendar_read`.
APPROVAL_FREE_TOOLS = {
    "echo": "inert framework tool; performs nothing at all",
    "calendar_list_events": (
        "read-only, and the user granted access in Google's own consent "
        "screen; the disclosure is made once at connection time"
    ),
}


def test_only_the_enumerated_tools_may_skip_approval(registry) -> None:
    """The invariant that keeps the operator switch safe.

    Previously "only a DIAGNOSTIC tool may skip approval", which held while
    `echo` was the only one. Stage 4F-G added a read-only INFORMATION tool
    that also skips it, so the rule became a list -- and a list is the
    stronger form here: a new approval-free tool now fails this test until
    someone writes down why it should be one.
    """
    skipping = {
        name for name in registry.names()
        if registry.definition(name).enabled
        and not registry.definition(name).requires_approval
    }

    assert skipping == set(APPROVAL_FREE_TOOLS), skipping


def test_every_approval_free_tool_is_read_only(registry) -> None:
    """Whatever else it does, it must not change anything.

    The reason approval can be skipped at all. A tool that both writes and
    skips approval would be reachable from a chat message with no gate but
    the recogniser.
    """
    for name in APPROVAL_FREE_TOOLS:
        definition_ = registry.definition(name)
        assert definition_.risk_level in (RiskLevel.LOW, RiskLevel.MEDIUM), name
        # No write-shaped tool is in the set, by name or by category.
        assert definition_.category is not ToolCategory.FILE_OPERATION, name
        assert definition_.category is not ToolCategory.COMMUNICATION, name


def test_no_registered_tool_claims_background_execution(registry) -> None:
    """No tool runs unattended, whatever else it may do."""
    for name in registry.names():
        assert (
            registry.definition(name).execution_mode is not ExecutionMode.BACKGROUND
        ), name


def test_the_process_registry_is_populated() -> None:
    from app.tools import catalog  # noqa: F401  (import for registration)

    assert "echo" in get_registry().names()


# --- Argument validation ----------------------------------------------------


def test_valid_arguments_are_accepted() -> None:
    assert EchoTool().validate_arguments({"text": "hello"}) == {"text": "hello"}


@pytest.mark.parametrize(
    "arguments",
    [
        {},                                   # missing required
        {"text": ""},                         # too short
        {"text": "A" * 501},                  # too long
        {"text": 123},                        # wrong type
        {"text": None},
        {"text": ["a"]},
        {"message": "hello"},                 # wrong field name
    ],
)
def test_invalid_arguments_are_rejected(arguments) -> None:
    with pytest.raises(ArgumentValidationError):
        EchoTool().validate_arguments(arguments)


def test_unknown_argument_fields_are_rejected_not_ignored() -> None:
    """Refused, unlike model output elsewhere in the codebase.

    An unexpected argument means the proposal and the tool disagree about what
    is being asked for; dropping it silently would run a *different* action
    from the one proposed.
    """
    with pytest.raises(ArgumentValidationError) as caught:
        EchoTool().validate_arguments({"text": "hello", "approved": True})
    assert "approved" in caught.value.fields


def test_argument_errors_name_fields_never_values() -> None:
    """Values are model output and may contain anything."""
    with pytest.raises(ArgumentValidationError) as caught:
        EchoTool().validate_arguments({"text": "SECRET-VALUE", "extra": "SECRET-2"})
    rendered = str(caught.value) + str(caught.value.fields)
    assert "SECRET-VALUE" not in rendered
    assert "SECRET-2" not in rendered


def test_a_tool_without_a_schema_accepts_no_arguments() -> None:
    tool = _Sample(name="bare")
    assert tool.validate_arguments({}) == {}
    with pytest.raises(ArgumentValidationError):
        tool.validate_arguments({"anything": 1})


def test_argument_models_are_frozen() -> None:
    validated = EchoArguments(text="hello")
    with pytest.raises(ValidationError):
        validated.text = "changed"


def test_validating_arguments_cannot_touch_tool_metadata() -> None:
    tool = EchoTool()
    before = tool.definition.model_dump()

    tool.validate_arguments({"text": "hello"})
    with pytest.raises(ArgumentValidationError):
        tool.validate_arguments({"risk_level": "low", "requires_approval": False})

    assert tool.definition.model_dump() == before
