"""Bounds on talking to the outside world.

Three policies, all of them ceilings rather than defaults an integration can
raise. An integration declares what it needs; the policy decides what it gets,
and the smaller of the two wins. That direction matters: a policy an
integration could widen is a suggestion.

Nothing here opens a socket. Stage 4F-A deliberately ships no HTTP client at
all -- these are the rules a future one must obey, written before there is
anything to be tempted into shipping without them.
"""

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple
from urllib.parse import urlparse

from app.core.logging import get_logger
from app.integrations.errors import NetworkPolicyViolation

logger = get_logger(__name__)

#: Schemes that may ever be used. Everything else is refused by absence.
#:
#: `file:` reads the local disk. `ftp:`, `gopher:` and friends are protocol
#: smuggling primitives against some clients. `data:` is not a network
#: destination at all. None of them is denied by name -- an allow-list means a
#: scheme nobody thought of is refused rather than permitted.
ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"https"})

#: Ports an integration may reach. HTTPS only, matching the scheme list.
ALLOWED_PORTS: FrozenSet[int] = frozenset({443})

#: Address ranges that are never a legitimate integration destination.
#:
#: This is the SSRF list. The cloud metadata endpoints are the reason it
#: exists in a stage with no HTTP client: 169.254.169.254 hands out instance
#: credentials to anything that asks, and a request to it from inside the
#: deployment is indistinguishable from a legitimate one.
_FORBIDDEN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",        # this host
        "10.0.0.0/8",       # private
        "100.64.0.0/10",    # carrier-grade NAT
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local, includes cloud metadata
        "172.16.0.0/12",    # private
        "192.0.0.0/24",     # IETF protocol assignments
        "192.168.0.0/16",   # private
        "198.18.0.0/15",    # benchmarking
        "224.0.0.0/4",      # multicast
        "240.0.0.0/4",      # reserved
        "255.255.255.255/32",
        "::1/128",          # loopback
        "fc00::/7",         # unique local
        "fe80::/10",        # link-local
        "::ffff:0:0/96",    # IPv4-mapped, so mapping cannot bypass the list
    )
)

#: Hostnames that resolve to somewhere local whatever DNS says.
_FORBIDDEN_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
})


@dataclass(frozen=True)
class TimeoutPolicy:
    """Bounded execution, in three layers.

    Three numbers rather than one, because they fail differently. A connect
    that hangs is usually a firewall silently dropping packets; a read that
    hangs is usually a provider incident; a total bound is what stops a
    sequence of individually-acceptable retries from holding an execution open
    for minutes. The dispatcher must stay responsive, so all three are
    finite and none can be disabled.
    """

    connect_seconds: float = 5.0
    read_seconds: float = 15.0
    #: Covers every attempt including backoff. The one an operator should
    #: reason about, because it is the only one a user would feel.
    total_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("connect_seconds", "read_seconds", "total_seconds"):
            value = getattr(self, name)
            if not value or value <= 0:
                # Zero or negative would mean "no timeout" in most clients.
                # A misconfigured bound must fail closed, not become infinite.
                raise ValueError(f"{name} must be positive")
        if self.total_seconds < self.read_seconds:
            raise ValueError("total_seconds cannot be below read_seconds")


@dataclass(frozen=True)
class RetryPolicy:
    """When a failed call may be tried again -- which is rarely.

    Retrying is not free and it is not safe by default. Repeating a rejected
    credential can trip a lockout; repeating a malformed request produces the
    same rejection; and repeating a request that *did* arrive but whose
    response was lost can duplicate a side effect. So the default is one
    attempt, and retrying is opt-in per error type via
    `IntegrationError.retryable`.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 8.0
    #: Total wall-clock ceiling across all attempts and all backoff.
    max_total_seconds: float = 30.0

    #: Whether an operation with a side effect may be retried at all.
    #:
    #: False by default and the safest thing in this file. A retried
    #: `send_email` sends two emails if the first request arrived and only its
    #: response was lost -- and the client cannot tell that case from a
    #: request that never arrived. An integration must opt in per operation,
    #: and only where the provider offers an idempotency key.
    retry_side_effects: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_attempts > 5:
            # An arbitrary ceiling, but an unbounded one is how a rate limit
            # becomes an outage. Five is already more than most calls deserve.
            raise ValueError("max_attempts above 5 is not permitted")
        if self.max_total_seconds <= 0:
            raise ValueError("max_total_seconds must be positive")

    def should_retry(
        self, error: Exception, attempt: int, elapsed_seconds: float,
        has_side_effect: bool = False,
    ) -> bool:
        """Whether to try again. Four independent reasons to say no.

        Every one of them must pass. The order is not significant -- they are
        conjunctive -- but the side-effect check is listed first because it is
        the one whose absence would be most expensive.
        """
        if has_side_effect and not self.retry_side_effects:
            return False
        if not getattr(error, "retryable", False):
            return False
        if attempt >= self.max_attempts:
            return False
        if elapsed_seconds >= self.max_total_seconds:
            return False
        return True

    def backoff_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """How long to wait before attempt `attempt + 1`.

        A provider's own `Retry-After` is honoured where it gave one, but
        clamped: a provider asking for a six-hour wait must not be able to
        hold an execution open for six hours.
        """
        if retry_after is not None and retry_after > 0:
            return min(float(retry_after), self.max_backoff_seconds)
        delay = self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        return min(delay, self.max_backoff_seconds)


@dataclass(frozen=True)
class NetworkPolicy:
    """Which destinations an integration may reach.

    An **allow-list of hosts**, declared by the integration in code. There is
    no wildcard that means "anywhere", and `allowed_hosts` being empty refuses
    everything rather than permitting everything -- the direction an empty
    collection fails in is the whole point.

    This is what stops a future integration from becoming a general HTTP
    client. A destination that is not in a registered integration's own
    allow-list has no way to be reached, because there is no code path that
    takes a URL from anywhere else.
    """

    allowed_hosts: FrozenSet[str] = frozenset()
    allowed_schemes: FrozenSet[str] = ALLOWED_SCHEMES
    allowed_ports: FrozenSet[int] = ALLOWED_PORTS

    max_response_bytes: int = 2_000_000
    #: How many redirect hops may be followed. Each one is re-checked, so
    #: this bounds work rather than trust -- but an unbounded chain is a
    #: denial-of-service against Mai whether or not each hop is safe.
    max_redirects: int = 3
    #: Redirects are not followed. A redirect is the provider choosing a new
    #: destination after the policy already approved the first one, which is
    #: exactly the check being bypassed. An integration that needs to follow
    #: one re-enters through `check` with the new URL.
    follow_redirects: bool = False

    timeouts: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    retries: RetryPolicy = field(default_factory=RetryPolicy)

    def check(self, url: str, resolve=None) -> None:
        """Raise unless this exact URL is permitted. Returns nothing.

        Raising rather than returning a boolean, so a caller cannot forget to
        look at the answer -- the failure mode of an ignored boolean here is
        an unreviewed outbound request.

        `resolve` is injectable so tests can exercise DNS rebinding without a
        network. In production it is `socket.getaddrinfo`.
        """
        try:
            parsed = urlparse((url or "").strip())
            # `.port` parses lazily and raises on a non-numeric or
            # out-of-range port, so it is touched inside the guard too.
            declared_port = parsed.port
        except ValueError as exc:
            # A URL this function cannot parse is a URL it cannot vouch for.
            # `urlparse` raises on malformed IPv6 (`https://[::1`) and on an
            # invalid port, and letting that escape would surface a raw
            # ValueError instead of a refusal -- indistinguishable from a bug
            # elsewhere, and not the fail-closed behaviour this stage
            # requires.
            raise NetworkPolicyViolation(detail="malformed") from exc

        if parsed.scheme.lower() not in self.allowed_schemes:
            raise NetworkPolicyViolation(detail="scheme")

        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise NetworkPolicyViolation(detail="host")

        if host in _FORBIDDEN_HOSTS:
            raise NetworkPolicyViolation(detail="host")

        port = declared_port or (443 if parsed.scheme.lower() == "https" else 0)
        if port not in self.allowed_ports:
            raise NetworkPolicyViolation(detail="port")

        # The allow-list. Exact match or a subdomain of a listed host --
        # never a substring, which would let `evil-example.com` pass a list
        # containing `example.com`.
        if not any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self.allowed_hosts
        ):
            raise NetworkPolicyViolation(detail="host")

        # And where it actually points. A name on the allow-list that
        # resolves into a private range is DNS rebinding, and the allow-list
        # alone would have permitted it.
        for address in self._addresses(host, port, resolve):
            if is_forbidden_address(address):
                logger.warning(
                    "Refused a destination resolving to a restricted address",
                    # No address and no host: a refusal that named them would
                    # be a way to map the network from outside.
                    extra={"integration_check": "address"},
                )
                raise NetworkPolicyViolation(detail="address")

    def _addresses(self, host: str, port: int, resolve) -> Tuple[str, ...]:
        """Every address the host resolves to, or the literal if it is one."""
        try:
            return (str(ipaddress.ip_address(host)),)
        except ValueError:
            pass

        resolver = resolve if resolve is not None else socket.getaddrinfo
        try:
            return tuple(
                str(info[4][0]) for info in resolver(host, port)
            )
        except Exception:  # noqa: BLE001
            # A name that cannot be resolved is refused rather than allowed
            # through to a client that would resolve it again itself.
            raise NetworkPolicyViolation(detail="address") from None


def is_forbidden_address(address: str) -> bool:
    """True for anything in a range no integration may reach.

    Unparseable is forbidden. A value this function cannot understand is not
    a value it can vouch for.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True

    # IPv4-mapped IPv6 is unwrapped before the range check. Without this,
    # ::ffff:127.0.0.1 would miss every IPv4 rule in the list.
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped

    return any(parsed in network for network in _FORBIDDEN_NETWORKS)


__all__ = [
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "NetworkPolicy",
    "RetryPolicy",
    "TimeoutPolicy",
    "is_forbidden_address",
]
