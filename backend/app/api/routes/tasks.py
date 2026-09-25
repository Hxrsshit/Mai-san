"""Stage 6A: reading tasks, their steps, their journal and Mai's activity.

**Read-only, deliberately.** There is no create, no update, no transition and
no execute endpoint. A task is created from a user turn through
`TaskService.create_for_user`; exposing an HTTP constructor would be a second
way in, and the one thing this stage must guarantee is that a task has a
person behind it.

Nothing here interprets a task's objective. It is returned as stored and
rendered by the client as text.
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import Tasks
from app.core.logging import get_logger
from app.tasks.models import Task, TaskEvent, TaskStep
from app.tasks.schemas import (
    ActivityAnswer,
    ActivityReport,
    TaskDetail,
    TaskEventRead,
    TaskList,
    TaskRead,
    TaskStepRead,
)
from app.tasks.states import TaskState

logger = get_logger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _step(step: TaskStep) -> TaskStepRead:
    return TaskStepRead.model_validate(step, from_attributes=True)


def _event(event: TaskEvent) -> TaskEventRead:
    return TaskEventRead(
        id=event.id,
        event_type=event.event_type,
        actor=event.actor,
        sequence=event.sequence,
        occurred_at=event.occurred_at,
        metadata=event.event_metadata or {},
    )


def _task(task: Task, step_count: int = 0) -> TaskRead:
    return TaskRead(
        id=task.id,
        owner_id=task.owner_id,
        conversation_id=task.conversation_id,
        origin=task.origin,
        objective=task.objective,
        state=task.state,
        priority=task.priority,
        deadline=task.deadline,
        current_step=task.current_step,
        budget=task.budget or {},
        spent=task.spent or {},
        error_code=task.error_code,
        result=task.result,
        failure_count=task.failure_count,
        completed_at=task.completed_at,
        cancelled_at=task.cancelled_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        step_count=step_count,
    )


@router.get("", response_model=TaskList)
async def list_tasks(
    service: Tasks,
    state: Optional[List[TaskState]] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> TaskList:
    """This owner's tasks, newest first. Bounded."""
    tasks, total = await service.list_tasks(states=state, limit=limit)
    return TaskList(tasks=[_task(t) for t in tasks], total=total)


@router.get("/activity", response_model=ActivityReport)
async def activity(service: Tasks) -> ActivityReport:
    """The six activity questions, each answered from persisted records.

    Deliberately before `/{task_id}` in the file: a literal path segment must
    be declared ahead of a parameterised one, or `activity` is read as a task
    id and every request 422s.
    """
    answers = {}
    for key in ("did", "doing", "will_do", "failed", "waiting_for", "needs_approval"):
        question, states, tasks, total = await service.activity(key)
        answers[key] = ActivityAnswer(
            question=question,
            states=list(states),
            tasks=[_task(t) for t in tasks],
            total=total,
        )
    return ActivityReport(**answers)


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: uuid.UUID, service: Tasks) -> TaskDetail:
    """One task, with its steps and its journal."""
    task = await service.get_detail(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found"
        )
    base = _task(task, step_count=len(task.steps))
    return TaskDetail(
        **base.model_dump(),
        steps=[_step(s) for s in sorted(task.steps, key=lambda s: s.sequence)],
        events=[_event(e) for e in sorted(task.events, key=lambda e: e.sequence)],
        plan=task.plan,
    )


@router.get("/{task_id}/steps", response_model=List[TaskStepRead])
async def get_steps(task_id: uuid.UUID, service: Tasks) -> List[TaskStepRead]:
    task = await service.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found"
        )
    return [_step(s) for s in await service.steps_for(task_id)]


@router.get("/{task_id}/events", response_model=List[TaskEventRead])
async def get_events(
    task_id: uuid.UUID,
    service: Tasks,
    limit: int = Query(default=200, ge=1, le=500),
) -> List[TaskEventRead]:
    task = await service.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task_not_found"
        )
    return [_event(e) for e in await service.events_for(task_id, limit=limit)]


__all__ = ["router"]
