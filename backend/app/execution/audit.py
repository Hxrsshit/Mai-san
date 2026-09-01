"""The append-only execution journal.

One function writes events, and nothing anywhere updates or deletes one. The
journal answers "what happened to this action?" in a way the execution row
cannot: the row shows where something ended up, the journal shows how it got
there, including the attempts that were refused.

Metadata is redacted before it is persisted. Stage 3D established that
exception text routinely carries a connection string, and an audit table is
exactly the kind of long-lived store where such a value would survive.
"""

import uuid
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, redact
from app.execution.models import ExecutionEvent, ExecutionEventType

logger = get_logger(__name__)

#: Metadata keys that are never written, whatever a caller passes.
#:
#: Belt and braces: no call site supplies these, and redaction would mask the
#: values anyway. Dropping the keys outright means a future call site cannot
#: introduce one by accident.
_FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "password", "secret", "token", "credential",
    "database_url", "dsn", "authorization", "content", "traceback",
    "stack_trace", "exception",
})

#: Longest string any single metadata value may be.
MAX_METADATA_VALUE_CHARS = 200
MAX_METADATA_KEYS = 20


def sanitise(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce metadata to something safe to keep forever.

    Forbidden keys are dropped, values are bounded and stringified where they
    are not primitives, and everything passes through the Stage 3D redactor.
    """
    if not metadata:
        return {}

    clean: Dict[str, Any] = {}
    for key, value in list(metadata.items())[:MAX_METADATA_KEYS]:
        name = str(key).lower()
        if name in _FORBIDDEN_KEYS or any(
            marker in name for marker in ("secret", "password", "token", "key")
        ):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            clean[str(key)] = value
        elif value is None:
            clean[str(key)] = None
        else:
            clean[str(key)] = redact(str(value))[:MAX_METADATA_VALUE_CHARS]
    return clean


async def record(
    session: AsyncSession,
    execution_id: uuid.UUID,
    event_type: ExecutionEventType,
    actor: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
) -> ExecutionEvent:
    """Append one event. The only way a journal row is ever created.

    The sequence number is derived from the current maximum for this
    execution, and the pair is uniquely indexed -- so two concurrent writers
    cannot silently produce an ambiguous order. One of them loses the insert
    and retries at the next position, which is the correct outcome: an audit
    trail with two "step 3"s would be worse than one that briefly conflicts.
    """
    highest = (
        await session.execute(
            select(func.coalesce(func.max(ExecutionEvent.sequence), 0)).where(
                ExecutionEvent.execution_id == execution_id
            )
        )
    ).scalar_one()

    event = ExecutionEvent(
        execution_id=execution_id,
        event_type=event_type,
        actor=actor,
        event_metadata=sanitise(metadata),
        sequence=int(highest) + 1,
    )
    session.add(event)
    await session.flush()

    logger.info(
        "Execution event recorded",
        extra={
            "execution_id": str(execution_id),
            "event": event_type.value,
            "actor": actor,
            "sequence": event.sequence,
        },
    )
    return event


__all__ = ["MAX_METADATA_VALUE_CHARS", "record", "sanitise"]
