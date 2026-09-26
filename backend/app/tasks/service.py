"""Creating, reading and transitioning tasks. Nothing executes.

Three responsibilities, deliberately in one auditable place: turning a user
turn into a persisted task, moving it between declared states, and answering
the six activity questions from records.

### What this module cannot do

There is no runner here, and no seam for one. `advance`, `run`, `execute`,
`claim` and `dispatch` do not exist, no method moves a task to `running` or
`completed`, and `spent` is never written. That is not an accident of the
stage being early -- `STAGE_6A_REACHABLE` names the states this service may
produce, and a structural test pins the two against each other.

### Who may create a task

A person, and nothing else. `create_for_user` is the only constructor, it
takes `TaskOrigin.USER` (the enum's only member), and no capability,
integration or synthesis module imports this file -- which a structural test
also asserts. The rule matters more here than anywhere else in Mai: an email
that could create a task would be an email that sets Mai working, and every
later stage makes that task more capable.

### The objective is data

A task's objective is the user's own words. It is stored, shown and compared;
it is never parsed for instructions, never logged, and never used to select a
tool. Stage 6C will choose capabilities from a registry, not from this text.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.planning import limits
from app.tasks import events as journal
from app.tasks.capabilities import BindingStatus, bind_plan
from app.tasks.plans import PlanCheck, validate_for_task
from app.tasks.models import (
    LOCAL_OWNER_ID,
    MAX_CAPABILITY_CHARS,
    MAX_OBJECTIVE_CHARS,
    MAX_STEP_KEY_CHARS,
    MAX_STEP_TITLE_CHARS,
    Priority,
    Task,
    TaskEventType,
    TaskOrigin,
    TaskStep,
)
from app.tasks.schemas import (
    BUDGET_KEYS,
    DEFAULT_BUDGET,
    TaskOutcome,
    TaskResult,
)
from app.tasks.states import (
    REACHABLE_STATES,
    RUNNER_ONLY_STATES,
    WAITING_STATES,
    TaskState,
    TaskStepState,
    can_step_transition,
    can_transition,
    is_step_terminal,
    is_terminal,
)

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)

#: Most tasks returned by one listing.
MAX_LISTED = 50
#: Most steps one plan may be materialised into.
#:
#: An alias, not a second bound. Plan size is the planner's to own, and
#: Stage 6B removed the duplicate check that lived here: the validator
#: refuses an oversized plan first, with its own `too_many_tasks` reason, so
#: a copy of the number here could only ever disagree with the real one.
MAX_STEPS = limits.MAX_TASKS


class TaskService:
    """Tasks, their steps and their journal. No execution."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        owner_id: uuid.UUID = LOCAL_OWNER_ID,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        #: Whose tasks this service instance may see or change. Every query
        #: below filters on it; nothing accepts an owner as an argument.
        self._owner_id = owner_id

    @property
    def owner_id(self) -> uuid.UUID:
        return self._owner_id

    # --- Creating -----------------------------------------------------------

    async def create_for_user(
        self,
        objective: str,
        conversation_id: Optional[uuid.UUID] = None,
        priority: Priority = Priority.MEDIUM,
        deadline: Optional[datetime] = None,
        budget: Optional[Dict[str, int]] = None,
    ) -> TaskResult:
        """Create one task from a user turn. The only constructor.

        Named for its origin rather than for what it does, so a call site
        that is not a user turn reads wrong. There is no `create_for_system`,
        no `create_from_content` and no `origin` parameter: `TaskOrigin` has
        one member, and widening it is the change a reviewer would have to
        argue for.
        """
        cleaned = " ".join((objective or "").split())
        if not cleaned:
            return TaskResult(outcome=TaskOutcome.REFUSED, reason="empty_objective")
        if len(cleaned) > MAX_OBJECTIVE_CHARS:
            # Refused rather than truncated. A silently shortened objective is
            # a task working towards something the user did not ask for.
            return TaskResult(outcome=TaskOutcome.REFUSED, reason="objective_too_long")

        resolved_budget = self._resolve_budget(budget)
        if resolved_budget is None:
            return TaskResult(outcome=TaskOutcome.REFUSED, reason="invalid_budget")

        task = Task(
            owner_id=self._owner_id,
            conversation_id=conversation_id,
            origin=TaskOrigin.USER,
            objective=cleaned,
            state=TaskState.PROPOSED,
            priority=priority,
            deadline=deadline,
            budget=resolved_budget,
            # Zero, and nothing in this stage increments it.
            spent={key: 0 for key in DEFAULT_BUDGET},
        )

        try:
            async with self._session.begin_nested():
                self._session.add(task)
                await self._session.flush()
                await journal.record(
                    self._session,
                    task.id,
                    TaskEventType.TASK_CREATED,
                    actor="user",
                    # Counts and constants. Never the objective itself.
                    metadata={
                        "objective_chars": len(cleaned),
                        "priority": priority.value,
                        "has_deadline": deadline is not None,
                    },
                )
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to persist a task",
                extra={"error": str(exc)},
            )
            return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")

        logger.info(
            "Task created",
            extra={
                "task_id": str(task.id),
                "owner_id": str(self._owner_id),
                "objective_chars": len(cleaned),
                "priority": priority.value,
            },
        )
        return TaskResult(
            outcome=TaskOutcome.CREATED, task_id=task.id, state=task.state
        )

    def _resolve_budget(
        self, budget: Optional[Dict[str, int]]
    ) -> Optional[Dict[str, int]]:
        """Merge a caller's budget over the defaults, or refuse it.

        A closed key set and positive integers only. A budget carrying a key
        nothing enforces would read as a bound and be none.
        """
        resolved = dict(DEFAULT_BUDGET)
        if not budget:
            return resolved
        for key, value in budget.items():
            if key not in BUDGET_KEYS:
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                return None
            if value > DEFAULT_BUDGET[key]:
                # A caller may tighten a bound, never loosen it. The ceiling
                # is application code, so no request can raise it.
                return None
            resolved[key] = value
        return resolved

    # --- The plan -----------------------------------------------------------

    async def attach_plan(self, task_id: uuid.UUID, plan) -> TaskResult:
        """Persist a validated plan and materialise its steps.

        Takes `app.planning.schemas.Plan` -- the plan the application
        *accepted* after validating its dependency graph -- rather than the
        `PlanProposal` a model returned. Storing the proposal would be
        storing model output as though it were a decision.

        The plan is written once. A later stage that replans writes a new
        plan and records the event; it does not edit this one, because a plan
        that changes cannot be checked against what was approved.
        """
        task = await self.get(task_id)
        if task is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")
        if is_terminal(task.state):
            return TaskResult(
                outcome=TaskOutcome.TERMINAL, task_id=task.id, state=task.state,
                reason="task_is_terminal",
            )
        if task.plan is not None:
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="plan_already_attached",
            )
        if not can_transition(task.state, TaskState.PLANNED):
            return TaskResult(
                outcome=TaskOutcome.INVALID_TRANSITION, task_id=task.id,
                state=task.state, reason="cannot_plan_from_state",
            )

        # Stage 6B. Validation happens here, on the way in, so it cannot be
        # skipped by constructing a `Plan` directly instead of through
        # `build_plan`. Stage 6A checked only that the list was non-empty and
        # bounded: a cycle, a self-dependency and a dangling edge were all
        # measured to be accepted and materialised into steps.
        check = validate_for_task(plan)
        if not check.ok:
            logger.info(
                "Plan refused",
                # A reason code and a task id. `detail` can name a step id,
                # which came from a model, so it is not logged.
                extra={"task_id": str(task.id), "reason": check.reason},
            )
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason=check.reason,
            )

        tasks_in_plan = list(getattr(plan, "tasks", []) or [])

        try:
            async with self._session.begin_nested():
                task.plan = plan.model_dump(mode="json")
                for position, step in enumerate(tasks_in_plan, start=1):
                    self._session.add(
                        TaskStep(
                            task_id=task.id,
                            step_key=str(step.id)[:MAX_STEP_KEY_CHARS],
                            # Validated above: present, positive, unique.
                            sequence=int(step.order),
                            title=str(step.title)[:MAX_STEP_TITLE_CHARS],
                            state=TaskStepState.PENDING,
                            depends_on=[
                                str(d)[:MAX_STEP_KEY_CHARS]
                                for d in (step.dependencies or [])
                            ],
                        )
                    )
                task.state = TaskState.PLANNED
                task.updated_at = datetime.now(timezone.utc)
                await self._session.flush()

                await journal.record(
                    self._session, task.id, TaskEventType.PLAN_ATTACHED,
                    actor="system",
                    metadata={
                        "step_count": len(tasks_in_plan),
                        "dependency_count": sum(
                            len(s.dependencies or []) for s in tasks_in_plan
                        ),
                    },
                )
                # Assumptions are the plan's own. They are recorded as events
                # rather than copied to a column: the plan already owns them,
                # and a second copy is a second thing to keep true.
                for assumption in list(getattr(plan, "assumptions", []) or [])[:10]:
                    await journal.record(
                        self._session, task.id,
                        TaskEventType.ASSUMPTION_RECORDED,
                        actor="system",
                        metadata={"assumption": str(assumption)},
                    )
                await journal.record(
                    self._session, task.id, TaskEventType.STATE_CHANGED,
                    actor="system",
                    metadata={"from": "proposed", "to": TaskState.PLANNED.value},
                )
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to attach a plan",
                extra={"task_id": str(task_id), "error": str(exc)},
            )
            return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")

        logger.info(
            "Plan attached",
            extra={"task_id": str(task.id), "step_count": len(tasks_in_plan)},
        )
        return TaskResult(
            outcome=TaskOutcome.UPDATED, task_id=task.id, state=task.state
        )

    async def attach_proposal(
        self, task_id: uuid.UUID, proposal, goal
    ) -> TaskResult:
        """Validate a model's `PlanProposal` and attach the result.

        The Stage 6B entry point for model output. `build_plan` is Stage 4B's
        own two-layer pipeline -- schema, then graph -- and it returns a
        `Plan` only for a proposal that passed both, so holding one is the
        proof. Nothing here re-implements it.

        `attach_plan` then validates again. That is not redundancy for its own
        sake: a `Plan` can be constructed directly, so the check that matters
        is the one on the way into the database, and this method exists to
        give model output a named door rather than to be that check.
        """
        from app.planning.validator import PlanValidationError, build_plan

        try:
            plan = build_plan(proposal, goal)
        except PlanValidationError as refusal:
            reason = (
                refusal.args[0].split(":")[0].strip()
                if refusal.args else "invalid_plan"
            )
            logger.info(
                "Proposal refused",
                extra={"task_id": str(task_id), "reason": reason},
            )
            return TaskResult(outcome=TaskOutcome.REFUSED, reason=reason)
        except Exception:  # noqa: BLE001 - malformed model output is not a 500
            return TaskResult(outcome=TaskOutcome.REFUSED, reason="invalid_plan")

        return await self.attach_plan(task_id, plan)

    # --- Stage 6C: capability binding and authorization ---------------------

    async def authorize_plan(self, task_id: uuid.UUID) -> TaskResult:
        """Bind every step's capability, then decide whether execution may begin.

        The one place a persisted plan becomes permission, and the reason it
        is a separate call rather than part of `attach_plan`: a plan that
        authorised itself on arrival would make planning and permission the
        same act, which is exactly the boundary Stage 6B established.

        All or nothing. A plan whose steps do not all bind is refused before
        any execution state exists -- deciding halfway through would leave a
        task that can never finish and a journal that says it started.

        Nothing runs here. What this produces is a task in `queued` (or
        `awaiting_approval`) with each step's capability bound to the
        registry's canonical name. Creating the execution records is
        `create_step_execution`; running one needs a runner, which does not
        exist.
        """
        task = await self.get_detail(task_id)
        if task is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")
        if is_terminal(task.state):
            return TaskResult(
                outcome=TaskOutcome.TERMINAL, task_id=task.id, state=task.state,
                reason="task_is_terminal",
            )
        if task.plan is None:
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="no_plan_attached",
            )
        if task.authorized_at is not None:
            # Authorising twice would re-open a decision a person already
            # made, and the second answer could differ from the first.
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="already_authorized",
            )
        if task.state is not TaskState.PLANNED:
            return TaskResult(
                outcome=TaskOutcome.INVALID_TRANSITION, task_id=task.id,
                state=task.state, reason="cannot_authorize_from_state",
            )

        steps = sorted(task.steps, key=lambda step: step.sequence)
        # Read from the *plan*, which is what the model proposed. The rows do
        # not carry a capability until this method writes one.
        proposed = {
            str(entry.get("id")): entry
            for entry in (task.plan.get("tasks") or [])
            if isinstance(entry, dict)
        }

        class _Proposed:
            __slots__ = ("step_key", "capability", "arguments")

            def __init__(self, key, entry):
                self.step_key = key
                self.capability = entry.get("capability")
                self.arguments = entry.get("arguments") or {}

        binding = bind_plan(
            [_Proposed(step.step_key, proposed.get(step.step_key, {})) for step in steps]
        )
        if not binding.ok:
            logger.info(
                "Plan authorization refused",
                # A reason code, a task id and a count. Never a capability
                # name the model supplied, and never an argument value.
                extra={
                    "task_id": str(task.id),
                    "reason": binding.reason,
                    "step_count": len(steps),
                },
            )
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason=binding.reason,
            )

        now = datetime.now(timezone.utc)
        target = (
            TaskState.AWAITING_APPROVAL if binding.needs_approval else TaskState.QUEUED
        )
        by_key = {b.step_key: b for b in binding.bindings}

        try:
            async with self._session.begin_nested():
                for step in steps:
                    bound = by_key[step.step_key]
                    # The registry's canonical name, never the model's
                    # spelling. This is the value execution reads.
                    step.capability = (bound.capability or "")[:MAX_CAPABILITY_CHARS]
                    step.arguments = dict(
                        (proposed.get(step.step_key) or {}).get("arguments") or {}
                    )
                    step.updated_at = now

                task.authorized_at = now
                task.state = target
                task.updated_at = now
                await self._session.flush()

                await journal.record(
                    self._session, task.id,
                    TaskEventType.APPROVAL_REQUESTED
                    if target is TaskState.AWAITING_APPROVAL
                    else TaskEventType.APPROVAL_GRANTED,
                    actor="policy",
                    metadata={
                        "step_count": len(steps),
                        "requires_approval": binding.needs_approval,
                        "to": target.value,
                    },
                )
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to authorize a plan",
                extra={"task_id": str(task_id), "error": str(exc)},
            )
            return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")

        logger.info(
            "Plan authorized",
            extra={
                "task_id": str(task.id),
                "state": target.value,
                "step_count": len(steps),
            },
        )
        return TaskResult(
            outcome=TaskOutcome.UPDATED, task_id=task.id, state=target
        )

    def authorization_valid(self, task, now: Optional[datetime] = None) -> bool:
        """Whether this task's authorization is still in date.

        A permission with no expiry is a standing grant by omission. The
        window is the same one execution approvals use and for the same
        reason: a decision taken an hour ago is not a decision about now.
        """
        if task is None or task.authorized_at is None:
            return False
        moment = now or datetime.now(timezone.utc)
        granted = task.authorized_at
        if granted.tzinfo is None:
            # SQLite hands back naive values; reading one as machine-local
            # would expire a fresh grant or revive a stale one.
            granted = granted.replace(tzinfo=timezone.utc)
        ttl = int(
            getattr(self._settings, "TASK_AUTHORIZATION_TTL_SECONDS", 900) or 900
        )
        return moment <= granted + timedelta(seconds=ttl)

    @staticmethod
    def budget_exceeded(task) -> Optional[str]:
        """Which bound this task has reached, or None.

        Only the two counters the application can actually measure are
        enforced: steps started and executions created. `max_model_calls`
        and `max_seconds` are recorded and deliberately not enforced --
        the runner makes no model call, and claiming to enforce a bound
        nothing measures would be the fabrication this codebase exists to
        avoid.
        """
        budget = dict(task.budget or {})
        spent = dict(task.spent or {})
        for key in ("max_steps", "max_tool_calls"):
            limit = budget.get(key)
            used = spent.get(key, 0)
            if isinstance(limit, int) and isinstance(used, int) and used >= limit:
                return key
        return None

    # --- Stage 6C: dependency-aware readiness --------------------------------

    async def runnable_steps(self, task_id: uuid.UUID) -> List[TaskStep]:
        """The steps whose dependencies are all satisfied, in plan order.

        The validated graph decides the order, not the sequence number. A
        plan whose edges permit `c` before `b` gets `c` offered, and a step
        whose dependency failed is never offered at all -- a dependent of a
        failed step cannot become runnable by waiting.
        """
        task = await self.get_detail(task_id)
        if task is None or task.authorized_at is None:
            # An unauthorised plan has no runnable steps, whatever its graph
            # says. Readiness is downstream of permission.
            return []
        if is_terminal(task.state):
            return []

        by_key = {step.step_key: step for step in task.steps}
        ready: List[TaskStep] = []
        for step in sorted(task.steps, key=lambda s: s.sequence):
            if step.state is not TaskStepState.PENDING:
                continue
            blocked = False
            for dependency in step.depends_on or []:
                prior = by_key.get(dependency)
                if prior is None or prior.state is not TaskStepState.COMPLETED:
                    blocked = True
                    break
            if not blocked:
                ready.append(step)
        return ready

    async def create_step_execution(
        self, task_id: uuid.UUID, step_key: str, executions=None
    ) -> TaskResult:
        """Record the execution one ready step would perform. Runs nothing.

        Uses `ExecutionService.create`, which already owns the authorization
        decision, the idempotency key, the duplicate handling and the audit
        event. Nothing here re-implements any of that: a second execution
        system would be a second place for the authorization decision to be
        got wrong.

        The execution is left `PROPOSED`. Approving and running it is the
        existing execution lifecycle's, and wiring it to happen here would
        make attaching a plan begin work -- the thing Stage 6B forbade.
        """
        task = await self.get_detail(task_id)
        if task is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")
        if task.authorized_at is None:
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="plan_not_authorized",
            )
        if is_terminal(task.state):
            return TaskResult(
                outcome=TaskOutcome.TERMINAL, task_id=task.id, state=task.state,
                reason="task_is_terminal",
            )

        step = next((s for s in task.steps if s.step_key == step_key), None)
        if step is None:
            return TaskResult(
                outcome=TaskOutcome.NOT_FOUND, task_id=task.id, reason="step_not_found"
            )
        if step.execution_id is not None:
            # One execution per step. Returning the existing record is what
            # makes this safe to call twice.
            return TaskResult(
                outcome=TaskOutcome.UPDATED, task_id=task.id, state=task.state,
                reason="execution_already_created",
            )
        if not step.capability:
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="step_declares_no_capability",
            )

        ready = {s.step_key for s in await self.runnable_steps(task_id)}
        if step_key not in ready:
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="dependencies_incomplete",
            )

        if executions is None:
            from app.execution.service import ExecutionService

            executions = ExecutionService(self._session, settings=self._settings)

        from app.execution.errors import ExecutionError
        from app.execution.schemas import ExecutionRequest

        try:
            execution = await executions.create(
                ExecutionRequest(
                    tool_name=step.capability,
                    arguments=dict(step.arguments or {}),
                    # Derived, not random: the same step of the same task is
                    # one action however many times it is requested.
                    idempotency_key=f"task:{task.id}:{step.step_key}"[:128],
                ),
                conversation_id=task.conversation_id,
            )
        except ExecutionError as refusal:
            logger.info(
                "Step execution refused",
                extra={"task_id": str(task.id), "reason": refusal.reason},
            )
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason=refusal.reason,
            )
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to create a step execution",
                extra={"task_id": str(task.id), "error": str(exc)},
            )
            return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")

        step.execution_id = execution.id
        step.updated_at = datetime.now(timezone.utc)
        await self._session.flush()

        logger.info(
            "Step execution created",
            extra={"task_id": str(task.id), "execution_id": str(execution.id)},
        )
        return TaskResult(
            outcome=TaskOutcome.UPDATED, task_id=task.id, state=task.state
        )

    # --- Stage 6C: step lifecycle --------------------------------------------
    #
    # Transitions only. Each is driven by an external caller -- there is no
    # loop here, and nothing calls these on its own.

    async def mark_step_started(
        self, task_id: uuid.UUID, step_key: str
    ) -> TaskResult:
        return await self._move_step(
            task_id, step_key, TaskStepState.RUNNING,
            TaskEventType.STEP_STARTED, require_ready=True,
        )

    async def mark_step_completed(
        self, task_id: uuid.UUID, step_key: str
    ) -> TaskResult:
        return await self._move_step(
            task_id, step_key, TaskStepState.COMPLETED,
            TaskEventType.STEP_COMPLETED,
        )

    async def mark_step_failed(
        self, task_id: uuid.UUID, step_key: str, reason: Optional[str] = None
    ) -> TaskResult:
        return await self._move_step(
            task_id, step_key, TaskStepState.FAILED,
            TaskEventType.STEP_FAILED, reason=reason,
        )

    async def _move_step(
        self,
        task_id: uuid.UUID,
        step_key: str,
        target: TaskStepState,
        event: TaskEventType,
        require_ready: bool = False,
        reason: Optional[str] = None,
    ) -> TaskResult:
        """Move one step, enforcing the step state machine and the graph."""
        task = await self.get_detail(task_id)
        if task is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")
        if task.authorized_at is None:
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                reason="plan_not_authorized",
            )
        if is_terminal(task.state):
            return TaskResult(
                outcome=TaskOutcome.TERMINAL, task_id=task.id, state=task.state,
                reason="task_is_terminal",
            )

        step = next((s for s in task.steps if s.step_key == step_key), None)
        if step is None:
            return TaskResult(
                outcome=TaskOutcome.NOT_FOUND, task_id=task.id, reason="step_not_found"
            )
        if is_step_terminal(step.state):
            return TaskResult(
                outcome=TaskOutcome.TERMINAL, task_id=task.id, state=task.state,
                reason="step_is_terminal",
            )
        if not can_step_transition(step.state, target):
            return TaskResult(
                outcome=TaskOutcome.INVALID_TRANSITION, task_id=task.id,
                state=task.state, reason="undeclared_step_transition",
            )
        if require_ready:
            ready = {s.step_key for s in await self.runnable_steps(task_id)}
            if step_key not in ready:
                return TaskResult(
                    outcome=TaskOutcome.REFUSED, task_id=task.id, state=task.state,
                    reason="dependencies_incomplete",
                )

        now = datetime.now(timezone.utc)
        try:
            async with self._session.begin_nested():
                step.state = target
                step.updated_at = now
                if target is TaskStepState.RUNNING:
                    step.started_at = now
                if target in {TaskStepState.COMPLETED, TaskStepState.FAILED}:
                    step.completed_at = now
                await self._session.flush()
                await journal.record(
                    self._session, task.id, event, actor="system",
                    metadata={
                        "step": step.step_key,
                        "sequence": step.sequence,
                        "reason": reason,
                    },
                )
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to move a step",
                extra={"task_id": str(task_id), "error": str(exc)},
            )
            return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")

        return TaskResult(
            outcome=TaskOutcome.UPDATED, task_id=task.id, state=task.state
        )

    # --- Transitions --------------------------------------------------------

    async def transition(
        self,
        task_id: uuid.UUID,
        target: TaskState,
        actor: str = "user",
        reason: Optional[str] = None,
    ) -> TaskResult:
        """Move a task to a declared state. The only mutator of `state`.

        Refuses anything that is not an edge in `ALLOWED_TRANSITIONS`, and
        refuses every state Stage 6A cannot reach -- so a caller cannot use
        this to declare a task `completed` in a stage with no runner.
        """
        if target in RUNNER_ONLY_STATES:
            # `running` and `completed` mean a step actually ran. Only
            # `TaskRunner` may say so, and it writes them directly rather
            # than through this method -- so every other caller, including
            # every API path, is refused here.
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task_id,
                reason="state_not_reachable_in_this_stage",
            )
        if target not in REACHABLE_STATES:  # pragma: no cover - the set is total
            return TaskResult(
                outcome=TaskOutcome.REFUSED, task_id=task_id,
                reason="state_not_reachable_in_this_stage",
            )

        task = await self.get(task_id)
        if task is None:
            return TaskResult(outcome=TaskOutcome.NOT_FOUND, reason="task_not_found")

        current = task.state
        if is_terminal(current):
            return TaskResult(
                outcome=TaskOutcome.TERMINAL, task_id=task.id, state=current,
                reason="task_is_terminal",
            )
        if not can_transition(current, target):
            logger.info(
                "Task transition refused",
                extra={
                    "task_id": str(task.id),
                    "from": current.value,
                    "to": target.value,
                },
            )
            return TaskResult(
                outcome=TaskOutcome.INVALID_TRANSITION, task_id=task.id,
                state=current, reason="undeclared_transition",
            )

        now = datetime.now(timezone.utc)
        try:
            async with self._session.begin_nested():
                task.state = target
                task.updated_at = now
                if target is TaskState.CANCELLED:
                    task.cancelled_at = now
                    # Pending steps go with it. A cancelled task with steps
                    # still "pending" would answer "what will you do?" wrongly.
                    await self._cancel_pending_steps(task.id)
                if target is TaskState.FAILED:
                    task.error_code = (reason or "task_failed")[:64]
                    task.failure_count = int(task.failure_count or 0) + 1
                await self._session.flush()

                await journal.record(
                    self._session, task.id, _EVENT_FOR.get(
                        target, TaskEventType.STATE_CHANGED
                    ),
                    actor=actor,
                    metadata={
                        "from": current.value,
                        "to": target.value,
                        "reason": reason,
                    },
                )
        except _DB_ERRORS as exc:
            logger.error(
                "Failed to transition a task",
                extra={"task_id": str(task_id), "error": str(exc)},
            )
            return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")

        logger.info(
            "Task transitioned",
            extra={
                "task_id": str(task.id),
                "from": current.value,
                "to": target.value,
                "actor": actor,
            },
        )
        outcome = (
            TaskOutcome.CANCELLED if target is TaskState.CANCELLED
            else TaskOutcome.UPDATED
        )
        return TaskResult(outcome=outcome, task_id=task.id, state=target)

    async def cancel(
        self, task_id: uuid.UUID, reason: Optional[str] = None
    ) -> TaskResult:
        """Cancel a task. A convenience over `transition`, same rules."""
        return await self.transition(
            task_id, TaskState.CANCELLED, actor="user", reason=reason
        )

    async def _cancel_pending_steps(self, task_id: uuid.UUID) -> None:
        steps = (
            await self._session.execute(
                select(TaskStep).where(
                    TaskStep.task_id == task_id,
                    TaskStep.state == TaskStepState.PENDING,
                )
            )
        ).scalars().all()
        for step in steps:
            step.state = TaskStepState.CANCELLED

    # --- Reading ------------------------------------------------------------

    async def get(self, task_id: uuid.UUID) -> Optional[Task]:
        """One task of this owner's, or None.

        The owner filter is in the query rather than checked afterwards, so
        there is no branch where a row is loaded and then found to belong to
        someone else -- the shape of bug that leaks an id or a timing signal.
        """
        try:
            return (
                await self._session.execute(
                    select(Task).where(
                        Task.id == task_id, Task.owner_id == self._owner_id
                    )
                )
            ).scalars().first()
        except _DB_ERRORS as exc:
            logger.error("Failed to read a task", extra={"error": str(exc)})
            return None

    async def get_detail(self, task_id: uuid.UUID) -> Optional[Task]:
        """One task with its steps and journal loaded."""
        try:
            return (
                await self._session.execute(
                    select(Task)
                    .where(Task.id == task_id, Task.owner_id == self._owner_id)
                    .options(selectinload(Task.steps), selectinload(Task.events))
                )
            ).scalars().first()
        except _DB_ERRORS as exc:
            logger.error("Failed to read a task", extra={"error": str(exc)})
            return None

    async def list_tasks(
        self,
        states: Optional[Sequence[TaskState]] = None,
        limit: int = MAX_LISTED,
    ) -> Tuple[List[Task], int]:
        """This owner's tasks, newest first, bounded."""
        bounded = max(1, min(int(limit), MAX_LISTED))
        where = [Task.owner_id == self._owner_id]
        if states:
            where.append(Task.state.in_(list(states)))
        try:
            rows = (
                await self._session.execute(
                    select(Task)
                    .where(*where)
                    .order_by(Task.created_at.desc())
                    .limit(bounded)
                )
            ).scalars().all()
            total = (
                await self._session.execute(
                    select(func.count()).select_from(Task).where(*where)
                )
            ).scalar_one()
        except _DB_ERRORS as exc:
            logger.error("Failed to list tasks", extra={"error": str(exc)})
            return [], 0
        return list(rows), int(total)

    async def steps_for(self, task_id: uuid.UUID) -> List[TaskStep]:
        task = await self.get(task_id)
        if task is None:
            return []
        rows = (
            await self._session.execute(
                select(TaskStep)
                .where(TaskStep.task_id == task_id)
                .order_by(TaskStep.sequence.asc())
            )
        ).scalars().all()
        return list(rows)

    async def events_for(
        self, task_id: uuid.UUID, limit: int = 200
    ) -> List:
        from app.tasks.models import TaskEvent

        task = await self.get(task_id)
        if task is None:
            return []
        rows = (
            await self._session.execute(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id)
                .order_by(TaskEvent.sequence.asc())
                .limit(max(1, min(int(limit), 500)))
            )
        ).scalars().all()
        return list(rows)

    # --- The six activity questions ----------------------------------------
    #
    # Each is a query over persisted state. None consults a model, and none
    # is answered from prose -- which is what makes the answers checkable.

    #: Question text and the states that answer it. Data rather than six
    #: methods, so the set cannot drift from the state machine unnoticed.
    ACTIVITY_QUESTIONS = {
        "did": ("What did you do?", (TaskState.COMPLETED,)),
        "doing": ("What are you doing?", (TaskState.RUNNING,)),
        "will_do": (
            "What are you planning to do?",
            (TaskState.PROPOSED, TaskState.PLANNED, TaskState.QUEUED),
        ),
        "failed": ("What failed?", (TaskState.FAILED,)),
        "waiting_for": ("What are you waiting for?", tuple(sorted(
            WAITING_STATES, key=lambda s: s.value
        ))),
        "needs_approval": (
            "What requires approval?", (TaskState.AWAITING_APPROVAL,)
        ),
    }

    async def activity(self, key: str) -> Tuple[str, Tuple[TaskState, ...], List[Task], int]:
        """Answer one activity question from records."""
        question, states = self.ACTIVITY_QUESTIONS[key]
        tasks, total = await self.list_tasks(states=states)
        return question, states, tasks, total


#: Which journal entry a transition writes. A terminal state gets its own
#: event type so "what failed?" can be answered from the journal as well as
#: from the row.
_EVENT_FOR = {
    TaskState.CANCELLED: TaskEventType.TASK_CANCELLED,
    TaskState.FAILED: TaskEventType.TASK_FAILED,
    TaskState.BLOCKED: TaskEventType.TASK_BLOCKED,
}


__all__ = ["MAX_LISTED", "MAX_STEPS", "TaskService"]
