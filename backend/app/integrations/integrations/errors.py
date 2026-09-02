"""Provider failures, as typed internal errors.

An external service can fail in ways nothing else in Mai can: it can be slow,
rate-limited, unreachable, or refuse credentials that worked yesterday. None
of those is "something went wrong", and collapsing them would leave the
response layer unable to say anything true about what happened.

Every error here is *converted* from whatever the provider raised. A provider
exception never reaches the user, because provider exceptions carry hostnames,
URLs, request bodies and sometimes the Authorization header that failed. What
crosses this boundary is a reason code and a safe detail string.
"""

from typing import Optional


class IntegrationError(Exception):
    """Base class. Carries a stable reason and nothing a provider wrote.

    `detail` is application text -- an operation name, an integration name, a
    field name. It is never a URL, a response body, a header, or an exception
    string, because all four routinely carry credentials or internal
    addresses.
    """

    reason: str = "integration_failed"
    #: Whether retrying this exact request could plausibly succeed. Declared
    #: per class rather than decided at the call site, so a new error type has
    #: to state its own answer instead of inheriting an optimistic default.
    retryable: bool = False

    def __init__(self, reason: str = "", detail: str = "") -> None:
        self.reason = reason or self.reason
        self.detail = detail
        super().__init__(self.reason)


# --- Configuration and registry ---------------------------------------------


class UnknownIntegration(IntegrationError):
    """No integration is registered under that name. Fails closed."""

    reason = "unknown_integration"


class IntegrationNotConfigured(IntegrationError):
    reason = "integration_not_configured"


class ProviderConfigurationError(IntegrationError):
    """The deployment is misconfigured. Retrying will not fix it."""

    reason = "provider_configuration_error"


class UnsupportedOperation(IntegrationError):
    """The integration does not offer that named operation.

    Raised rather than attempted. An integration exposes a fixed set of named
    operations precisely so that an unrecognised name is a refusal instead of
    a request nobody reviewed.
    """

    reason = "unsupported_operation"


# --- Credentials ------------------------------------------------------------


class CredentialsMissing(IntegrationError):
    reason = "credentials_missing"


class CredentialsExpired(IntegrationError):
    reason = "credentials_expired"


class CredentialScopeInsufficient(IntegrationError):
    """The credential exists but was not granted this permission.

    Distinct from `ProviderForbidden`: this is Mai declining before the call,
    because a read-only grant must not be spent attempting a write.
    """

    reason = "credential_scope_insufficient"


# --- Provider responses -----------------------------------------------------


class ProviderTimeout(IntegrationError):
    reason = "provider_timeout"
    retryable = True


class ProviderUnavailable(IntegrationError):
    reason = "provider_unavailable"
    retryable = True


class ProviderRateLimited(IntegrationError):
    """Retryable only in the sense that waiting may help.

    `retry_after` is the provider's own guidance where it supplied any. It is
    a number of seconds, clamped by the retry policy -- a provider asking for
    a six-hour wait must not be able to hold an execution open for six hours.
    """

    reason = "provider_rate_limited"
    retryable = True

    def __init__(
        self, reason: str = "", detail: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(reason, detail)
        self.retry_after = retry_after


class ProviderUnauthorized(IntegrationError):
    """Credentials rejected. Never retried: repeating a rejected credential
    achieves nothing and can trip a provider's lockout."""

    reason = "provider_unauthorized"


class ProviderForbidden(IntegrationError):
    """Authenticated, and not permitted. Never retried."""

    reason = "provider_forbidden"


class ProviderNotFound(IntegrationError):
    reason = "provider_not_found"


class ProviderValidationError(IntegrationError):
    """The request was malformed. Retrying sends the same malformed request."""

    reason = "provider_validation_error"


class ProviderInvalidResponse(IntegrationError):
    """The provider replied with something the adapter cannot parse.

    Not retryable by default. A provider returning HTML where JSON was
    promised is usually a captive portal, an error page or an incident, and
    hammering it is the wrong response.
    """

    reason = "provider_invalid_response"


class ResponseTooLarge(IntegrationError):
    """The response exceeded the policy bound and was not read.

    Refused rather than truncated: a truncated response is a partial answer
    that looks like a whole one, and the caller has no way to tell.
    """

    reason = "response_too_large"


# --- Network policy ---------------------------------------------------------


class NetworkPolicyViolation(IntegrationError):
    """A destination the policy does not allow. Never retried.

    The reason code is deliberately vague to whoever sees it. A refusal that
    said *why* a host was refused would be a way to map the network from
    outside -- which host is internal, which port answers, which range is
    private.
    """

    reason = "destination_not_allowed"


__all__ = [
    "CredentialScopeInsufficient",
    "CredentialsExpired",
    "CredentialsMissing",
    "IntegrationError",
    "IntegrationNotConfigured",
    "NetworkPolicyViolation",
    "ProviderConfigurationError",
    "ProviderForbidden",
    "ProviderInvalidResponse",
    "ProviderNotFound",
    "ProviderRateLimited",
    "ProviderTimeout",
    "ProviderUnauthorized",
    "ProviderUnavailable",
    "ProviderValidationError",
    "ResponseTooLarge",
    "UnknownIntegration",
    "UnsupportedOperation",
]
