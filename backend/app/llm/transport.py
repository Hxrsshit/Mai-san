"""The network policy for LLM provider traffic.

Stage 4F-B left one unpoliced outbound path: the LLM provider held its own
`httpx.AsyncClient` and never consulted `NetworkPolicy`. That was documented
as a deliberate exception -- the destination comes from operator
configuration, not from user input -- but "documented exception" and
"invariant" are different things, and only one of them survives a future
developer.

This module removes the exception. The provider now reaches the same
`SecureHttpClient` as web research, with a policy built here.

Trusted destination, still checked
----------------------------------

Research and provider traffic have genuinely different semantics:

    research:  a user's query   -> a destination fixed in code
    provider:  application data -> a destination fixed in configuration

The second is more trusted -- an operator setting `GROQ_BASE_URL` is not an
attacker -- and it is still checked, for two reasons. A misconfigured base URL
should not be able to reach the cloud metadata endpoint. And a policy with an
exemption in it is a policy someone will eventually widen.

So the provider gets a **single-host allow-list** derived from its configured
URL, and every other check -- scheme, port, address ranges, redirects --
applies exactly as it does to research.
"""

from typing import Optional
from urllib.parse import urlparse

from app.core.logging import get_logger
from app.integrations.errors import NetworkPolicyViolation
from app.integrations.http_client import SecureHttpClient
from app.integrations.policy import NetworkPolicy, RetryPolicy, TimeoutPolicy

logger = get_logger(__name__)

#: Providers POST completions and do nothing else.
#:
#: Not `{"GET", "POST"}`. The provider has exactly one operation, and a method
#: it never uses is a capability waiting to be found by something else.
PROVIDER_METHODS = frozenset({"POST"})

#: A completion response is large by API standards and small by transfer
#: standards. Two megabytes is far above any real completion and far below
#: anything that would strain memory.
MAX_PROVIDER_RESPONSE_BYTES = 2_000_000

#: The transport performs **no** retries for provider traffic.
#:
#: The provider already has its own retry loop, and it is the better one: it
#: honours `Retry-After`, distinguishes retryable statuses from caller errors,
#: and raises typed LLM errors the API layer maps to HTTP responses. Layering
#: the transport's retries underneath would multiply attempts -- three
#: transport attempts inside three provider attempts is nine requests to a
#: rate-limited endpoint -- and would break the bounded-retry guarantee both
#: layers are trying to make.
NO_TRANSPORT_RETRIES = RetryPolicy(max_attempts=1)


def provider_policy(
    base_url: str,
    timeout_seconds: float,
    connect_seconds: float = 10.0,
    extra_request_headers=frozenset(),
) -> NetworkPolicy:
    """Build the network policy for one configured provider endpoint.

    Fails closed. A base URL that cannot be parsed, is not HTTPS, names no
    host, or resolves into a forbidden range produces a policy that permits
    nothing reachable -- rather than a policy that permits everything, which
    is what an empty allow-list would mean if the direction were reversed.
    """
    host = provider_host(base_url)

    # The outer bound is exactly `connect + read`: the two phases a single
    # request has. Provider redirects are refused and transport retries are
    # disabled, so there is no third phase to leave room for -- and an
    # arbitrary margin above the sum would be a number nobody could justify.
    read = max(1.0, float(timeout_seconds))
    connect = max(1.0, min(float(connect_seconds), read))

    return NetworkPolicy(
        allowed_hosts=frozenset({host}),
        allowed_methods=PROVIDER_METHODS,
        max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        # A completions endpoint has no reason to redirect, and following one
        # would mean re-POSTing application data to a destination the origin
        # chose. Refused outright.
        follow_redirects=False,
        timeouts=TimeoutPolicy(
            connect_seconds=connect,
            read_seconds=read,
            total_seconds=connect + read,
        ),
        retries=NO_TRANSPORT_RETRIES,
        # Named by the provider that needs them, not open to every caller.
        extra_request_headers=frozenset(extra_request_headers),
    )


def provider_host(base_url: str) -> str:
    """The host from a configured provider URL, or raise.

    Raising rather than returning `""`: an empty host would build a policy
    with an empty allow-list, which refuses everything -- safe, but it would
    surface at request time as a confusing destination refusal instead of at
    startup as the configuration error it is.
    """
    try:
        parsed = urlparse((base_url or "").strip())
    except ValueError as exc:
        raise NetworkPolicyViolation(detail="provider_url") from exc

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise NetworkPolicyViolation(detail="provider_url")
    return host


def build_provider_client(
    base_url: str,
    timeout_seconds: float,
    transport=None,
    resolve=None,
    extra_request_headers=frozenset(),
) -> SecureHttpClient:
    """The one client a provider gets. Same class research uses.

    `transport` is injectable so provider tests drive a stub -- and the policy
    still runs against it, which is what makes those tests meaningful rather
    than decorative.
    """
    policy = provider_policy(
        base_url, timeout_seconds, extra_request_headers=extra_request_headers
    )
    logger.debug(
        "Provider network policy built",
        # The host, which is configuration and not a secret. Never the key,
        # and never the full URL, which could carry one in a path segment.
        extra={"provider_host": next(iter(policy.allowed_hosts), "")},
    )
    return SecureHttpClient(policy=policy, transport=transport, resolve=resolve)


__all__ = [
    "MAX_PROVIDER_RESPONSE_BYTES",
    "NO_TRANSPORT_RETRIES",
    "PROVIDER_METHODS",
    "build_provider_client",
    "provider_host",
    "provider_policy",
]
