"""Request-scoped middleware."""

import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, set_request_id

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: Correlation ids are echoed into every log line for a request and into every
#: error body. A client-supplied value is therefore untrusted input on a path
#: that reaches both, so it is sanitised rather than trusted.
MAX_REQUEST_ID_LENGTH = 64

# Conservative allowlist. Enough for a UUID, a hex id or a trace id from a
# proxy; nothing that could terminate a header, forge a log line, or carry a
# control character into a terminal reading the console formatter.
_REQUEST_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def _sanitise_request_id(value: str) -> str:
    """Reduce a client-supplied correlation id to something safe to echo.

    Returns "" when nothing usable survives, which makes the caller generate a
    fresh id instead. Length is capped because the value is repeated in every
    log line for the request: an unbounded header would turn one request into
    arbitrary log volume.
    """
    if not value:
        return ""
    return _REQUEST_ID_ALLOWED.sub("", value)[:MAX_REQUEST_ID_LENGTH]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, logs completion, and echoes the id back."""

    async def dispatch(self, request: Request, call_next) -> Response:
        supplied = _sanitise_request_id(request.headers.get(REQUEST_ID_HEADER, ""))
        request_id = supplied or uuid.uuid4().hex
        set_request_id(request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000

        # /health is polled constantly; keep it out of the info stream.
        log = logger.debug if request.url.path == "/health" else logger.info
        log(
            "Request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
