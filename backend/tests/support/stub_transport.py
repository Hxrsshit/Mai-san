"""An httpx transport that records requests and never touches a network.

The point of driving `SecureHttpClient` against this rather than mocking the
client is that **the policy still runs**. An SSRF test is only meaningful if
it exercises the code path a real request would take, right up to the moment a
socket would be opened -- so the check, the redirect walk and the size bound
all execute, and only the connection itself is replaced.

`connections` records every destination that got as far as the transport. A
refused URL leaves it empty, which is the assertion most of these tests make.
"""

import json
from typing import Any, Dict, List, Optional

import httpx


class StubTransport(httpx.AsyncBaseTransport):
    """Answers requests from a script. Records what it was asked for."""

    def __init__(
        self,
        status_code: int = 200,
        payload: Optional[Dict[str, Any]] = None,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        responses: Optional[List[Dict[str, Any]]] = None,
        raise_error: Optional[Exception] = None,
    ) -> None:
        #: Every URL that reached the transport. Empty means nothing connected.
        self.connections: List[str] = []
        #: Every request's headers, so credential handling can be asserted.
        self.request_headers: List[Dict[str, str]] = []
        #: Every request's body, so redirect body-dropping can be asserted.
        self.bodies: List[bytes] = []
        self._status = status_code
        self._payload = payload
        self._body = body
        self._headers = headers or {}
        #: A scripted sequence, for redirect chains. Each entry may set
        #: `status_code`, `headers` and `payload`.
        self._responses = list(responses or [])
        self._raise = raise_error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.connections.append(str(request.url))
        self.request_headers.append(dict(request.headers))
        self.bodies.append(request.content)

        if self._raise is not None:
            raise self._raise

        if self._responses:
            script = self._responses.pop(0)
            status = script.get("status_code", 200)
            headers = script.get("headers", {})
            payload = script.get("payload")
            body = script.get("body")
        else:
            status, headers, payload, body = (
                self._status, self._headers, self._payload, self._body
            )

        if body is None:
            body = json.dumps(payload or {}).encode("utf-8")

        return httpx.Response(status, headers=headers, content=body)


def brave_payload(count: int = 2) -> Dict[str, Any]:
    """A response shaped like the search provider's."""
    return {
        "web": {
            "results": [
                {
                    "title": f"Result {index}",
                    "url": f"https://source-{index}.example.org/page",
                    "description": f"Snippet for result {index}.",
                }
                for index in range(1, count + 1)
            ]
        }
    }
