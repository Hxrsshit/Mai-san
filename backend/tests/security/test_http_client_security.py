"""Stage 4F-B: SSRF and network-boundary tests at the client itself.

Part 5 is explicit that testing `policy.check(url)` is not enough. Every test
here drives `SecureHttpClient` and then asserts against
`transport.connections` -- the list of destinations that got as far as the
transport. A refused URL leaves it **empty**, which is a much stronger claim
than "a helper raised": it says nothing would have been dialled.
"""

import asyncio
import json

import httpx
import pytest

from app.integrations.errors import (
    NetworkPolicyViolation,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseTooLarge,
)
from app.integrations.http_client import (
    ALLOWED_REQUEST_HEADERS,
    USER_AGENT,
    SecureHttpClient,
)
from app.integrations.policy import NetworkPolicy, RetryPolicy, TimeoutPolicy
from tests.support.stub_transport import StubTransport, brave_payload

ALLOWED_HOST = "api.example.com"


def _client(transport=None, resolves_to="93.184.216.34", **policy_kwargs):
    def resolve(host, port):
        return [(2, 1, 6, "", (resolves_to, port))]

    policy = NetworkPolicy(
        allowed_hosts=frozenset({ALLOWED_HOST}), **policy_kwargs
    )
    return SecureHttpClient(
        policy=policy, transport=transport or StubTransport(), resolve=resolve
    )


# --- The happy path, so the refusals below mean something -------------------


async def test_an_allowed_destination_is_reached() -> None:
    transport = StubTransport(payload={"ok": True})
    client = _client(transport)

    response = await client.get(f"https://{ALLOWED_HOST}/v1/search")

    assert response.status_code == 200
    assert json.loads(response.content) == {"ok": True}
    assert transport.connections == [f"https://{ALLOWED_HOST}/v1/search"]


# --- SSRF: nothing prohibited ever reaches the transport --------------------


@pytest.mark.parametrize(
    "url",
    [
        # Loopback, in every form.
        "https://127.0.0.1/x", "https://127.1.2.3/x", "https://localhost/x",
        "https://0.0.0.0/x", "https://[::1]/x",
        # Private IPv4.
        "https://10.0.0.1/x", "https://172.16.5.4/x", "https://192.168.1.1/x",
        "https://100.64.0.1/x",
        # Private and link-local IPv6.
        "https://[fc00::1]/x", "https://[fe80::1]/x",
        # Cloud metadata services.
        "https://169.254.169.254/latest/meta-data/",
        "https://169.254.170.2/v2/credentials",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://metadata/x", "https://instance-data/x",
        # Unsafe schemes.
        "http://api.example.com/x", "file:///etc/passwd",
        "ftp://api.example.com/x", "gopher://api.example.com/_",
        "data:text/plain,hello", "javascript:alert(1)",
        "dict://api.example.com:11211/", "ldap://api.example.com/",
        # Arbitrary ports on an allow-listed host.
        "https://api.example.com:22/x", "https://api.example.com:8080/x",
        "https://api.example.com:6379/x", "https://api.example.com:11211/x",
        # Hosts that only look allow-listed.
        "https://api.example.com.attacker.test/x",
        "https://notapi.example.com/x", "https://evil-api.example.com/x",
        # Malformed.
        "", "   ", "https://", "not a url", "https://[::1",
    ],
)
async def test_no_prohibited_destination_is_ever_dialled(url) -> None:
    """The strongest form of the claim: the transport was never asked."""
    transport = StubTransport()
    client = _client(transport)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(url)

    assert transport.connections == []


async def test_an_ipv4_mapped_loopback_is_refused_at_the_client() -> None:
    """`::ffff:127.0.0.1` would miss every IPv4 rule without unwrapping."""
    transport = StubTransport()
    client = _client(transport, resolves_to="::ffff:127.0.0.1")

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/x")

    assert transport.connections == []


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "169.254.169.254", "10.1.2.3", "192.168.0.5", "::1", "fc00::5"],
)
async def test_dns_rebinding_is_refused_at_the_client(address) -> None:
    """An allow-listed name that resolves somewhere private never connects."""
    transport = StubTransport()
    client = _client(transport, resolves_to=address)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/x")

    assert transport.connections == []


async def test_an_unresolvable_host_never_connects() -> None:
    def nxdomain(host, port):
        raise OSError("no such host")

    transport = StubTransport()
    policy = NetworkPolicy(allowed_hosts=frozenset({ALLOWED_HOST}))
    client = SecureHttpClient(policy, transport=transport, resolve=nxdomain)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/x")

    assert transport.connections == []


async def test_an_empty_allow_list_dials_nothing() -> None:
    transport = StubTransport()
    client = SecureHttpClient(NetworkPolicy(), transport=transport)

    with pytest.raises(NetworkPolicyViolation):
        await client.get("https://anything.test/x")

    assert transport.connections == []


# --- Redirects --------------------------------------------------------------


def _redirect_to(location, then=None):
    return StubTransport(
        responses=[
            {"status_code": 302, "headers": {"location": location}},
            then or {"status_code": 200, "payload": {"ok": True}},
        ]
    )


async def test_redirects_are_refused_when_the_policy_forbids_them() -> None:
    """Default behaviour: a redirect is not followed at all."""
    transport = _redirect_to(f"https://{ALLOWED_HOST}/second")
    client = _client(transport)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/first")

    # The first hop happened; the second did not.
    assert len(transport.connections) == 1


async def test_a_safe_redirect_is_followed_when_permitted() -> None:
    transport = _redirect_to(f"https://{ALLOWED_HOST}/second")
    client = _client(transport, follow_redirects=True)

    response = await client.get(f"https://{ALLOWED_HOST}/first")

    assert response.status_code == 200
    assert transport.connections == [
        f"https://{ALLOWED_HOST}/first", f"https://{ALLOWED_HOST}/second",
    ]


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1/stolen",
        "https://localhost/stolen",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.1/internal",
        "https://[::1]/stolen",
        "http://api.example.com/downgrade",
        "file:///etc/passwd",
        "gopher://api.example.com/_",
        "https://attacker.test/exfiltrate",
        "https://api.example.com:22/x",
    ],
)
async def test_an_unsafe_redirect_target_is_refused(target) -> None:
    """A safe first URL does not make an unsafe second one safe.

    The redirect target is checked independently, by the same policy, before
    the second connection is attempted -- so exactly one destination reaches
    the transport and it is the one that was allowed.
    """
    transport = _redirect_to(target)
    client = _client(transport, follow_redirects=True)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/first")

    assert transport.connections == [f"https://{ALLOWED_HOST}/first"]


async def test_a_redirect_to_a_rebinding_host_is_refused() -> None:
    """The address check runs on the redirect target too, not just the first."""
    transport = _redirect_to(f"https://{ALLOWED_HOST}/second")
    client = _client(transport, follow_redirects=True, resolves_to="127.0.0.1")

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/first")

    assert transport.connections == []


async def test_a_redirect_chain_is_bounded() -> None:
    """An endless chain is a denial of service even when every hop is safe."""
    transport = StubTransport(
        responses=[
            {"status_code": 302, "headers": {"location": f"https://{ALLOWED_HOST}/{n}"}}
            for n in range(20)
        ]
    )
    client = _client(transport, follow_redirects=True, max_redirects=3)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/first")

    assert len(transport.connections) <= 4


async def test_an_empty_redirect_location_is_refused() -> None:
    transport = StubTransport(
        responses=[{"status_code": 302, "headers": {"location": ""}}]
    )
    client = _client(transport, follow_redirects=True)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/first")


async def test_a_relative_redirect_is_resolved_and_rechecked() -> None:
    """Relative targets are ordinary and cannot change host -- but are checked."""
    transport = _redirect_to("/second")
    client = _client(transport, follow_redirects=True)

    response = await client.get(f"https://{ALLOWED_HOST}/first")

    assert response.status_code == 200
    assert transport.connections[-1] == f"https://{ALLOWED_HOST}/second"


# --- Response bounds --------------------------------------------------------


async def test_an_oversized_response_is_refused_not_truncated() -> None:
    """Truncating would produce a partial answer that looks like a whole one."""
    transport = StubTransport(body=b"x" * 5_000_000)
    client = _client(transport, max_response_bytes=1000)

    with pytest.raises(ResponseTooLarge):
        await client.get(f"https://{ALLOWED_HOST}/big")


async def test_a_response_at_the_limit_is_accepted() -> None:
    """The bound is a ceiling, not an off-by-one refusal."""
    transport = StubTransport(body=b"x" * 1000)
    client = _client(transport, max_response_bytes=1000)

    response = await client.get(f"https://{ALLOWED_HOST}/ok")

    assert len(response.content) == 1000


async def test_the_client_never_advertises_compression() -> None:
    """A bound on decompressed bytes is only enforceable if nothing inflates.

    Advertising `gzip` would mean the transport inflates before the size
    check could run, which is exactly how a decompression bomb gets past a
    byte limit.
    """
    transport = StubTransport()
    client = _client(transport)

    await client.get(f"https://{ALLOWED_HOST}/x")

    assert transport.request_headers[0]["accept-encoding"] == "identity"


# --- Timeouts ---------------------------------------------------------------


async def test_a_slow_response_produces_a_structured_timeout() -> None:
    """A timeout is a typed refusal, never a raw exception reaching the user."""
    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(5)
            return httpx.Response(200, content=b"{}")

    client = _client(
        Slow(), timeouts=TimeoutPolicy(
            connect_seconds=0.1, read_seconds=0.1, total_seconds=0.3
        )
    )

    with pytest.raises(ProviderTimeout):
        await client.get(f"https://{ALLOWED_HOST}/slow")


async def test_a_transport_failure_becomes_a_typed_error() -> None:
    """The exception message can name an internal host, so it is dropped."""
    transport = StubTransport(
        raise_error=httpx.ConnectError("failed connecting to 10.1.2.3:443")
    )
    client = _client(transport)

    with pytest.raises(ProviderUnavailable) as failure:
        await client.get(f"https://{ALLOWED_HOST}/x")

    assert "10.1.2.3" not in str(failure.value)
    assert "10.1.2.3" not in failure.value.detail


# --- Headers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    ["cookie", "authorization", "host", "x-forwarded-for", "referer",
     "x-api-key", "proxy-authorization"],
)
async def test_a_caller_cannot_set_an_arbitrary_header(header) -> None:
    """An allow-list, so a header nobody considered is refused by absence."""
    transport = StubTransport()
    client = _client(transport)

    with pytest.raises(NetworkPolicyViolation):
        await client.get(f"https://{ALLOWED_HOST}/x", headers={header: "value"})

    assert transport.connections == []


async def test_the_allow_listed_headers_are_the_harmless_ones() -> None:
    assert ALLOWED_REQUEST_HEADERS == {"accept", "accept-encoding", "user-agent"}


async def test_the_client_sends_a_constant_user_agent() -> None:
    """Never a version string that fingerprints the deployment."""
    transport = StubTransport()
    client = _client(transport)

    await client.get(f"https://{ALLOWED_HOST}/x")

    assert transport.request_headers[0]["user-agent"] == USER_AGENT


async def test_an_auth_header_is_applied_but_not_via_the_allow_list() -> None:
    """Credentials do not travel the same route as ordinary headers."""
    transport = StubTransport()
    client = _client(transport)

    await client.get(
        f"https://{ALLOWED_HOST}/x", auth_header=("X-Token", "SEARCH_SECRET_123")
    )

    assert transport.request_headers[0]["x-token"] == "SEARCH_SECRET_123"


async def test_no_credential_appears_in_the_request_url() -> None:
    """A query string is logged by proxies and kept in provider access logs."""
    transport = StubTransport()
    client = _client(transport)

    await client.get(
        f"https://{ALLOWED_HOST}/x",
        params={"q": "weather"},
        auth_header=("X-Token", "SEARCH_SECRET_123"),
    )

    assert "SEARCH_SECRET_123" not in transport.connections[0]


# --- No write methods -------------------------------------------------------


@pytest.mark.parametrize(
    "method", ["post", "put", "patch", "delete", "head", "options", "request", "send"]
)
def test_the_client_offers_no_write_method(method) -> None:
    """Research is read-only, and a client that could POST could be talked into it."""
    assert not hasattr(SecureHttpClient, method), method
