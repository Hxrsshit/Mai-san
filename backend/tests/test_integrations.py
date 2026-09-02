"""Stage 4F-A: the integration foundation, exercised with a fake provider.

No external service is contacted anywhere in this file. The "provider" is a
Python function whose behaviour each test sets, which is the point: the
architecture is what is under test, and a real provider would make these
slow, flaky and dependent on someone else's uptime.
"""

import pytest

from app.integrations.base import Integration, IntegrationState, OperationSpec
from app.integrations.credentials import (
    CredentialRequirement,
    CredentialState,
    CredentialStatus,
    CredentialType,
    EnvironmentCredentialResolver,
)
from app.integrations.errors import (
    CredentialsMissing,
    IntegrationError,
    NetworkPolicyViolation,
    ProviderForbidden,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnauthorized,
    ProviderValidationError,
    UnknownIntegration,
    UnsupportedOperation,
)
from app.integrations.policy import (
    NetworkPolicy,
    RetryPolicy,
    TimeoutPolicy,
    is_forbidden_address,
)
from app.integrations.registry import IntegrationRegistry, build_integrations
from app.integrations.result import (
    DataClassification,
    ExternalData,
    ExternalResult,
    ExternalResultState,
    TrustLevel,
    at_least,
)
from tests.support.fake_integration import (
    FakeIntegration,
    integration_environment,
)


def _ok(operation="lookup", **overrides):
    return ExternalResult(
        **{
            "state": ExternalResultState.SUCCESS,
            "integration": "fake_provider",
            "operation": operation,
            "summary": "done",
            **overrides,
        }
    )


# --- The registry -----------------------------------------------------------


def test_an_integration_can_be_registered_and_retrieved() -> None:
    registry = IntegrationRegistry()
    integration = FakeIntegration()
    registry.register(integration)

    assert registry.get("fake_provider") is integration
    assert registry.contains("fake_provider")
    assert registry.names() == ("fake_provider",)


def test_a_duplicate_registration_is_refused() -> None:
    """Silently replacing would take over a name tools already reference."""
    registry = IntegrationRegistry()
    registry.register(FakeIntegration())

    with pytest.raises(ValueError):
        registry.register(FakeIntegration())


def test_an_unknown_integration_is_refused_rather_than_guessed() -> None:
    registry = IntegrationRegistry()
    registry.register(FakeIntegration())

    for name in ("fake", "fake_provider_2", "FAKE-PROVIDER", "", "  "):
        assert registry.get(name) is None, name

    with pytest.raises(UnknownIntegration):
        registry.require("nothing_like_this")


def test_case_and_whitespace_canonicalise_to_the_same_integration() -> None:
    """As in Stage 4C: the only normalisation, and it cannot change meaning."""
    registry = IntegrationRegistry()
    integration = FakeIntegration()
    registry.register(integration)

    assert registry.get("  FAKE_PROVIDER  ") is integration


def test_the_registry_is_immutable_once_sealed() -> None:
    """Registration is a startup activity, so it stops being possible."""
    registry = IntegrationRegistry()
    registry.seal()

    with pytest.raises(RuntimeError):
        registry.register(FakeIntegration())


def test_the_shipped_registry_holds_exactly_one_integration() -> None:
    """Stage 4F-A connected nothing. Stage 4F-B connects exactly one.

    Exact rather than a minimum: a second integration appearing without this
    test being updated would mean an external service became reachable
    without anyone deciding it should.
    """
    from app.integrations.registry import get_integration_registry

    registry = get_integration_registry()
    assert registry.names() == ("web_search",)
    assert registry.sealed is True


def test_building_the_catalogue_registers_only_web_search() -> None:
    registry = build_integrations(IntegrationRegistry())

    assert registry.names() == ("web_search",)


def test_the_shipped_search_integration_is_read_only() -> None:
    """One operation, and it declares itself free of side effects."""
    from app.integrations.registry import get_integration_registry

    integration = get_integration_registry().require("web_search")

    assert integration.operation_names() == ("search",)
    assert integration._operations["search"].has_side_effect is False


# --- The operation contract -------------------------------------------------


def test_an_integration_exposes_only_its_named_operations() -> None:
    integration = FakeIntegration()

    assert integration.operation_names() == ("lookup",)
    assert integration.supports("lookup")
    assert not integration.supports("request")


def test_an_unnamed_operation_is_refused() -> None:
    """Refused, not forwarded. There is no fallback that guesses."""
    integration = FakeIntegration()

    for hostile in (
        "request", "get", "post", "fetch", "LOOKUP ", "lookup2", "", "..",
    ):
        with pytest.raises(UnsupportedOperation):
            integration.invoke(hostile, {})


def test_no_integration_exposes_a_generic_request_method() -> None:
    """The single most important absence in the whole stage.

    `request(url, method, headers, body)` would make every endpoint the
    provider has reachable, turn SSRF into a matter of choosing a string, and
    move the decision about what Mai may do from code review to runtime.
    """
    for forbidden in (
        "request", "http", "fetch", "call", "get", "post", "put", "delete",
        "patch", "send", "execute", "raw",
    ):
        assert not hasattr(Integration, forbidden), forbidden
        assert not hasattr(FakeIntegration(), forbidden), forbidden


def test_an_integration_receives_only_its_operation_arguments() -> None:
    """Data minimisation, checked at the boundary itself."""
    integration = FakeIntegration()
    integration.invoke("lookup", {"query": "weather"})

    assert integration.calls == [{"query": "weather"}]


# --- Availability states ----------------------------------------------------


def test_a_missing_credential_makes_an_integration_unconfigured() -> None:
    integration = FakeIntegration(
        credentials=EnvironmentCredentialResolver(environ={})
    )

    assert integration.state() is IntegrationState.NOT_CONFIGURED
    assert integration.available is False


def test_a_present_credential_makes_an_integration_available() -> None:
    integration = FakeIntegration(
        credentials=EnvironmentCredentialResolver(
            environ={"FAKE_PROVIDER_API_KEY": "k"}
        )
    )

    assert integration.state() is IntegrationState.AVAILABLE
    assert integration.available is True


def test_disabled_outranks_a_present_credential() -> None:
    """A disabled integration is disabled whatever its credentials say.

    Reporting it as "not configured" would send someone to add a key that
    would change nothing.
    """
    integration = FakeIntegration(
        credentials=EnvironmentCredentialResolver(
            environ={"FAKE_PROVIDER_API_KEY": "k"}
        ),
        enabled=False,
    )

    assert integration.state() is IntegrationState.DISABLED
    assert integration.available is False


def test_an_unavailable_integration_returns_a_result_rather_than_running() -> None:
    def explode(arguments):
        raise AssertionError("the operation must not be reached")

    integration = FakeIntegration(
        responder=explode, credentials=EnvironmentCredentialResolver(environ={})
    )
    result = integration.invoke("lookup", {"query": "x"})

    assert result.state is ExternalResultState.UNAVAILABLE
    assert result.succeeded is False


def test_whitespace_is_not_a_credential() -> None:
    """A configuration mistake must not become an authentication failure."""
    integration = FakeIntegration(
        credentials=EnvironmentCredentialResolver(
            environ={"FAKE_PROVIDER_API_KEY": "   "}
        )
    )

    assert integration.available is False


# --- Credentials never travel as data ---------------------------------------


def test_a_credential_state_carries_no_secret() -> None:
    """The field set is pinned, so an addition has to be deliberate."""
    assert set(CredentialState.model_fields) == {
        "identifier", "provider", "account", "credential_type", "status",
        "expires_at", "granted_scopes",
    }

    for forbidden in ("value", "secret", "token", "key", "password"):
        assert not any(
            forbidden in name for name in CredentialState.model_fields
        ), forbidden


def test_describing_a_credential_never_returns_its_value() -> None:
    resolver = EnvironmentCredentialResolver(
        environ={"FAKE_PROVIDER_API_KEY": "super-secret-value"}
    )
    requirement = FakeIntegration().credential_requirement

    state = resolver.describe(requirement)

    assert "super-secret-value" not in state.model_dump_json()
    assert state.status is CredentialStatus.AVAILABLE


def test_resolving_a_missing_secret_raises_rather_than_returning_empty() -> None:
    """An empty string would be sent to a provider as if it were a key."""
    resolver = EnvironmentCredentialResolver(environ={})

    with pytest.raises(CredentialsMissing):
        resolver.resolve_secret(FakeIntegration().credential_requirement)


def test_a_tool_schema_has_no_field_a_credential_could_arrive_in() -> None:
    """Structural: a user cannot supply a key because there is nowhere to put one."""
    from tests.support.fake_integration import FakeLookupArguments

    assert set(FakeLookupArguments.model_fields) == {"query"}

    with pytest.raises(Exception):
        FakeLookupArguments(query="x", api_key="k")


def test_scopes_are_modelled_per_permission_not_per_provider() -> None:
    """`gmail.send` must be distinguishable from `gmail.readonly`.

    Retrofitting scope boundaries onto already-issued tokens is not possible,
    so the distinction has to exist before the first OAuth integration does.
    """
    requirement = FakeIntegration().credential_requirement

    assert requirement.required_scopes == frozenset({"lookup.read"})
    assert requirement.credential_type is CredentialType.API_KEY
    # And the shape OAuth will need is already representable.
    assert CredentialType.OAUTH2 in CredentialType


def test_a_credential_belongs_to_an_account() -> None:
    """Single-user today; the field exists so multi-user is a value change."""
    assert FakeIntegration().credential_requirement.account == "default"
    assert "account" in CredentialState.model_fields


# --- The result contract ----------------------------------------------------


@pytest.mark.parametrize("state", list(ExternalResultState))
def test_every_result_state_is_representable_and_only_one_succeeds(state) -> None:
    result = ExternalResult(
        state=state, integration="fake_provider", operation="lookup"
    )

    assert result.succeeded is (state is ExternalResultState.SUCCESS)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProviderTimeout(), ExternalResultState.TIMEOUT),
        (ProviderRateLimited(), ExternalResultState.RATE_LIMITED),
        (ProviderUnauthorized(), ExternalResultState.UNAUTHORIZED),
        (ProviderForbidden(), ExternalResultState.FORBIDDEN),
        (ProviderValidationError(), ExternalResultState.VALIDATION_ERROR),
    ],
)
def test_each_provider_error_maps_to_its_own_result_state(error, expected) -> None:
    """Not collapsed into "something went wrong"."""
    def failing(arguments):
        raise error

    result = FakeIntegration(responder=failing).invoke("lookup", {"query": "x"})

    assert result.state is expected
    assert result.reason == error.reason


def test_an_unconverted_exception_becomes_unknown_error_not_success() -> None:
    """An adapter bug must never read as a completed operation."""
    def buggy(arguments):
        raise ValueError("https://internal.host/path?token=abcd")

    result = FakeIntegration(responder=buggy).invoke("lookup", {"query": "x"})

    assert result.state is ExternalResultState.UNKNOWN_ERROR
    assert result.succeeded is False
    # And the exception message -- which carried a host and a token -- is gone.
    assert "internal.host" not in result.model_dump_json()
    assert "abcd" not in result.model_dump_json()


def test_a_result_carries_safe_audit_metadata_only() -> None:
    result = _ok(provider_status=200, latency_ms=12, attempts=2)

    assert result.audit_metadata() == {
        "integration": "fake_provider",
        "operation": "lookup",
        "result": "success",
        "reason": None,
        "latency_ms": 12,
        "attempts": 2,
        "provider_status": 200,
    }
    # Content is absent by construction: it does not belong in an audit table.
    assert "data" not in result.audit_metadata()


def test_latency_and_attempts_are_recorded_by_the_integration() -> None:
    result = FakeIntegration().invoke("lookup", {"query": "x"})

    assert result.attempts == 1
    assert result.latency_ms is not None and result.latency_ms >= 0


# --- The trust boundary -----------------------------------------------------


def test_external_data_is_always_untrusted() -> None:
    """There is no value of any field that makes it trusted."""
    data = ExternalData(source="fake_provider", content="hello")

    assert data.trust_level is TrustLevel.UNTRUSTED
    assert "trust_level" not in ExternalData.model_fields


def test_external_data_cannot_be_marked_trusted() -> None:
    with pytest.raises(Exception):
        ExternalData(
            source="fake_provider", content="x", trust_level=TrustLevel.APPLICATION
        )


def test_external_data_cannot_be_edited_after_construction() -> None:
    data = ExternalData(source="fake_provider", content="x")

    with pytest.raises(Exception):
        data.content = "something else"


def test_external_data_records_its_source_and_time() -> None:
    data = ExternalData(source="fake_provider", content="x")

    assert data.source == "fake_provider"
    assert data.retrieved_at is not None


def test_data_classification_is_ordered() -> None:
    assert at_least(DataClassification.SECRET, DataClassification.PRIVATE)
    assert at_least(DataClassification.PRIVATE, DataClassification.PRIVATE)
    assert not at_least(DataClassification.PUBLIC, DataClassification.PRIVATE)


def test_external_data_defaults_to_private_not_public() -> None:
    """When the classification is unstated, treat it as more sensitive."""
    assert ExternalData(source="s").classification is DataClassification.PRIVATE


# --- Timeouts ---------------------------------------------------------------


def test_every_timeout_is_finite_and_positive() -> None:
    policy = TimeoutPolicy()

    assert policy.connect_seconds > 0
    assert policy.read_seconds > 0
    assert policy.total_seconds > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"connect_seconds": 0}, {"read_seconds": 0}, {"total_seconds": 0},
        {"connect_seconds": -1}, {"read_seconds": -5}, {"total_seconds": -1},
        {"connect_seconds": None},
    ],
)
def test_a_disabled_timeout_is_refused(kwargs) -> None:
    """Zero means "no timeout" in most clients, so it must not be settable."""
    with pytest.raises((ValueError, TypeError)):
        TimeoutPolicy(**kwargs)


def test_the_total_bound_cannot_be_below_the_read_bound() -> None:
    with pytest.raises(ValueError):
        TimeoutPolicy(read_seconds=20, total_seconds=5)


def test_timeouts_are_separately_configurable() -> None:
    policy = TimeoutPolicy(connect_seconds=1, read_seconds=2, total_seconds=9)

    assert (policy.connect_seconds, policy.read_seconds, policy.total_seconds) == (
        1, 2, 9
    )


# --- Retries ----------------------------------------------------------------


def test_retrying_is_off_by_default() -> None:
    """One attempt unless someone opted in."""
    assert RetryPolicy().max_attempts == 1


def test_a_non_retryable_error_is_not_retried() -> None:
    calls = []

    def unauthorized(arguments):
        calls.append(1)
        raise ProviderUnauthorized()

    integration = FakeIntegration(
        responder=unauthorized, retries=RetryPolicy(max_attempts=5)
    )
    result = integration.invoke("lookup", {"query": "x"})

    assert len(calls) == 1
    assert result.attempts == 1
    assert result.state is ExternalResultState.UNAUTHORIZED


def test_a_retryable_error_is_retried_up_to_the_limit() -> None:
    calls = []

    def unavailable(arguments):
        calls.append(1)
        raise ProviderTimeout()

    integration = FakeIntegration(
        responder=unavailable,
        retries=RetryPolicy(max_attempts=3, backoff_seconds=0.01),
    )
    result = integration.invoke("lookup", {"query": "x"})

    assert len(calls) == 3
    assert result.attempts == 3
    assert result.state is ExternalResultState.TIMEOUT


def test_retrying_stops_once_it_succeeds() -> None:
    attempts = []

    def flaky(arguments):
        attempts.append(1)
        if len(attempts) < 2:
            raise ProviderTimeout()
        return _ok()

    integration = FakeIntegration(
        responder=flaky, retries=RetryPolicy(max_attempts=4, backoff_seconds=0.01)
    )
    result = integration.invoke("lookup", {"query": "x"})

    assert result.succeeded is True
    assert result.attempts == 2


def test_an_operation_with_a_side_effect_is_never_retried_by_default() -> None:
    """The most expensive default in the file.

    A retried send duplicates the message when the first request arrived and
    only its response was lost -- and the client cannot tell that case from a
    request that never arrived.
    """
    calls = []

    def unavailable(arguments):
        calls.append(1)
        raise ProviderTimeout()

    integration = FakeIntegration(
        responder=unavailable,
        retries=RetryPolicy(max_attempts=5, backoff_seconds=0.01),
        side_effect_operation=True,
    )
    integration.invoke("lookup", {"query": "x"})

    assert len(calls) == 1


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(
        backoff_seconds=1, backoff_multiplier=2, max_backoff_seconds=4
    )

    assert [policy.backoff_for(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 4, 4]


def test_a_provider_retry_after_is_honoured_but_clamped() -> None:
    """A provider must not be able to hold an execution open for hours."""
    policy = RetryPolicy(max_backoff_seconds=8)

    assert policy.backoff_for(1, retry_after=3) == 3
    assert policy.backoff_for(1, retry_after=21600) == 8


def test_the_total_duration_bound_stops_retrying() -> None:
    policy = RetryPolicy(max_attempts=5, max_total_seconds=10)

    assert policy.should_retry(ProviderTimeout(), attempt=1, elapsed_seconds=1)
    assert not policy.should_retry(ProviderTimeout(), attempt=1, elapsed_seconds=11)


def test_an_unbounded_retry_count_is_refused() -> None:
    for attempts in (0, -1, 6, 100):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=attempts)


def test_backoff_never_breaches_the_total_bound() -> None:
    """The wait itself counts. Taking it would breach the ceiling, so it stops."""
    slept = []

    def unavailable(arguments):
        raise ProviderRateLimited(retry_after=30)

    integration = FakeIntegration(
        responder=unavailable,
        retries=RetryPolicy(
            max_attempts=5, max_backoff_seconds=30, max_total_seconds=1
        ),
    )
    result = integration.invoke("lookup", {"query": "x"})

    assert integration.slept == []
    assert result.state is ExternalResultState.RATE_LIMITED
