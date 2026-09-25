"""The append-only task journal.

One function writes events and nothing anywhere updates or deletes one --
the same contract as `app.execution.audit`, which this deliberately mirrors
rather than reinvents.

Redaction is imported from that module rather than copied. A second
forbidden-key list would be a second thing to keep correct, and the first
time the two disagreed the weaker one would be the one an attacker found.
"""

import uuid
from typing import Any, Dict, FrozenSet, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

# One sanitiser for both journals. See the module docstring.
from app.execution.audit import sanitise
from app.tasks.models import TaskEvent, TaskEventType

logger = get_logger(__name__)

#: The only actors that may appear. The same three `execution_events` uses.
ACTORS: FrozenSet[str] = frozenset({"user", "system", "policy"})

#: The events Stage 6A is permitted to write.
#:
#: Everything else in `TaskEventType` describes something a runner does, and
#: writing one now would be recording an action that did not happen -- the
#: exact fault Stage 5D.1 exists to prevent, moved into the audit trail where
#: it would be harder to notice. A test pins this set against the ones the
#: service can actually emit.
STAGE_6A_EVENTS: FrozenSet[TaskEventType] = frozenset({
    TaskEventType.TASK_CREATED,
    TaskEventType.PLAN_ATTACHED,
    TaskEventType.ASSUMPTION_RECORDED,
    TaskEventType.STATE_CHANGED,
    TaskEventType.TASK_BLOCKED,
    TaskEventType.TASK_CANCELLED,
    TaskEventType.TASK_FAILED,
})


class TaskEventRefused(RuntimeError):
    """An event this stage may not write, or an actor that does not exist."""


async def record(
    session: AsyncSession,
    task_id: uuid.UUID,
    event_type: TaskEventType,
    actor: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
) -> TaskEvent:
    """Append one event. The only way a journal row is ever created.

    The sequence number is derived from the current maximum for this task and
    the pair is uniquely indexed, so two concurrent writers cannot silently
    produce an ambiguous order: one loses the insert, which is the correct
    outcome. A journal with two "step 3"s would be worse than one that
    briefly conflicts.
    """
    if event_type not in STAGE_6A_EVENTS:
        # Refused rather than dropped. A caller trying to record a step
        # completion in a stage with no runner has a bug, and swallowing it
        # would leave a task whose journal quietly disagrees with its state.
        raise TaskEventRefused(f"{event_type.value} cannot be recorded in this stage")
    if actor not in ACTORS:
        raise TaskEventRefused(f"unknown actor: {actor!r}")

    highest = (
        await session.execute(
            select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
                TaskEvent.task_id == task_id
            )
        )
    ).scalar_one()

    event = TaskEvent(
        task_id=task_id,
        event_type=event_type,
        actor=actor,
        event_metadata=sanitise(metadata),
        sequence=int(highest) + 1,
    )
    session.add(event)
    await session.flush()

    logger.info(
        "Task event recorded",
        # Ids, a type and an actor. Never the objective, never a plan, never
        # a step title -- all three are the user's own words.
        extra={
            "task_id": str(task_id),
            "event_type": event_type.value,
            "actor": actor,
            "sequence": event.sequence,
        },
    )
    return event


__all__ = ["ACTORS", "STAGE_6A_EVENTS", "TaskEventRefused", "record"]
