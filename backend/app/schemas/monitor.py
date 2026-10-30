"""What a client is told about a Stage 6N monitoring turn.

The outcome and the task it concerned, nothing more. The sender, the subject
words, the baseline and the deadline are in the application-written reply the
person already read; the task row holds the rest and is readable through the
authenticated tasks API. No grant id, no capability detail, no Gmail data.
"""

import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.tasks.requests import MonitorOutcome, MonitorResult


class MonitorRead(BaseModel):
    """One turn's monitoring state, as the wire sees it."""

    model_config = ConfigDict(frozen=True)

    outcome: MonitorOutcome
    task_id: Optional[uuid.UUID] = None

    @classmethod
    def from_result(cls, result: MonitorResult) -> Optional["MonitorRead"]:
        """None when the turn had nothing to do with monitoring."""
        if result is None or result.outcome is MonitorOutcome.NOT_MONITOR:
            return None
        return cls(outcome=result.outcome, task_id=result.task_id)


__all__ = ["MonitorRead"]
