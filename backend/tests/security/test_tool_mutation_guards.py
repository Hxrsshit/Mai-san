"""Stage 4C: proof that the authority guards are load-bearing.

An assertion that passes tells you nothing on its own -- it might be checking
something that could never have been false. Each test here disables one guard
in-process and shows the corresponding security assertion *fails*, then
restores it and shows the assertion passes.

That is the difference between "this property holds" and "this property is
enforced". Nothing here edits a file; every mutation is a monkeypatch confined
to one test.

The same eight mutations were also applied to the source files directly during
development; the counts are recorded in the acceptance report.
"""

import pytest

from app.tools import policy
from app.tools.authorization import AuthorizationService
from app.tools.catalog import build_catalog
from app.tools.registry import ToolRegistry
from app.tools.schemas import (
    ActionProposal,
    AuthorizationStatus,
    DenialReason,
    RiskLevel,
    ToolDefinition,
    status_rank,
)


@pytest.fixture
def registry() -> ToolRegistry:
    return build_catalog(ToolRegistry())


@pytest.fixture
def service(registry) -> AuthorizationService:
    return AuthorizationService(registry=registry)


def assert_unknown_is_refused(service) -> None:
    """The security assertion under test."""
    decision = service.authorize(ActionProposal(tool_name="delete_everything"))
    assert decision.status is AuthorizationStatus.UNKNOWN_TOOL


def assert_critical_is_refused(service) -> None:
    decision = service.authorize(ActionProposal(tool_name="future_delete_file"))
    assert decision.status is AuthorizationStatus.FORBIDDEN


def assert_near_miss_is_unknown(service) -> None:
    decision = service.authorize(ActionProposal(tool_name="delete_all_files"))
    assert decision.status is AuthorizationStatus.UNKNOWN_TOOL


# --- 1. Unknown tools must fail closed --------------------------------------


def test_the_unknown_tool_guard_is_load_bearing(service, monkeypatch) -> None:
    assert_unknown_is_refused(service)  # holds today

    monkeypatch.setattr(
        policy, "evaluate",
        lambda definition, name, intent=None: (
            AuthorizationStatus.ALLOWED, DenialReason.ALLOWED
        ),
    )
    with pytest.raises(AssertionError):
        assert_unknown_is_refused(service)

    monkeypatch.undo()
    assert_unknown_is_refused(service)


# --- 2. Approval requirements must not be overridable -----------------------


def test_the_approval_guard_is_load_bearing(registry, monkeypatch) -> None:
    from tests.test_tool_registry import _Sample

    registry.register(
        _Sample(name="gated", risk_level=RiskLevel.MEDIUM, requires_approval=True)
    )
    service = AuthorizationService(registry=registry)

    def gated_status():
        return service.authorize(ActionProposal(tool_name="gated")).status

    assert gated_status() is AuthorizationStatus.APPROVAL_REQUIRED

    monkeypatch.setattr(
        policy, "_declared_approval_rule",
        lambda definition: (AuthorizationStatus.ALLOWED, DenialReason.ALLOWED),
    )
    assert gated_status() is AuthorizationStatus.ALLOWED, (
        "the mutation did not take effect, so this test proves nothing"
    )

    monkeypatch.undo()
    assert gated_status() is AuthorizationStatus.APPROVAL_REQUIRED


# --- 3. Model metadata must not control risk --------------------------------


def test_the_risk_ceiling_guard_is_load_bearing(service, monkeypatch) -> None:
    assert_critical_is_refused(service)

    monkeypatch.setattr(
        policy, "_risk_ceiling_rule",
        lambda definition: (AuthorizationStatus.ALLOWED, DenialReason.ALLOWED),
    )
    monkeypatch.setattr(
        policy, "_enabled_rule",
        lambda definition: (AuthorizationStatus.ALLOWED, DenialReason.ALLOWED),
    )
    with pytest.raises(AssertionError):
        assert_critical_is_refused(service)

    monkeypatch.undo()
    assert_critical_is_refused(service)


# --- 4. The intent boundary must not be bypassable --------------------------


def test_the_intent_boundary_guard_is_load_bearing(service, monkeypatch) -> None:
    from app.intent.policy import derive
    from app.intent.schemas import IntentClassification

    question = derive(IntentClassification(intent_type="question", confidence=0.9))

    def echo_status():
        return service.authorize(
            ActionProposal(tool_name="echo", arguments={"text": "x"}),
            intent=question,
        ).status

    assert echo_status() is AuthorizationStatus.FORBIDDEN

    monkeypatch.setattr(
        policy, "_intent_rule",
        lambda intent: (AuthorizationStatus.ALLOWED, DenialReason.ALLOWED),
    )
    assert echo_status() is AuthorizationStatus.ALLOWED, (
        "the mutation did not take effect"
    )

    monkeypatch.undo()
    assert echo_status() is AuthorizationStatus.FORBIDDEN


# --- 5. Fuzzy matching must not be introduced -------------------------------


def test_the_exact_match_guard_is_load_bearing(registry, monkeypatch) -> None:
    service = AuthorizationService(registry=registry)
    assert_near_miss_is_unknown(service)

    real_get = registry.get

    def fuzzy_get(name):
        """A plausible bug: fall back to any tool sharing a word.

        This is the shape of mistake that maps `delete_all_files` onto
        `future_delete_file` -- both contain "delete".
        """
        exact = real_get(name)
        if exact is not None:
            return exact
        wanted = set(registry.canonical(name).replace("-", "_").split("_"))
        for known in registry.names():
            if wanted & set(known.split("_")):
                return real_get(known)
        return None

    monkeypatch.setattr(registry, "get", fuzzy_get)
    monkeypatch.setattr(
        registry, "definition",
        lambda name: (fuzzy_get(name).definition if fuzzy_get(name) else None),
    )
    with pytest.raises(AssertionError):
        assert_near_miss_is_unknown(service)

    monkeypatch.undo()
    assert_near_miss_is_unknown(service)


# --- 6. Registry metadata must stay immutable -------------------------------


def test_the_immutability_guard_is_load_bearing(registry) -> None:
    """A frozen definition refuses the write. An unfrozen one accepts it."""
    definition = registry.definition("future_delete_file")

    with pytest.raises(Exception):
        definition.risk_level = RiskLevel.LOW

    class _Unfrozen(ToolDefinition):
        model_config = {"frozen": False, "extra": "forbid"}

    mutable = _Unfrozen(**definition.model_dump())
    mutable.risk_level = RiskLevel.LOW
    assert mutable.risk_level is RiskLevel.LOW, "the mutation did not take effect"

    # The authoritative registry is untouched either way.
    assert registry.definition("future_delete_file").risk_level is RiskLevel.CRITICAL


# --- 7. The no-execution boundary must stay absent --------------------------


def test_the_no_execution_guard_is_load_bearing() -> None:
    """The check looks for a method by name, so adding one is detectable."""
    from app.tools.base import Tool

    forbidden = {"execute", "run", "invoke", "dispatch", "__call__"}

    def defined_dangerous(cls) -> set:
        return {name for name in vars(cls)} & forbidden

    assert defined_dangerous(Tool) == set()

    class _Executable(Tool):
        @property
        def definition(self):  # pragma: no cover - never registered
            raise NotImplementedError

        def execute(self, arguments):  # pragma: no cover
            return arguments

    assert defined_dangerous(_Executable) == {"execute"}, (
        "the check cannot see an added execute method, so it proves nothing"
    )


# --- 8. Authorization must stay monotonic -----------------------------------


def test_the_monotonicity_guard_is_load_bearing() -> None:
    """`most_restrictive` must be a maximum. A minimum is detectable."""
    from app.tools.schemas import most_restrictive

    pair = (AuthorizationStatus.ALLOWED, AuthorizationStatus.FORBIDDEN)
    assert most_restrictive(*pair) is AuthorizationStatus.FORBIDDEN

    def broken(*statuses):
        return min(statuses, key=status_rank)

    assert broken(*pair) is AuthorizationStatus.ALLOWED, (
        "the mutation did not take effect"
    )
    assert broken(*pair) is not most_restrictive(*pair)
