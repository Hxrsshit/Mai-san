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

#: The events the service is permitted to write.
#:
#: Stage 6A allowed seven and refused the rest, because writing one for
#: something that did not happen is the Stage 5D.1 fault moved into the audit
#: trail. Stage 6C adds the four that now describe things that *do* happen:
#: approval being asked for and granted, and a step starting or finishing.
#:
#: Still refused: `task_completed`, `replanned`, `observation_recorded` and
#: `budget_exceeded`. Each describes a runner's work, and there is no runner.
EMITTABLE_EVENTS: FrozenSet[TaskEventType] = frozenset({
    TaskEventType.TASK_CREATED,
    TaskEventType.PLAN_ATTACHED,
    TaskEventType.ASSUMPTION_RECORDED,
    TaskEventType.STATE_CHANGED,
    TaskEventType.TASK_BLOCKED,
    TaskEventType.TASK_CANCELLED,
    TaskEventType.TASK_FAILED,
    # Stage 6C.
    TaskEventType.APPROVAL_REQUESTED,
    TaskEventType.APPROVAL_GRANTED,
    TaskEventType.STEP_STARTED,
    TaskEventType.STEP_COMPLETED,
    TaskEventType.STEP_FAILED,
    # Stage 6D. A runner exists, so what it did is now a fact.
    TaskEventType.EXECUTION_CREATED,
    TaskEventType.RUNNER_BLOCKED,
    TaskEventType.RUNNER_REFUSED,
    TaskEventType.TASK_COMPLETED,
    TaskEventType.BUDGET_EXCEEDED,
})

#: Deliberately still unwritable: `replanned` and `observation_recorded`.
#: Both describe observe-and-replan, which Stage 6D does not do.
#:
#: Deliberately *absent from the vocabulary entirely*: `runner_started`,
#: `step_selected` and `authorization_checked`. Each would be written on
#: every invocation and carries no fact the others do not -- an
#: `execution_created` event proves the authorization check passed, and a
#: `runner_blocked` proves a step was selected and refused. A journal that
#: records deliberation rather than outcomes is one nobody reads.

#: Kept so Stage 6A's own tests keep naming what they pinned.
STAGE_6A_EVENTS = EMITTABLE_EVENTS


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
    if event_type not in EMITTABLE_EVENTS:
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


__all__ = [
    "ACTORS",
    "EMITTABLE_EVENTS",
    "STAGE_6A_EVENTS",
    "TaskEventRefused",
    "record",
]
