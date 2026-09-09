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
        #: The verb of each request, so a test can assert what was used.
        self.methods: List[str] = []
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
        self.methods.append(request.method)

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


def tavily_payload(count: int = 2) -> Dict[str, Any]:
    """A response shaped like Tavily's, from its published API reference.

    Deliberately carries the fields Mai does *not* read -- `score`,
    `raw_content`, `answer`, `request_id` -- so a test proves they are
    ignored rather than merely absent.
    """
    return {
        "query": "test",
        "answer": "A synthesised answer Mai does not use.",
        "request_id": "req-abc",
        "response_time": 1.2,
        "results": [
            {
                "title": f"Result {index}",
                "url": f"https://source-{index}.example.org/page",
                "content": f"Snippet for result {index}.",
                "score": 0.9,
                "raw_content": "<html>ignored</html>",
            }
            for index in range(1, count + 1)
        ],
    }


def calendar_payload(count: int = 2) -> Dict[str, Any]:
    """A Google Calendar response, carrying everything Mai must not keep.

    Attendees, conference links, attachments and private properties are all
    present deliberately, so a test proves they are dropped rather than
    merely absent from a thin fixture.
    """
    return {
        "kind": "calendar#events",
        "items": [
            {
                "id": f"evt-{index}",
                "summary": f"Meeting {index}",
                "location": f"Room {index}",
                "description": "Passcode 4821.",
                "start": {"dateTime": f"2026-09-11T0{index}:00:00Z"},
                "end": {"dateTime": f"2026-09-11T0{index}:30:00Z"},
                "organizer": {
                    "email": "priya@corp.example", "displayName": "Priya"
                },
                "attendees": [{"email": "alex@corp.example"}],
                "hangoutLink": "https://meet.google.com/abc-defg-hij",
            }
            for index in range(1, count + 1)
        ],
    }


def sent_query(transport, index: int = 0) -> str:
    """The search query that actually reached the wire, whichever verb was used.

    Providers disagree about where a query travels: Brave puts it in the query
    string of a GET, Tavily puts it in the JSON body of a POST. A test that
    reads one of those is a test that silently stops checking anything when
    the configured provider changes -- it would pass by finding nothing.

    So this reads whichever the request actually used, and raises if neither
    is present rather than returning "" and letting an assertion compare two
    empty strings.
    """
    import json
    from urllib.parse import parse_qs, urlparse

    url = transport.connections[index]
    params = parse_qs(urlparse(url).query)
    if "q" in params:
        return params["q"][0]

    body = transport.bodies[index]
    if body:
        payload = json.loads(body.decode("utf-8"))
        if "query" in payload:
            return str(payload["query"])

    raise AssertionError(
        f"no query found in request {index}: url={url!r} body={body!r}"
    )
