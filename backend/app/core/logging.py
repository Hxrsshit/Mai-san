"""Structured logging setup.

Emits one JSON object per line in production-style deployments, or a compact
human-readable line when LOG_FORMAT=console. A per-request correlation id is
carried in a ContextVar so every log line inside a request can be tied together.

**Logging policy: metadata, never private content.**

Mai's database holds conversations, memories and an entity graph about one
person. A log file has different retention, different backup behaviour and a
wider audience than the database it describes, so log lines carry ids, counts,
durations and status codes -- never message text, memory content, conversation
titles or entity names.

Application code follows that policy by construction. Third-party libraries do
not, which is what `_DATA_CARRYING_LOGGERS` below exists to contain.
"""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Correlation id for the in-flight request; empty outside of a request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Attributes present on every LogRecord; anything else is treated as structured extra.
_RESERVED_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


#: Patterns redacted from every rendered log line.
#:
#: Applied in the formatters rather than at the call sites. 33 places copy
#: `str(exc)` into a structured field and 19 attach a traceback; a driver
#: exception routinely embeds the connection DSN, and a provider exception can
#: embed an Authorization header. Redacting at each site would work until
#: someone adds the thirty-fourth. The formatter is the one place every log
#: line must pass through.
_REDACTIONS = (
    # Credentials inside a URL: postgresql://user:password@host -> user:***@host
    (re.compile(r"(?<=://)([^\s:/@]+):([^\s:/@]+)(?=@)"), r"\1:***"),
    # Provider and platform key shapes.
    (re.compile(r"\bgsk_[A-Za-z0-9]{8,}"), "gsk_***"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9]{8,}"), "sk-or-v1-***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "sk-***"),
    (re.compile(r"\bghp_[A-Za-z0-9]{8,}"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "github_pat_***"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"), "AKIA***"),
    # Authorization headers, however they were stringified.
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)(\"?(?:api[_-]?key|authorization|password|secret|token)\"?\s*[:=]\s*\"?)([^\s,\"}}]{4,})"),
     r"\1***"),
)


def redact(text: str) -> str:
    """Mask credential shapes in a rendered log line.

    A best-effort net, not a guarantee: it recognises the shapes this system
    actually handles. It is the last line of defence behind the real control,
    which is that application code logs ids and counts rather than content.
    """
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _extra_fields(record: logging.LogRecord) -> Dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        payload.update(_extra_fields(record))

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return redact(json.dumps(payload, default=str, ensure_ascii=False))


class ConsoleFormatter(logging.Formatter):
    """Compact, readable formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%H:%M:%S"
        )
        request_id = request_id_var.get()
        prefix = f"[{request_id[:8]}] " if request_id else ""
        line = (
            f"{timestamp} {record.levelname:<8} {record.name:<28} "
            f"{prefix}{record.getMessage()}"
        )

        extras = _extra_fields(record)
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            line = f"{line} | {rendered}"

        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"

        return redact(line)


#: Loggers whose DEBUG output contains user data or transport internals.
#: Pinned to WARNING regardless of the configured application level.
_DATA_CARRYING_LOGGERS = (
    # Database drivers: log full statements including bound parameter values.
    "aiosqlite",
    "asyncpg",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "sqlalchemy.dialects",
    "sqlalchemy.orm",
    # HTTP clients: log request/response internals on the path that carries
    # the provider credential and the assembled prompt.
    "httpx",
    "httpcore",
)


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Install the root log handler. Safe to call more than once."""
    formatter: logging.Formatter = (
        ConsoleFormatter() if log_format == "console" else JsonFormatter()
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn ships its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # Third-party loggers that emit row data, and are therefore pinned above
    # the application's level.
    #
    # This is a privacy control, not noise reduction. At DEBUG the database
    # drivers log every statement *with its bound parameters* -- which is the
    # full text of every message, memory, conversation title and entity name.
    # `LOG_LEVEL=DEBUG` is an ordinary thing to set while troubleshooting, and
    # an operator has no reason to expect it to write their entire personal
    # knowledge base to stdout in plaintext.
    #
    # SQL statements themselves remain reachable for debugging through
    # `DB_ECHO`, which routes via `sqlalchemy.engine` -- but the raw driver
    # chatter carrying parameter values stays off.
    for name in _DATA_CARRYING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_request_id(request_id: str) -> None:
    request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    return request_id_var.get() or None
