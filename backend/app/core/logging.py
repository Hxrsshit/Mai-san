"""Structured logging setup.

Emits one JSON object per line in production-style deployments, or a compact
human-readable line when LOG_FORMAT=console. A per-request correlation id is
carried in a ContextVar so every log line inside a request can be tied together.
"""

import json
import logging
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

        return json.dumps(payload, default=str, ensure_ascii=False)


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

        return line


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

    # SQLAlchemy is noisy at INFO when echo is on; keep it at WARNING by default.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_request_id(request_id: str) -> None:
    request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    return request_id_var.get() or None
