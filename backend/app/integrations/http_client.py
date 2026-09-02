"""The only approved path for outbound HTTP in Mai.

Stage 4F-A wrote `NetworkPolicy` and enforced it nowhere -- there was no
client to enforce it against, and the acceptance report said so plainly. This
module is the client, and it enforces the policy **itself** rather than
trusting callers to remember:

    policy.check(url)      # a caller can forget this
    client.get(url)        # this cannot be reached without the check

Every request runs `NetworkPolicy.check` before a connection is attempted, on
the initial URL and again on every redirect target. There is no parameter that
disables it and no method that bypasses it.

What this client deliberately does not offer
--------------------------------------------

No `POST`, `PUT`, `PATCH` or `DELETE`. Web research is read-only, and a client
that could submit a form is a client that could be talked into submitting one.
Callers cannot supply arbitrary headers either -- an allow-list of header
names is enforced, so a caller cannot smuggle a `Cookie`, an `Authorization`
it built itself, or a `Host` override.

Bounded everywhere: connection, read and total time; response bytes read;
decompressed bytes; and redirect hops. An external provider cannot make Mai
wait forever or allocate without limit.
"""

import asyncio
import time
from typing import Dict, Mapping, Optional, Tuple

import httpx

from app.core.logging import get_logger
from app.integrations.errors import (
    NetworkPolicyViolation,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseTooLarge,
)
from app.integrations.policy import NetworkPolicy

logger = get_logger(__name__)

#: Header names a caller may set. Everything else is refused.
#:
#: An allow-list, not a deny-list. A caller that could set arbitrary headers
#: could send a `Cookie`, override `Host` to defeat the destination check, or
#: attach an `Authorization` it built from somewhere other than the credential
#: resolver. Authentication headers are added by the client from a credential
#: the *integration* supplied, never from this mapping.
ALLOWED_REQUEST_HEADERS = frozenset({"accept", "accept-encoding", "user-agent"})

#: How Mai identifies itself. A constant: never a version string that leaks
#: the deployment, and never anything derived from user input.
USER_AGENT = "Mai/1.0 (+research)"

#: Read in chunks so the size bound is enforced while the body streams, not
#: after it has already been held in memory.
_CHUNK_BYTES = 64 * 1024


class HttpResponse:
    """A bounded, already-read response. Carries no client and no socket."""

    __slots__ = ("status_code", "content", "headers", "url", "elapsed_ms")

    def __init__(
        self, status_code: int, content: bytes, headers: Mapping[str, str],
        url: str, elapsed_ms: int,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = dict(headers)
        #: The final URL, after any redirects -- each of which was itself
        #: checked. Kept so a caller can see where the answer came from.
        self.url = url
        self.elapsed_ms = elapsed_ms


class SecureHttpClient:
    """Policy-enforcing HTTP. GET only, bounded, no arbitrary headers.

    One client per integration, holding that integration's own policy. The
    policy is not a parameter of `get` -- it is fixed at construction, so a
    caller cannot pass a laxer one for a single request.
    """

    def __init__(
        self,
        policy: NetworkPolicy,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        resolve=None,
    ) -> None:
        self._policy = policy
        #: Injectable so tests exercise the client against a stub instead of
        #: the network. The policy runs identically either way -- which is
        #: what makes an SSRF test at this boundary meaningful.
        self._transport = transport
        self._resolve = resolve
        self._client: Optional[httpx.AsyncClient] = None

    async def get(
        self,
        url: str,
        params: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        auth_header: Optional[Tuple[str, str]] = None,
    ) -> HttpResponse:
        """Fetch one URL. Raises rather than returning a failure sentinel.

        `auth_header` is a `(name, value)` pair the *integration* obtained
        from the credential resolver. It is applied here and never logged,
        never echoed, and never placed in any object that leaves this method.
        """
        total = self._policy.timeouts.total_seconds
        try:
            return await asyncio.wait_for(
                self._get(url, params, headers, auth_header), timeout=total
            )
        except asyncio.TimeoutError as exc:
            # The outer bound. Covers redirect hops and slow bodies together,
            # which the per-phase httpx timeouts individually do not.
            raise ProviderTimeout(detail="total") from exc

    async def _get(self, url, params, headers, auth_header) -> HttpResponse:
        request_headers = self._safe_headers(headers, auth_header)
        started = time.monotonic()
        current = url
        hops = 0

        while True:
            # The enforcement point. Runs before every connection, on the
            # initial URL and on each redirect target -- a safe first URL
            # does not make an unsafe redirect safe.
            self._policy.check(current, resolve=self._resolve)

            response = await self._send(current, params, request_headers)

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                hops += 1
                if hops > self._policy.max_redirects:
                    raise NetworkPolicyViolation(detail="redirects")
                if not self._policy.follow_redirects:
                    raise NetworkPolicyViolation(detail="redirect")
                current = self._absolute(current, location)
                # Query parameters belong to the original request only; a
                # redirect target carries its own.
                params = None
                await response.aclose()
                continue

            content = await self._read_bounded(response)
            final_url = str(response.url)
            await response.aclose()

            return HttpResponse(
                status_code=response.status_code,
                content=content,
                headers=response.headers,
                url=final_url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    async def _send(self, url, params, headers) -> httpx.Response:
        client = self._ensure_client()
        request = client.build_request(
            "GET", url, params=params, headers=headers
        )
        try:
            return await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(detail="request") from exc
        except httpx.TransportError as exc:
            # A connection-level failure. The message can name an internal
            # host, so only the type crosses.
            logger.warning(
                "Outbound request failed at the transport layer",
                extra={"error_type": type(exc).__name__},
            )
            raise ProviderUnavailable(detail="transport") from exc

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        """Read the body, refusing once the cap is passed.

        Refused rather than truncated. A truncated body is a partial answer
        that looks like a whole one, and a JSON parser would either fail
        confusingly or -- worse -- succeed on a prefix.

        The cap applies to *decompressed* bytes, because that is what
        consumes memory: `httpx` inflates as it iterates, so a small gzipped
        body that expands enormously is caught here rather than after.
        """
        limit = self._policy.max_response_bytes
        chunks = []
        total = 0

        try:
            async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise ResponseTooLarge(detail="body")
                chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(detail="read") from exc

        return b"".join(chunks)

    def _safe_headers(self, headers, auth_header) -> Dict[str, str]:
        """Build the request headers. Refuses anything not allow-listed."""
        built = {
            "user-agent": USER_AGENT,
            "accept": "application/json",
            # Identity only: a client that advertised gzip would have to
            # inflate before it could bound the size, and the bound is what
            # stops a decompression bomb.
            "accept-encoding": "identity",
        }

        for name, value in (headers or {}).items():
            lowered = str(name).lower()
            if lowered not in ALLOWED_REQUEST_HEADERS:
                # Refused, not dropped. A caller trying to set a header it
                # may not set is a mistake worth surfacing.
                raise NetworkPolicyViolation(detail="header")
            built[lowered] = str(value)

        if auth_header is not None:
            name, value = auth_header
            # Applied last so a caller-supplied header cannot overwrite it,
            # and never through the allow-list above -- credentials do not
            # travel by the same route as ordinary headers.
            built[str(name)] = value

        return built

    def _absolute(self, current: str, location: str) -> str:
        """Resolve a redirect target against the current URL.

        A relative `Location` is resolved rather than refused, because it is
        ordinary and cannot change host. The result goes back through
        `policy.check` at the top of the loop regardless.
        """
        from urllib.parse import urljoin

        if not location.strip():
            raise NetworkPolicyViolation(detail="redirect")
        return urljoin(current, location.strip())

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeouts = self._policy.timeouts
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeouts.read_seconds,
                    connect=timeouts.connect_seconds,
                    read=timeouts.read_seconds,
                    write=timeouts.connect_seconds,
                    pool=timeouts.connect_seconds,
                ),
                # Never httpx's own redirect handling: it would follow a hop
                # without the policy seeing it. Redirects are walked above,
                # one at a time, each checked.
                follow_redirects=False,
                transport=self._transport,
                # No cookie jar. A cookie is state a provider sets and Mai
                # then sends back somewhere; research needs none.
                cookies=None,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = [
    "ALLOWED_REQUEST_HEADERS",
    "USER_AGENT",
    "HttpResponse",
    "SecureHttpClient",
]
