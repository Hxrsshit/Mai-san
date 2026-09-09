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

Method capability is per-policy, not per-class
----------------------------------------------

Stage 4F-B offered `get` and nothing else: web research is read-only, and a
client that could submit a form is a client that could be talked into
submitting one. Stage 4F-C brought the LLM provider through this same
boundary, and the provider must POST.

Adding a general `post()` would have handed the *research* integration a
write-capable client, which is a real weakening. So the permitted methods
moved onto the `NetworkPolicy` instead: research declares `{"GET"}`, the
provider declares `{"POST"}`, and each is refused the other's. One boundary,
and every caller narrower than it.

`PUT`, `PATCH` and `DELETE` are offered by no method here at all -- nothing in
Mai needs them, and an unused write verb is a capability waiting to be found.

Callers cannot supply arbitrary headers -- an allow-list of header names is
enforced, so a caller cannot smuggle a `Cookie`, an `Authorization` it built
itself, or a `Host` override. `Content-Type` is set by this client when it is
given a body, rather than accepted from a caller.

A policy may widen that list by naming specific headers in
`extra_request_headers`, which is how a provider sends a required protocol
header such as `anthropic-version` without every caller gaining it.

Bounded everywhere: connection, read and total time; response bytes read;
decompressed bytes; and redirect hops. An external provider cannot make Mai
wait forever or allocate without limit.
"""

import asyncio
import time
from typing import Any, Dict, Mapping, Optional, Tuple

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


class ResponseHeaders(dict):
    """A header mapping that keeps HTTP's case-insensitivity.

    Written because losing it caused a real regression: `dict(response.headers)`
    looks harmless, but a plain dict made `headers.get("Retry-After")` miss a
    header the server sent as `retry-after`. Rate-limit backoff silently
    stopped honouring the server's own guidance, and nothing failed loudly.

    Keys are stored lowercased; lookups lowercase too.
    """

    def __init__(self, items=None) -> None:
        super().__init__(
            {str(key).lower(): value for key, value in dict(items or {}).items()}
        )

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key) -> bool:
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class HttpResponse:
    """A bounded, already-read response. Carries no client and no socket.

    Deliberately not an `httpx.Response`. That object can still stream, still
    holds a connection, and exposes `request` -- including the headers the
    request carried, one of which is the credential. Handing one to a caller
    would put the API key on an object that error handlers and log lines
    routinely reach for.

    What crosses instead: a status, decoded headers, the already-bounded body,
    and the final URL. `json()` and `text` are provided because callers need
    them and would otherwise re-implement decoding, badly.
    """

    __slots__ = ("status_code", "content", "headers", "url", "elapsed_ms")

    def __init__(
        self, status_code: int, content: bytes, headers: Mapping[str, str],
        url: str, elapsed_ms: int,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = ResponseHeaders(headers)
        #: The final URL, after any redirects -- each of which was itself
        #: checked. Kept so a caller can see where the answer came from.
        self.url = url
        self.elapsed_ms = elapsed_ms

    @property
    def text(self) -> str:
        """The body as text. Never raises on malformed bytes."""
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        """Parse the body as JSON, or raise `ValueError`.

        `ValueError` rather than a bespoke type: callers already handle it
        (`json.JSONDecodeError` is a subclass), and a transport that invented
        its own decoding error would make every caller learn one more.
        """
        import json as _json

        return _json.loads(self.text)


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

        `auth_header` is a `(name, value)` pair the *caller* obtained from the
        credential resolver. It is applied here and never logged, never
        echoed, and never placed in any object that leaves this method.
        """
        return await self._request(
            "GET", url, params=params, headers=headers, auth_header=auth_header
        )

    async def post_json(
        self,
        url: str,
        json_body: Mapping[str, Any],
        headers: Optional[Mapping[str, str]] = None,
        auth_header: Optional[Tuple[str, str]] = None,
    ) -> HttpResponse:
        """POST a JSON body. Permitted only where the policy names POST.

        The body is serialised here from a mapping the *application* built.
        There is no raw-bytes variant and no caller-chosen content type: a
        client that accepted arbitrary bytes with an arbitrary type would be
        a general-purpose HTTP proxy wearing a narrower signature.
        """
        return await self._request(
            "POST", url, json_body=json_body, headers=headers,
            auth_header=auth_header,
        )

    async def post_form(
        self,
        url: str,
        form: Mapping[str, str],
        headers: Optional[Mapping[str, str]] = None,
        auth_header: Optional[Tuple[str, str]] = None,
    ) -> HttpResponse:
        """POST form-encoded data. Permitted only where the policy names POST.

        A third body shape rather than a general one. OAuth token endpoints
        take `application/x-www-form-urlencoded` and reject JSON, so the
        choice is between this method and letting a caller supply raw bytes
        with a content type of its choosing -- and the second is a
        general-purpose HTTP client wearing a narrower signature.

        The client encodes the mapping and sets the content type itself, for
        the same reason it does with JSON: a caller-chosen encoding and a
        caller-chosen type can disagree, and this way they cannot.
        """
        return await self._request(
            "POST", url, form_body=form, headers=headers, auth_header=auth_header
        )

    async def _request(
        self, method, url, params=None, json_body=None, headers=None,
        auth_header=None, form_body=None,
    ) -> HttpResponse:
        """Every request passes here, and every gate lives here.

        The method check comes first: a policy that does not name this method
        refuses before a URL is even parsed, so a caller cannot learn anything
        about a destination it was never allowed to address.
        """
        if not self._policy.permits(method):
            raise NetworkPolicyViolation(detail="method")

        total = self._policy.timeouts.total_seconds
        try:
            return await asyncio.wait_for(
                self._send_bounded(method, url, params, json_body, headers,
                                   auth_header, form_body),
                timeout=total,
            )
        except asyncio.TimeoutError as exc:
            # The outer bound. Covers redirect hops and slow bodies together,
            # which the per-phase httpx timeouts individually do not.
            raise ProviderTimeout(detail="total") from exc

    async def _send_bounded(
        self, method, url, params, json_body, headers, auth_header,
        form_body=None,
    ) -> HttpResponse:
        request_headers = self._safe_headers(
            headers, auth_header, has_body=json_body is not None,
            has_form=form_body is not None,
        )
        started = time.monotonic()
        current = url
        hops = 0

        while True:
            # The enforcement point. Runs before every connection, on the
            # initial URL and on each redirect target -- a safe first URL
            # does not make an unsafe redirect safe.
            self._policy.check(current, resolve=self._resolve)

            response = await self._send(
                method, current, params, json_body, request_headers, form_body
            )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                hops += 1
                if hops > self._policy.max_redirects:
                    raise NetworkPolicyViolation(detail="redirects")
                if not self._policy.follow_redirects:
                    raise NetworkPolicyViolation(detail="redirect")
                current = self._absolute(current, location)
                # Query parameters belong to the original request only; a
                # redirect target carries its own. The body is dropped for the
                # same reason and a stronger one: re-POSTing to a destination
                # the origin chose is how one approved write becomes two.
                params = None
                json_body = None
                form_body = None
                method = "GET" if not self._policy.permits(method) else method
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

    async def _send(
        self, method, url, params, json_body, headers, form_body=None
    ) -> httpx.Response:
        client = self._ensure_client()
        request = client.build_request(
            method, url, params=params, json=json_body, data=form_body,
            headers=headers,
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

    def _safe_headers(
        self, headers, auth_header, has_body=False, has_form=False
    ) -> Dict[str, str]:
        """Build the request headers. Refuses anything not allow-listed."""
        built = {
            "user-agent": USER_AGENT,
            "accept": "application/json",
            # Identity only: a client that advertised gzip would have to
            # inflate before it could bound the size, and the bound is what
            # stops a decompression bomb.
            "accept-encoding": "identity",
        }

        if has_body:
            # Set here, not accepted from a caller. A caller-chosen content
            # type is half of an arbitrary-request primitive; this client
            # sends JSON because it serialises JSON, and the two cannot
            # disagree.
            built["content-type"] = "application/json"
        elif has_form:
            built["content-type"] = "application/x-www-form-urlencoded"

        permitted = ALLOWED_REQUEST_HEADERS | {
            str(name).lower() for name in self._policy.extra_request_headers
        }

        for name, value in (headers or {}).items():
            lowered = str(name).lower()
            if lowered not in permitted:
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
    "ResponseHeaders",
    "SecureHttpClient",
]
