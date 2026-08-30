"""Stage 4C: authorization decisions.

Four explicit states, decided deterministically from application-controlled
metadata. No model, no database, no network.
"""

import pytest
from pydantic import ValidationError

from app.intent.policy import derive, fallback
from app.intent.schemas import IntentClassification
from app.tools import policy
from app.tools.authorization import AuthorizationService
from app.tools.catalog import build_catalog
from app.tools.registry import ToolRegistry
from app.tools.schemas import (
    ActionProposal,
    ActionSource,
    AuthorizationDecision,
    AuthorizationStatus,
    DenialReason,
    RiskLevel,
    ToolCategory,
    ToolDefinition,
    most_restrictive,
    status_rank,
)

from tests.test_tool_registry import _Sample


@pytest.fixture
def registry() -> ToolRegistry:
    return build_catalog(ToolRegistry())


@pytest.fixture
def authorize(registry):
    service = AuthorizationService(registry=registry)

    def run(tool_name, arguments=None, source=ActionSource.MODEL, intent=None):
        return service.authorize(
            ActionProposal(
                tool_name=tool_name, arguments=arguments or {}, source=source
            ),
            intent=intent,
        )

    return run


def intent_for(kind, **overrides):
    payload = {"intent_type": kind, "confidence": 0.9}
    payload.update(overrides)
    return derive(IntentClassification(**payload))


# --- The four states --------------------------------------------------------


def test_an_unknown_tool_is_refused(authorize) -> None:
    decision = authorize("delete_everything")

    assert decision.status is AuthorizationStatus.UNKNOWN_TOOL
    assert decision.reason == DenialReason.UNKNOWN_TOOL
    assert decision.requires_approval is True
    assert decision.risk_level is None
    assert decision.is_refused


def test_a_disabled_tool_is_forbidden(authorize) -> None:
    decision = authorize("future_web_search")

    assert decision.status is AuthorizationStatus.FORBIDDEN
    assert decision.reason == DenialReason.TOOL_DISABLED
    assert decision.is_refused


def test_an_approval_required_tool_is_gated(registry) -> None:
    registry.register(
        _Sample(name="gated", risk_level=RiskLevel.MEDIUM, requires_approval=True)
    )
    decision = AuthorizationService(registry=registry).authorize(
        ActionProposal(tool_name="gated")
    )

    assert decision.status is AuthorizationStatus.APPROVAL_REQUIRED
    assert decision.requires_approval is True
    assert not decision.is_allowed
    assert not decision.is_refused


def test_a_permitted_tool_is_allowed(authorize) -> None:
    decision = authorize("echo", {"text": "hello"})

    assert decision.status is AuthorizationStatus.ALLOWED
    assert decision.reason == DenialReason.ALLOWED
    assert decision.requires_approval is False
    assert decision.validated_arguments == {"text": "hello"}
    assert decision.is_allowed


def test_allowed_does_not_mean_anything_happened(authorize) -> None:
    """`ALLOWED` is a statement about policy, not a handle to spend."""
    decision = authorize("echo", {"text": "hello"})

    assert decision.is_allowed
    # Nothing on a decision runs, and nothing accepts one.
    assert not hasattr(decision, "execute")
    assert not hasattr(decision, "run")
    assert not hasattr(decision, "token")


# --- Determinism ------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["echo", "future_send_email", "future_delete_file", "unknown_thing"]
)
def test_repeated_decisions_are_identical(authorize, name) -> None:
    outcomes = {
        (authorize(name, {"text": "x"} if name == "echo" else {}).status,
         authorize(name, {"text": "x"} if name == "echo" else {}).reason)
        for _ in range(10)
    }
    assert len(outcomes) == 1


def test_a_decision_is_frozen(authorize) -> None:
    decision = authorize("future_delete_file")
    for field, value in (
        ("status", AuthorizationStatus.ALLOWED),
        ("requires_approval", False),
        ("risk_level", RiskLevel.LOW),
    ):
        with pytest.raises(ValidationError):
            setattr(decision, field, value)


def test_authorization_needs_no_model_database_or_network() -> None:
    """Structural: the module imports nothing that could reach any of them."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "tools"
    banned = ("app.llm", "sqlalchemy", "app.database", "httpx", "socket", "requests")

    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(
                    name == item or name.startswith(item + ".") for item in banned
                ), f"{path.name} imports {name}"


# --- The risk ladder --------------------------------------------------------


@pytest.mark.parametrize(
    "risk,expected",
    [
        (RiskLevel.LOW, AuthorizationStatus.ALLOWED),
        (RiskLevel.MEDIUM, AuthorizationStatus.ALLOWED),
        (RiskLevel.HIGH, AuthorizationStatus.APPROVAL_REQUIRED),
        (RiskLevel.CRITICAL, AuthorizationStatus.FORBIDDEN),
    ],
)
def test_risk_determines_the_floor(registry, risk, expected) -> None:
    """A tool opting out of approval still cannot escape its risk level."""
    registry.register(
        _Sample(name=f"risk-{risk.value}", risk_level=risk, requires_approval=False)
    )
    decision = AuthorizationService(registry=registry).authorize(
        ActionProposal(tool_name=f"risk-{risk.value}")
    )
    assert decision.status is expected


def test_critical_risk_is_refused_not_merely_gated(authorize) -> None:
    """An approval prompt is not sufficient protection for irreversible damage."""
    decision = authorize("future_delete_file")
    assert decision.status is AuthorizationStatus.FORBIDDEN
    assert decision.risk_level is RiskLevel.CRITICAL


def test_a_tool_can_raise_its_own_bar_but_not_lower_it(registry) -> None:
    registry.register(
        _Sample(name="cautious", risk_level=RiskLevel.LOW, requires_approval=True)
    )
    registry.register(
        _Sample(name="reckless", risk_level=RiskLevel.HIGH, requires_approval=False)
    )
    service = AuthorizationService(registry=registry)

    assert service.authorize(
        ActionProposal(tool_name="cautious")
    ).status is AuthorizationStatus.APPROVAL_REQUIRED
    assert service.authorize(
        ActionProposal(tool_name="reckless")
    ).status is AuthorizationStatus.APPROVAL_REQUIRED


# --- Monotonicity -----------------------------------------------------------


def test_the_status_ordering_is_total() -> None:
    ranks = [status_rank(status) for status in AuthorizationStatus]
    assert len(set(ranks)) == len(ranks)


def test_most_restrictive_never_loosens() -> None:
    for first in AuthorizationStatus:
        for second in AuthorizationStatus:
            result = most_restrictive(first, second)
            assert status_rank(result) >= status_rank(first)
            assert status_rank(result) >= status_rank(second)


def test_no_combination_of_metadata_yields_allowed_from_a_forbidden_input(
    registry,
) -> None:
    """Exhaustive: every registrable combination, checked for monotonicity."""
    service = AuthorizationService(registry=registry)
    index = 0
    for risk in RiskLevel:
        for category in ToolCategory:
            for approval in (True, False):
                for enabled in (True, False):
                    index += 1
                    name = f"combo-{index}"
                    registry.register(
                        _Sample(
                            name=name, risk_level=risk, category=category,
                            requires_approval=approval, enabled=enabled,
                        )
                    )
                    decision = service.authorize(ActionProposal(tool_name=name))

                    if not enabled:
                        assert decision.status is AuthorizationStatus.FORBIDDEN
                    elif risk is RiskLevel.CRITICAL:
                        assert decision.status is AuthorizationStatus.FORBIDDEN
                    elif risk is RiskLevel.HIGH or approval:
                        assert decision.status is AuthorizationStatus.APPROVAL_REQUIRED
                    else:
                        assert decision.status is AuthorizationStatus.ALLOWED

                    # The invariant that matters, in every case.
                    if decision.status is not AuthorizationStatus.ALLOWED:
                        assert decision.requires_approval is True


# --- Stage 4A precedence ----------------------------------------------------


@pytest.mark.parametrize("kind", ["conversation", "question"])
def test_a_conversational_turn_forbids_every_tool(authorize, kind) -> None:
    """Stage 4A clamps those turns to no execution capability. Tighter wins.

    The specification's example attack: the user asks a question, the model
    answers with "use tool future_delete_file". The proposal is forbidden
    because the *turn* had no such capability.
    """
    decision = authorize("echo", {"text": "hello"}, intent=intent_for(kind))

    assert decision.status is AuthorizationStatus.FORBIDDEN
    assert decision.reason == DenialReason.INTENT_FORBIDS


def test_an_action_turn_does_not_loosen_anything(authorize) -> None:
    """An action intent removes the intent objection; the rest still apply."""
    action = intent_for("action")

    assert authorize("echo", {"text": "x"}, intent=action).status is (
        AuthorizationStatus.ALLOWED
    )
    assert authorize("future_delete_file", intent=action).status is (
        AuthorizationStatus.FORBIDDEN
    )
    assert authorize("nope", intent=action).status is (
        AuthorizationStatus.UNKNOWN_TOOL
    )


def test_a_degraded_intent_forbids_tools(authorize) -> None:
    """A failed classification carries no capability, so nothing is permitted."""
    decision = authorize("echo", {"text": "x"}, intent=fallback("provider_error"))
    assert decision.status is AuthorizationStatus.FORBIDDEN


def test_no_intent_is_not_permission(authorize) -> None:
    """Absent intent means no intent-level rule, never a bypass of the others."""
    assert authorize("future_delete_file").status is AuthorizationStatus.FORBIDDEN
    assert authorize("unknown").status is AuthorizationStatus.UNKNOWN_TOOL


def test_intent_can_only_tighten(authorize) -> None:
    """For every tool, adding an intent never produces a looser outcome."""
    for name in ("echo", "future_web_search", "future_send_email", "future_delete_file"):
        arguments = {"text": "x"} if name == "echo" else {}
        without = authorize(name, arguments).status
        for kind in ("conversation", "question", "planning", "task", "research", "action"):
            with_intent = authorize(name, arguments, intent=intent_for(kind)).status
            assert status_rank(with_intent) >= status_rank(without)


# --- Arguments --------------------------------------------------------------


def test_invalid_arguments_forbid_the_action(authorize) -> None:
    decision = authorize("echo", {"text": ""})

    assert decision.status is AuthorizationStatus.FORBIDDEN
    assert decision.reason == DenialReason.INVALID_ARGUMENTS
    assert decision.validated_arguments is None


def test_unknown_argument_fields_forbid_the_action(authorize) -> None:
    decision = authorize("echo", {"text": "hello", "approved": True})

    assert decision.status is AuthorizationStatus.FORBIDDEN
    assert decision.reason == DenialReason.INVALID_ARGUMENTS


def test_arguments_are_not_validated_for_a_refused_tool(authorize) -> None:
    """A refusal must not depend on argument shape."""
    decision = authorize("future_delete_file", {"path": "/", "anything": 1})
    assert decision.status is AuthorizationStatus.FORBIDDEN
    assert decision.reason == DenialReason.TOOL_DISABLED
    assert decision.validated_arguments is None


@pytest.mark.parametrize(
    "arguments",
    [
        {f"key{index}": "value" for index in range(21)},   # too many keys
        {"a" * 65: "value"},                                # key too long
    ],
)
def test_oversized_argument_bags_are_rejected_at_the_proposal(arguments) -> None:
    with pytest.raises(ValidationError):
        ActionProposal(tool_name="echo", arguments=arguments)


# --- Failure handling -------------------------------------------------------


@pytest.mark.parametrize("name", ["  ", "\t", "\n"])
def test_a_blank_tool_name_is_refused(authorize, name) -> None:
    decision = authorize(name)
    assert decision.status is AuthorizationStatus.UNKNOWN_TOOL
    assert decision.reason == DenialReason.EMPTY_NAME


def test_an_empty_tool_name_is_rejected_at_the_proposal() -> None:
    with pytest.raises(ValidationError):
        ActionProposal(tool_name="")


def test_a_reason_is_always_an_application_constant(authorize) -> None:
    """A decision never echoes model output or user text back."""
    known = {
        value for name, value in vars(DenialReason).items()
        if not name.startswith("_")
    }
    for name in ("echo", "future_send_email", "future_delete_file", "<script>alert(1)"):
        decision = authorize(name, {"text": "x"} if name == "echo" else {})
        assert decision.reason in known
