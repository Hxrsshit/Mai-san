"""Approval: what it binds to, and when it stops applying.

An approval is not a flag. It is a statement that *this exact payload* may run,
made at a particular moment, and it stops applying when any of three things is
true: the payload changed, the clock ran out, or it was withdrawn.

The binding is a fingerprint over `(tool, arguments)`. Approving
`create_text_file("a.txt")` therefore does not approve
`create_text_file("b.txt")`, `delete_file("a.txt")`, or the same call with
`overwrite=True` -- each produces a different fingerprint, and a fingerprint
that does not match is not an approval.

Nothing here calls a model. An LLM writing "I approve this action" produces
text; approval happens when a human calls the approve endpoint, and only then.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from app.execution.errors import (
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalRequired,
)
from app.execution.models import Execution
from app.execution.schemas import payload_fingerprint
from app.execution.states import ExecutionState


def expiry_from(granted_at: datetime, ttl_seconds: int) -> datetime:
    """When an approval granted now stops applying.

    Always finite. A zero or negative TTL yields an already-expired approval
    rather than an eternal one: misconfiguration should fail closed.
    """
    return granted_at + timedelta(seconds=max(0, ttl_seconds))


def fingerprint_for(execution: Execution) -> str:
    """The fingerprint of what this execution would run *right now*."""
    return payload_fingerprint(execution.tool_name, dict(execution.arguments or {}))


def is_expired(execution: Execution, now: Optional[datetime] = None) -> bool:
    """True when the approval window has closed.

    An approved execution with no expiry is treated as expired. That state is
    unreachable -- a check constraint forbids it -- but if it ever occurred,
    "no answer" must read as "no".
    """
    if execution.approval_expires_at is None:
        return True
    return _now(now) >= _aware(execution.approval_expires_at)


def validate(execution: Execution, now: Optional[datetime] = None) -> None:
    """Raise unless this execution may proceed to run. Never returns a value.

    A function that raises rather than one that returns a boolean, so a caller
    cannot forget to check the result -- the failure mode of a boolean gate is
    silent, and here it would be an unapproved side effect.
    """
    if execution.state is not ExecutionState.APPROVED:
        # Includes PROPOSED, REVOKED, EXPIRED and every terminal state.
        raise ApprovalRequired(detail=execution.state.value)

    if execution.approved_fingerprint is None:
        raise ApprovalInvalid(detail="no fingerprint recorded")

    if is_expired(execution, now):
        raise ApprovalExpired()

    current = fingerprint_for(execution)
    if current != execution.approved_fingerprint:
        # The payload changed after approval. The approval described a
        # different action, so it is not an approval for this one.
        raise ApprovalInvalid(detail="payload changed since approval")


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    SQLite returns naive datetimes; PostgreSQL returns aware ones. Comparing
    the two raises, and an exception during an expiry check must not be the
    thing that decides whether something runs.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = ["expiry_from", "fingerprint_for", "is_expired", "validate"]
