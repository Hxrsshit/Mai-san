"""The Integration contract: named operations, and nothing else.

A `Tool` answers *what operation can Mai perform?* An `Integration` answers
*which external service provides it, and how do we talk to that service?* The
two are separate registries on purpose -- one capability may later be served
by a different provider without the capability changing, and one provider may
serve several capabilities.

The single most important thing about this interface is what it does **not**
have. There is no

    request(url, method, headers, body)

and there will not be one. That method is an arbitrary API client: it makes
every endpoint the provider has reachable, it makes SSRF a matter of choosing
a URL, and it moves the decision about what Mai may do from code review to
runtime. Instead an integration exposes a fixed set of **named operations**,
each written out by hand, each with its own arguments and its own result.

An operation name that is not registered is refused. Not attempted, not
guessed at, not looked up dynamically -- refused, because the set of things an
integration can do should be readable in one file.
"""

import asyncio
import enum
import inspect
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from app.core.logging import get_logger
from app.integrations.credentials import (
    CredentialRequirement,
    CredentialResolver,
    CredentialState,
    CredentialStatus,
    CredentialType,
    get_credential_resolver,
)
from app.integrations.errors import (
    IntegrationError,
    ProviderRateLimited,
    UnsupportedOperation,
)
from app.integrations.policy import NetworkPolicy
from app.integrations.result import ExternalResult, ExternalResultState

logger = get_logger(__name__)


class IntegrationState(str, enum.Enum):
    """Whether an integration could be used right now.

    **Not a tool authorization state**, and the distinction is load-bearing.
    Authorization answers "is Mai permitted to do this?"; this answers "is
    the service reachable and authenticated?". A tool can be perfectly
    authorized and still unusable because a key is missing, and reporting
    that as "forbidden" would send someone to look at the wrong thing.
    """

    #: No credential configured. The normal state for an unused integration.
    NOT_CONFIGURED = "not_configured"
    #: Configuration present, credential not yet verified.
    CONFIGURED = "configured"
    #: OAuth-shaped: configured, but the user has not authorised an account.
    AUTHENTICATION_REQUIRED = "authentication_required"
    AVAILABLE = "available"
    EXPIRED = "expired"
    #: Switched off by an operator. Deliberate, not a fault.
    DISABLED = "disabled"
    #: Known to be failing. Reserved for a future health check that observes
    #: a real failure; never set by guessing.
    UNAVAILABLE = "unavailable"


#: The one state in which an operation may be attempted.
USABLE_INTEGRATION_STATES = frozenset({IntegrationState.AVAILABLE})


class OperationSpec:
    """One named thing an integration can do.

    Declared in code, in the integration's own module. The `handler` is a
    bound method looked up from a dictionary the integration built itself --
    never resolved from a string at call time, so there is no path from a
    name in a request to an arbitrary callable.
    """

    __slots__ = ("name", "handler", "has_side_effect", "description")

    def __init__(
        self,
        name: str,
        handler: Callable[..., Any],
        has_side_effect: bool = False,
        description: str = "",
    ) -> None:
        self.name = name
        self.handler = handler
        #: Whether this changes something outside Mai. Governs retrying: an
        #: operation with a side effect is not retried unless the retry
        #: policy explicitly permits it and the provider offers idempotency.
        self.has_side_effect = has_side_effect
        self.description = description


class _Attempts:
    """Retry bookkeeping shared by the sync and async invoke loops.

    Extracted so the two loops differ only in how they wait -- `time.sleep`
    against `asyncio.sleep`. Every decision about *whether* to retry and *how
    long* to wait lives here and in `RetryPolicy`, so the two paths cannot
    drift into disagreeing about when a retry is safe.
    """

    __slots__ = ("policy", "has_side_effect", "started", "count", "last")

    def __init__(self, policy, has_side_effect: bool) -> None:
        self.policy = policy
        self.has_side_effect = has_side_effect
        self.started = time.monotonic()
        self.count = 0
        self.last: Optional[IntegrationError] = None

    def begin(self) -> None:
        self.count += 1

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def delay_for(self, error: IntegrationError) -> Optional[float]:
        """Seconds to wait before the next attempt, or None to stop.

        `None` covers both "policy says no" and "the wait itself would breach
        the total bound" -- there is no point taking a delay that ends after
        the deadline.
        """
        self.last = error
        if not self.policy.should_retry(
            error, self.count, self.elapsed, has_side_effect=self.has_side_effect
        ):
            return None

        delay = self.policy.backoff_for(
            self.count, getattr(error, "retry_after", None)
        )
        if self.elapsed + delay >= self.policy.max_total_seconds:
            return None
        return delay


class Integration(ABC):
    """An adapter for one external service.

    Subclasses declare metadata, a credential requirement, a network policy
    and a fixed operation table. They do not accept a session, a context
    package, a conversation, a memory or a provider -- an integration receives
    the arguments for one operation and nothing else, which is what
    data minimisation means here rather than being a rule someone has to
    remember at each call site.
    """

    #: Canonical name. Referenced by tools; never supplied by a request.
    name: str = ""
    description: str = ""
    #: Which external service this speaks to, for reporting.
    provider: str = ""

    def __init__(
        self,
        credentials: Optional[CredentialResolver] = None,
        enabled: bool = True,
    ) -> None:
        self._credentials = credentials or get_credential_resolver()
        self._enabled = enabled
        # Built once, at construction, from a method the subclass wrote. The
        # table is the complete set of things this integration can do.
        self._operations: Dict[str, OperationSpec] = {
            spec.name: spec for spec in self.declare_operations()
        }

    # --- Declarations a subclass supplies -----------------------------------

    @abstractmethod
    def declare_operations(self) -> Tuple[OperationSpec, ...]:
        """Every operation this integration offers. Fixed at construction."""

    @property
    def credential_requirement(self) -> CredentialRequirement:
        """What this integration needs to authenticate.

        Defaults to needing nothing, so an integration that genuinely needs no
        credential does not have to pretend otherwise.
        """
        return CredentialRequirement(
            identifier=f"{self.name}.none",
            provider=self.provider or self.name,
            credential_type=CredentialType.NONE,
        )

    @property
    def network_policy(self) -> NetworkPolicy:
        """Which destinations this integration may reach.

        The default allows **nothing**: an empty host allow-list refuses every
        URL. An integration that talks to a service names that service's hosts
        explicitly, and gets those and no others.
        """
        return NetworkPolicy()

    # --- State --------------------------------------------------------------

    def credential_state(self) -> CredentialState:
        return self._credentials.describe(self.credential_requirement)

    def state(self) -> IntegrationState:
        """Whether this integration could be used right now.

        Derived from the operator switch and the credential, in that order --
        a disabled integration is disabled whatever its credentials say, and
        reporting it as "not configured" would send someone to add a key that
        would change nothing.
        """
        if not self._enabled:
            return IntegrationState.DISABLED

        credential = self.credential_state()
        if credential.status is CredentialStatus.AVAILABLE:
            return IntegrationState.AVAILABLE
        if credential.status is CredentialStatus.EXPIRED:
            return IntegrationState.EXPIRED
        if credential.status is CredentialStatus.INSUFFICIENT_SCOPE:
            return IntegrationState.AUTHENTICATION_REQUIRED
        return IntegrationState.NOT_CONFIGURED

    @property
    def available(self) -> bool:
        return self.state() in USABLE_INTEGRATION_STATES

    def operation_names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._operations))

    def supports(self, operation: str) -> bool:
        return (operation or "").strip() in self._operations

    # --- Invocation ---------------------------------------------------------

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> ExternalResult:
        """Run one named operation, bounded and with failures converted.

        The only way an operation is reached. Four things happen here that a
        per-integration implementation would otherwise each have to remember:
        the operation name is checked against the table, availability is
        checked, retries are bounded by policy, and every provider exception
        is converted to an `ExternalResult` rather than escaping.

        Never raises for a provider failure. A failed call is a *result* with
        a state, because the caller needs to record it and say something true
        about it -- an exception would make "the provider was rate limited"
        indistinguishable from a bug.
        """
        spec = self._operations.get((operation or "").strip())
        if spec is None:
            # Refused, not attempted. There is no fallback that searches,
            # guesses, or forwards the name to the provider.
            raise UnsupportedOperation(detail=f"{self.name}:{operation}")

        if not self.available:
            return self._failure(
                spec.name,
                ExternalResultState.UNAVAILABLE,
                reason=self.state().value,
                summary="The integration is not available.",
            )

        attempts = _Attempts(self.network_policy.retries, spec.has_side_effect)

        while True:
            attempts.begin()
            try:
                result = spec.handler(dict(arguments))
            except IntegrationError as error:
                delay = attempts.delay_for(error)
                if delay is None:
                    break
                self._sleep(delay)
                continue
            except Exception as unexpected:  # noqa: BLE001
                # An adapter bug or an exception type nobody converted. The
                # message is dropped rather than reported: it may carry a URL,
                # a body, or an Authorization header.
                logger.warning(
                    "Integration operation raised an unconverted exception",
                    extra={
                        "integration": self.name,
                        "operation": spec.name,
                        "error_type": type(unexpected).__name__,
                    },
                )
                return self._failure(
                    spec.name,
                    ExternalResultState.UNKNOWN_ERROR,
                    reason="unconverted_error",
                    summary="The integration failed unexpectedly.",
                    attempts=attempts.count,
                    latency_ms=self._elapsed_ms(attempts.started),
                )
            else:
                return result.model_copy(
                    update={
                        "attempts": attempts.count,
                        "latency_ms": self._elapsed_ms(attempts.started),
                    }
                )

        return self._failure(
            spec.name,
            _STATE_FOR_ERROR.get(
                type(attempts.last), ExternalResultState.FAILED
            ),
            reason=attempts.last.reason if attempts.last else "failed",
            summary="The external operation did not succeed.",
            attempts=attempts.count,
            latency_ms=self._elapsed_ms(attempts.started),
        )

    async def ainvoke(
        self, operation: str, arguments: Mapping[str, Any]
    ) -> ExternalResult:
        """The async twin of `invoke`, for operations that do real I/O.

        Both exist because both are genuinely needed: an adapter over an
        in-process resource is naturally synchronous, and one that opens a
        socket must not block the event loop for seconds. The Stage 4E
        dispatcher awaits an awaitable result, so a tool chooses which it is
        and no gate above it changes.

        Identical guarantees to `invoke`: the operation name is checked
        against the table, availability is checked, retries are bounded by
        the same policy object, and every provider exception is converted to
        a result rather than escaping.
        """
        spec = self._operations.get((operation or "").strip())
        if spec is None:
            raise UnsupportedOperation(detail=f"{self.name}:{operation}")

        if not self.available:
            return self._failure(
                spec.name,
                ExternalResultState.UNAVAILABLE,
                reason=self.state().value,
                summary="The integration is not available.",
            )

        attempts = _Attempts(self.network_policy.retries, spec.has_side_effect)

        while True:
            attempts.begin()
            try:
                result = spec.handler(dict(arguments))
                if inspect.isawaitable(result):
                    result = await result
            except IntegrationError as error:
                delay = attempts.delay_for(error)
                if delay is None:
                    break
                await self._asleep(delay)
                continue
            except Exception as unexpected:  # noqa: BLE001
                logger.warning(
                    "Integration operation raised an unconverted exception",
                    extra={
                        "integration": self.name,
                        "operation": spec.name,
                        "error_type": type(unexpected).__name__,
                    },
                )
                return self._failure(
                    spec.name,
                    ExternalResultState.UNKNOWN_ERROR,
                    reason="unconverted_error",
                    summary="The integration failed unexpectedly.",
                    attempts=attempts.count,
                    latency_ms=self._elapsed_ms(attempts.started),
                )
            else:
                return result.model_copy(
                    update={
                        "attempts": attempts.count,
                        "latency_ms": self._elapsed_ms(attempts.started),
                    }
                )

        return self._failure(
            spec.name,
            _STATE_FOR_ERROR.get(
                type(attempts.last), ExternalResultState.FAILED
            ),
            reason=attempts.last.reason if attempts.last else "failed",
            summary="The external operation did not succeed.",
            attempts=attempts.count,
            latency_ms=self._elapsed_ms(attempts.started),
        )

    # --- Helpers ------------------------------------------------------------

    def _failure(
        self, operation: str, state: ExternalResultState, reason: str,
        summary: str, attempts: int = 1, latency_ms: Optional[int] = None,
    ) -> ExternalResult:
        return ExternalResult(
            state=state,
            integration=self.name,
            operation=operation,
            summary=summary,
            reason=reason,
            attempts=attempts,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _sleep(self, seconds: float) -> None:
        """Overridable so tests exercise backoff without waiting for it."""
        time.sleep(seconds)

    async def _asleep(self, seconds: float) -> None:
        """The async twin. Also overridable, for the same reason."""
        await asyncio.sleep(seconds)


#: Which result state each error converts to.
#:
#: A table rather than a chain of `isinstance`, so a new error type has to be
#: given a state deliberately. The fallback is `FAILED`, never `SUCCESS`.
_STATE_FOR_ERROR = {}


def _build_error_states():
    from app.integrations import errors as e

    return {
        e.ProviderTimeout: ExternalResultState.TIMEOUT,
        e.ProviderRateLimited: ExternalResultState.RATE_LIMITED,
        e.ProviderUnauthorized: ExternalResultState.UNAUTHORIZED,
        e.ProviderForbidden: ExternalResultState.FORBIDDEN,
        e.ProviderNotFound: ExternalResultState.NOT_FOUND,
        e.ProviderValidationError: ExternalResultState.VALIDATION_ERROR,
        e.ProviderUnavailable: ExternalResultState.UNAVAILABLE,
        e.ProviderInvalidResponse: ExternalResultState.FAILED,
        e.ResponseTooLarge: ExternalResultState.FAILED,
        e.NetworkPolicyViolation: ExternalResultState.FORBIDDEN,
        e.CredentialsMissing: ExternalResultState.UNAUTHORIZED,
        e.CredentialsExpired: ExternalResultState.UNAUTHORIZED,
        e.CredentialScopeInsufficient: ExternalResultState.FORBIDDEN,
        e.ProviderConfigurationError: ExternalResultState.FAILED,
    }


_STATE_FOR_ERROR.update(_build_error_states())


__all__ = [
    "Integration",
    "IntegrationState",
    "OperationSpec",
    "USABLE_INTEGRATION_STATES",
]
