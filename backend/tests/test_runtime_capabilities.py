"""Stage 4E.1: capability facts follow the registry, and only the registry.

The failure being fixed: asked what tools it had, Mai listed web search, a
calculator, code execution, filesystem access and email sending. None exists.
The model was answering from pretraining because nothing had told it
otherwise -- the same shape of bug as Stage 4D.1's "OpenAI / GPT-4", one level
up from identity into capability.

So the tests here are mostly about *derivation*: that the list comes from the
registries and cannot come from anywhere else.
"""

from typing import Optional, Type

import pytest
from pydantic import Field

from app.core.config import Settings
from app.execution.tools import ExecutableRegistry, ExecutableTool
from app.prompt.formatter import render_capabilities, render_runtime_facts
from app.runtime.capabilities import CapabilityState, build as build_capabilities
from app.runtime.facts import build as build_facts
from app.runtime.schemas import RuntimeFacts, ToolCapability
from app.tools.base import Tool, ToolArguments
from app.tools.registry import ToolRegistry
from app.tools.schemas import ExecutionMode, RiskLevel, ToolCategory, ToolDefinition


# --- Fixtures: a registry nobody else shares --------------------------------


class _Arguments(ToolArguments):
    text: str = Field(default="", max_length=100)


class _Declared(Tool):
    """A declaration built for one test, so no test mutates module state."""

    def __init__(self, **overrides) -> None:
        self._definition = ToolDefinition(
            **{
                "name": "invented_tool",
                "description": "A tool that exists only inside this test.",
                "category": ToolCategory.DIAGNOSTIC,
                "risk_level": RiskLevel.LOW,
                "requires_approval": True,
                **overrides,
            }
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        return _Arguments


class _Executor(ExecutableTool):
    """An implementation for the declaration above. Runs nothing."""

    def __init__(self, name: str = "invented_tool") -> None:
        self.name = name

    @property
    def arguments_model(self):
        return _Arguments

    def run(self, arguments, context):  # pragma: no cover - never dispatched
        raise AssertionError("a capability test must never execute anything")


def _capabilities(declarations, executable_names=(), execution_enabled=True):
    """Build capability facts from an isolated pair of registries."""
    registry = ToolRegistry()
    for declaration in declarations:
        registry.register(declaration)

    executors = ExecutableRegistry()
    for name in executable_names:
        executors.register(_Executor(name))

    return build_capabilities(
        settings=Settings(
            _env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=execution_enabled
        ),
        registry=registry,
        executable=executors,
    )


def _state_of(capabilities, identifier: str) -> CapabilityState:
    return next(item.state for item in capabilities if item.identifier == identifier)


# --- Requirement 1: derived from the registry -------------------------------


def test_a_newly_registered_tool_appears_without_editing_prompt_text() -> None:
    """The core of the stage: the list follows the registry.

    Nothing was added to a prompt, a constant or a catalogue of strings. A
    tool was registered, and the capability facts changed.
    """
    before = _capabilities([])
    after = _capabilities([_Declared()])

    assert before == ()
    assert [item.identifier for item in after] == ["invented_tool"]


def test_removing_a_tool_removes_the_capability() -> None:
    with_tool = _capabilities([_Declared()], executable_names=("invented_tool",))
    without = _capabilities([])

    assert _state_of(with_tool, "invented_tool") is CapabilityState.AVAILABLE_WITH_APPROVAL
    assert without == ()


def test_disabling_a_tool_changes_its_state() -> None:
    """Requirement 10's second half: the state follows the switch."""
    enabled = _capabilities(
        [_Declared(enabled=True)], executable_names=("invented_tool",)
    )
    disabled = _capabilities(
        [_Declared(enabled=False)], executable_names=("invented_tool",)
    )

    assert _state_of(enabled, "invented_tool") is CapabilityState.AVAILABLE_WITH_APPROVAL
    assert _state_of(disabled, "invented_tool") is CapabilityState.IMPLEMENTED_DISABLED


def test_a_capability_carries_the_declaration_metadata() -> None:
    capability = _capabilities(
        [_Declared(risk_level=RiskLevel.MEDIUM)], executable_names=("invented_tool",)
    )[0]

    assert capability.identifier == "invented_tool"
    assert capability.display_name == "Invented tool"
    assert capability.description == "A tool that exists only inside this test."
    assert capability.category == "diagnostic"
    assert capability.risk_level == "medium"
    assert capability.requires_approval is True
    assert capability.enabled is True


def test_the_capability_layer_invents_nothing() -> None:
    """An empty registry produces an empty list, not a plausible default."""
    assert _capabilities([]) == ()


# --- Requirement 2: the five states are all reachable -----------------------


def test_every_state_is_reachable() -> None:
    """A ladder with an unreachable rung is three states pretending to be five.

    `AVAILABLE` is the one no shipped tool occupies -- every executable tool
    in the catalogue requires approval -- so it is built here explicitly. It
    is a real state a `requires_approval=False` tool would reach, not a
    decorative one.
    """
    reached = {
        # Declared, no executor.
        _state_of(_capabilities([_Declared()]), "invented_tool"),
        # Executor, but the operator switched this tool off.
        _state_of(
            _capabilities([_Declared(enabled=False)], ("invented_tool",)),
            "invented_tool",
        ),
        # Executor, permitted, but execution is off for the deployment.
        _state_of(
            _capabilities([_Declared()], ("invented_tool",), execution_enabled=False),
            "invented_tool",
        ),
        # Executor, permitted, execution on, approval required.
        _state_of(
            _capabilities([_Declared()], ("invented_tool",)), "invented_tool"
        ),
        # Executor, permitted, execution on, no approval needed.
        _state_of(
            _capabilities([_Declared(requires_approval=False)], ("invented_tool",)),
            "invented_tool",
        ),
    }

    assert reached == set(CapabilityState)


def test_no_implementation_outranks_every_switch() -> None:
    """A tool with no executor is NOT_IMPLEMENTED whatever else is true.

    Reporting it as "disabled" would imply that enabling something would
    help. Nothing would -- there is no implementation to enable.
    """
    for enabled in (True, False):
        for execution in (True, False):
            capabilities = _capabilities(
                [_Declared(enabled=enabled)], execution_enabled=execution
            )
            assert (
                _state_of(capabilities, "invented_tool")
                is CapabilityState.NOT_IMPLEMENTED
            )


def test_a_forbidden_risk_level_is_reported_as_disabled() -> None:
    """Policy decides this, and the capability layer reports policy's answer.

    Deliberately not re-derived here: a second copy of the risk ceiling could
    disagree with the real one, and the section would then describe a Mai that
    does not exist.
    """
    capabilities = _capabilities(
        [_Declared(risk_level=RiskLevel.CRITICAL)], executable_names=("invented_tool",)
    )

    assert (
        _state_of(capabilities, "invented_tool") is CapabilityState.IMPLEMENTED_DISABLED
    )


def test_usable_is_true_only_in_the_two_available_states() -> None:
    for state in CapabilityState:
        capability = ToolCapability(
            identifier="t", display_name="T", description="d",
            category="diagnostic", risk_level="low",
            enabled=True, requires_approval=True, state=state,
        )
        expected = state in (
            CapabilityState.AVAILABLE, CapabilityState.AVAILABLE_WITH_APPROVAL
        )
        assert capability.usable is expected, state


# --- The shipped catalogue --------------------------------------------------


def test_the_real_catalogue_reports_the_three_workspace_tools() -> None:
    """Against the actual registries, not a fixture."""
    facts = build_facts(
        Settings(_env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=True)
    )

    usable = {item.identifier for item in facts.usable_capabilities}
    assert usable == {"create_text_file", "read_text_file", "list_workspace_files"}


def test_nothing_is_usable_in_the_default_deployment() -> None:
    """Execution ships off, so nothing is available -- and it says so."""
    facts = build_facts(Settings(_env_file=None, GROQ_API_KEY="x"))

    assert facts.usable_capabilities == ()
    assert all(
        item.state is CapabilityState.IMPLEMENTED_UNAVAILABLE
        for item in facts.capabilities
        if item.identifier in {"create_text_file", "read_text_file",
                               "list_workspace_files"}
    )


@pytest.mark.parametrize(
    "absent",
    ["email", "web_search", "browse", "python", "code_execution", "calculator",
     "calendar", "http", "shell"],
)
def test_no_pretrained_capability_appears_in_the_facts(absent) -> None:
    """The capabilities Mai was hallucinating are simply not there."""
    facts = build_facts(
        Settings(_env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=True)
    )

    for item in facts.usable_capabilities:
        assert absent not in item.identifier, item.identifier


# --- Requirement 3: dynamic rendering ---------------------------------------


def test_the_rendered_section_lists_no_tool_of_its_own() -> None:
    """The renderer is a loop, not a catalogue.

    Given an empty capability list it must list no tool at all -- which it can
    only do if it holds none. Scoped to the bullet entries: the preamble names
    email and web search deliberately, as examples of subjects the model may
    *explain*, and that sentence is the point of Requirement 5 rather than a
    capability claim. A separate test pins that it stays one sentence.
    """
    entries = [
        line for line in render_capabilities(())
        if line.startswith("- ")
    ]

    assert entries == ["- None"] * 5


def test_the_rendered_section_follows_the_registry() -> None:
    capabilities = _capabilities([_Declared()], executable_names=("invented_tool",))
    block = "\n".join(render_capabilities(capabilities))

    assert "Invented tool" in block
    assert "A tool that exists only inside this test." in block


def test_each_group_is_labelled_unambiguously() -> None:
    """Requirement 2's wording rule, checked in the text the model reads."""
    block = "\n".join(
        render_capabilities(
            _capabilities([_Declared()], execution_enabled=False)
        )
    ).lower()

    assert "not implemented" in block
    assert "cannot be performed" in block


def test_an_empty_group_says_none_rather_than_disappearing() -> None:
    """A missing heading would read as "unknown"; "None" reads as none."""
    block = "\n".join(render_capabilities(()))

    assert block.count("- None") == 5


# --- Requirement 4: the authoritative boundary ------------------------------


def test_the_section_states_that_it_is_authoritative() -> None:
    block = "\n".join(render_capabilities(())).lower()

    assert "authoritative" in block
    assert "complete" in block


def test_the_section_states_the_closed_world_rule() -> None:
    """The load-bearing sentence: absent means unavailable.

    Listing what exists cannot by itself stop the model reaching for a
    capability it remembers -- an absent thing has no line to read. The rule
    is what covers everything nobody enumerated.
    """
    block = "\n".join(render_capabilities(())).lower()

    assert "not available in this mai instance" in block
    assert "whatever you may recall from training" in block


def test_the_section_separates_knowing_from_doing() -> None:
    """Requirement 5, in the text: explaining is allowed, claiming is not."""
    block = "\n".join(render_capabilities(())).lower()

    assert "knowing how something works does not mean you can do it" in block
    assert "explain" in block


def test_the_section_does_not_enumerate_absent_capabilities() -> None:
    """The "not implemented" list must not become a manual catalogue.

    The only place email or web search may appear is the sentence permitting
    the model to *explain* them, and in registry-derived entries. Neither is
    a maintained list of things Mai lacks.
    """
    block = "\n".join(render_capabilities(()))

    # With no registered tools, nothing capability-shaped is enumerated at all
    # beyond the one explanatory sentence.
    assert block.lower().count("email") == 1
    assert block.lower().count("web search") == 1


# --- Requirement 8: one authority, composed not duplicated ------------------


def test_capabilities_live_inside_runtime_facts() -> None:
    """Not a second identity system: a field on the existing one."""
    assert "capabilities" in RuntimeFacts.model_fields

    facts = build_facts(Settings(_env_file=None, GROQ_API_KEY="x"))
    assert isinstance(facts.capabilities, tuple)
    assert all(isinstance(item, ToolCapability) for item in facts.capabilities)


def test_the_capability_section_sits_inside_the_runtime_facts_block() -> None:
    """One authoritative block, so there is one thing to rank and to trust."""
    from app.prompt.formatter import CAPABILITY_HEADER, RUNTIME_FACTS_HEADER

    block = render_runtime_facts(build_facts(Settings(_env_file=None, GROQ_API_KEY="x")))

    assert block.index(RUNTIME_FACTS_HEADER) < block.index(CAPABILITY_HEADER)


def test_identity_and_capability_are_reported_together() -> None:
    """All six conceptual facts in one place."""
    block = render_runtime_facts(
        build_facts(Settings(_env_file=None, GROQ_API_KEY="x"))
    ).lower()

    for fact in ("assistant name", "llm provider", "llm model", "database",
                 "long-term memory", "runtime capabilities"):
        assert fact in block, fact


# --- Requirement 9: this stage costs nothing per turn -----------------------


def test_building_capabilities_issues_no_database_query(session_factory) -> None:
    """Counted, not asserted by inspection.

    The engine is instrumented and the builders run; the count must be zero. A
    capability layer that queried per request would put a database round trip
    on the chat path for information that cannot change between requests.
    """
    from sqlalchemy import event

    statements = []
    engine = session_factory.kw["bind"].sync_engine

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        build_capabilities(settings=Settings(_env_file=None, GROQ_API_KEY="x"))
        build_facts(Settings(_env_file=None, GROQ_API_KEY="x"))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements == []


def test_building_capabilities_calls_no_model(fake_provider) -> None:
    """The provider is handed in and must go untouched.

    `build` reads `provider.name` and `provider.model` -- two properties. It
    never calls `generate_response`, so the call log stays empty.
    """
    assert fake_provider.calls == []

    facts = build_facts(
        Settings(_env_file=None, GROQ_API_KEY="x"), provider=fake_provider
    )

    assert fake_provider.calls == []
    assert facts.capabilities  # and it still produced a real answer


async def test_a_chat_turn_adds_no_extra_provider_call(
    client, conversation_id, fake_provider
) -> None:
    """One generation per turn, exactly as before this stage.

    The capability section is assembled from memory while the prompt is being
    built, so it costs nothing a turn was not already paying.
    """
    import json as _json

    fake_provider.extraction_reply = _json.dumps(
        {"should_store_memory": False, "memories": []}
    )
    before = len(fake_provider.calls)

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What can you do?"},
    )

    generations = len(fake_provider.calls) - before
    # One reply. Intent classification and memory extraction are scripted
    # separately in this fixture and are unchanged by this stage.
    assert generations == 1

    # And the capability section did reach the model on that single call.
    from app.prompt.formatter import CAPABILITY_HEADER

    assert any(
        CAPABILITY_HEADER in message.content for message in fake_provider.last_call
    )


# --- Requirement 11: unknown tools stay unknown -----------------------------


def test_an_unregistered_name_produces_no_capability() -> None:
    """Nothing is fabricated for a tool nobody registered.

    No entry, no approval flow, no proposal, no execution record. The absence
    is the answer.
    """
    capabilities = _capabilities([_Declared()])
    identifiers = {item.identifier for item in capabilities}

    for unknown in ("send_email", "web_search", "python_exec", "calculator"):
        assert unknown not in identifiers


def test_the_shipped_registry_has_no_email_or_search_executor() -> None:
    """The specific capabilities Mai hallucinated, checked by name."""
    facts = build_facts(
        Settings(_env_file=None, GROQ_API_KEY="x", EXECUTION_ENABLED=True)
    )
    by_name = {item.identifier: item for item in facts.capabilities}

    assert by_name["future_send_email"].state is CapabilityState.NOT_IMPLEMENTED
    assert by_name["future_web_search"].state is CapabilityState.NOT_IMPLEMENTED
    assert by_name["future_send_email"].usable is False
    assert by_name["future_web_search"].usable is False
