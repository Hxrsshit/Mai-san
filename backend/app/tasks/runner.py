"""Stage 6D: the task runner. One task, one step, one transition per call.

### What this is, and what it deliberately is not

`TaskRunner.advance` does exactly one thing: it finds the single next step of
one task that is genuinely ready, claims it, runs it through the existing
dispatcher, and records what happened. Then it returns.

There is no loop. Nothing calls `advance` on its own -- no scheduler, no
background task, no `asyncio` anywhere in this module. A caller invokes it,
gets a deterministic result, and decides whether to invoke it again. That
constraint is the whole design: a runner that drives itself is an autonomous
agent, and this stage was not permission to build one.

### The safety envelope is borrowed, not rebuilt

Every gate already existed before this file:

* `TaskService.runnable_steps` -- Stage 6C's dependency-aware readiness.
* `AuthorizationService` -- reached through `app.tasks.capabilities`, which
  re-binds the capability name at run time rather than trusting what was
  bound at authorization time.
* `ExecutionService.create` -- the one Execution constructor, with its own
  idempotency key and duplicate handling.
* `ExecutionService.approve` -- the fingerprint-and-TTL binding.
* `Dispatcher.dispatch` -- which re-checks enablement, approval, authorization
  and arguments, and claims the execution atomically.

This module adds one gate of its own, and one only: the **step claim**, a
conditional `UPDATE ... WHERE state = 'pending'` whose `rowcount` decides
which of two concurrent callers owns the step. It is the reminder scheduler's
pattern, reused rather than reinvented.

### Approval

The runner approves an execution only when policy already decided no person
is needed -- `authorization_status is ALLOWED`. A capability marked
`approval_required` blocks the runner and waits for a human to call
`ExecutionService.approve` themselves.

That is not a standing approval and not a new authority: the decision was
taken by `app.tools.policy` when the capability was declared, and the runner
merely declines to pretend a person is present.

### Truthfulness

Every outcome below corresponds to something that did or did not happen, read
from the database afterwards. The runner never reports a completion it did
not observe, and `RunnerOutcome` has no member meaning "probably fine".
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.execution.errors import ExecutionError
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.execution.schemas import ExecutionRequest
from app.tasks import events as journal
from app.tasks.capabilities import BindingStatus, bind_step
from app.tasks.models import TaskEventType, TaskStep
from app.tasks.schemas import RunnerOutcome, RunnerResult
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState, is_terminal
from app.tools.schemas import ActionProposal, ActionSource

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)


class TaskRunner:
    """Advances one task by one step. Never loops, never schedules."""

    def __init__(
        self,
        tasks: TaskService,
        executions: Optional[ExecutionService] = None,
        authorization=None,
        grants=None,
    ) -> None:
        self._tasks = tasks
        self._session = tasks._session
        self._settings = tasks._settings
        self._executions = executions or ExecutionService(
            self._session, settings=self._settings
        )
        # Stage 6E. The one authorization service, and the grant lookup it
        # consults. The runner holds the lookup only to hand it over: it
        # never calls a method on it, and never reads a grant itself.
        from app.authorization.grants import GrantService
        from app.tools.authorization import AuthorizationService

        self._authorization = authorization or AuthorizationService()
        self._grants = grants if grants is not None else GrantService(
            self._session, owner_id=tasks.owner_id
        )

    async def advance(self, task_id: uuid.UUID) -> RunnerResult:
        """Advance this task by at most one step. Safe to call repeatedly.

        Returns a `RunnerResult` describing exactly what happened. Never
        raises: a runner that throws leaves a claimed step with nobody to
        release it, so every failure is an outcome instead.
        """
        try:
            return await self._advance(task_id)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.error(
                "Runner failed",
                extra={"task_id": str(task_id), "error": type(exc).__name__},
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task_id,
                reason="runner_error",
            )

    async def _advance(self, task_id: uuid.UUID) -> RunnerResult:
        task = await self._tasks.get_detail(task_id)
        if task is None:
            # Includes another owner's task: `get_detail` filters on owner in
            # the query, so there is no branch where a foreign row is loaded
            # and then rejected.
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task_id,
                reason="task_not_found",
            )

        refusal = await self._refuse_task(task)
        if refusal is not None:
            return refusal

        if task.monitor is not None:
            # Stage 6G. A monitoring task's one step is the template for a
            # repeated check, not a step to run once -- running it here would
            # perform the check and complete the task without ever asking
            # whether the condition held.
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="monitoring_task_use_check",
            )

        ready = await self._tasks.runnable_steps(task_id)
        if not ready:
            return await self._nothing_ready(task)

        # One step. The first in plan order, and only the first -- advancing
        # two would make a single invocation's effect depend on how much work
        # happened to be available.
        step = ready[0]

        binding = bind_step(step.step_key, step.capability, step.arguments)
        if not binding.bindable:
            # Re-bound now rather than trusted from authorization time. A
            # capability can be withdrawn between the two, and the question
            # that matters is whether it is permitted *now*.
            await self._record(
                task, TaskEventType.RUNNER_REFUSED,
                {"step": step.step_key, "reason": binding.reason},
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                step_key=step.step_key, reason=binding.reason,
            )

        claimed = await self._claim(step)
        if not claimed:
            # Another caller took it between the read and the write.
            return RunnerResult(
                outcome=RunnerOutcome.BLOCKED, task_id=task.id,
                step_key=step.step_key, reason="step_already_claimed",
            )

        return await self._run_claimed_step(task, step, binding)

    # --- Stage 6G: one monitoring check -----------------------------------------

    async def check(self, task_id: uuid.UUID) -> RunnerResult:
        """Perform one monitoring check. Safe to call repeatedly. Never raises.

        Every gate `advance` uses, in the same order: owner-filtered load,
        terminal / authorisation / expiry / state / budget refusal,
        capability re-binding, the one authorization service, the one
        execution constructor, and the same approval rule. What differs is
        what happens to the result: it is evaluated against the task's
        condition rather than marked done.
        """
        try:
            return await self._check(task_id)
        except Exception as exc:  # noqa: BLE001 - see `advance`
            logger.error(
                "Monitoring check failed",
                extra={"task_id": str(task_id), "error": type(exc).__name__},
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task_id,
                reason="runner_error",
            )

    async def _check(self, task_id: uuid.UUID) -> RunnerResult:
        from app.tasks.monitoring import (
            MONITORABLE_CAPABILITIES,
            CheckResult,
            SpecRefused,
            evaluate,
            parse_spec,
        )

        task = await self._tasks.get_detail(task_id)
        if task is None:
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task_id,
                reason="task_not_found",
            )
        refusal = await self._refuse_task(task)
        if refusal is not None:
            return refusal
        if task.monitor is None:
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="not_a_monitoring_task",
            )

        # The stored spec, re-validated. It was validated on the way in; a
        # row edited since is refused rather than trusted.
        try:
            spec = parse_spec(task.monitor)
        except SpecRefused as refused:
            await self._record(
                task, TaskEventType.RUNNER_REFUSED,
                {"reason": "invalid_monitoring_config", "detail": refused.reason},
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="invalid_monitoring_config",
            )

        if len(task.steps) != 1:
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="invalid_monitoring_config",
            )
        step = task.steps[0]

        binding = bind_step(step.step_key, step.capability, step.arguments)
        if not binding.bindable:
            await self._record(
                task, TaskEventType.RUNNER_REFUSED,
                {"step": step.step_key, "reason": binding.reason},
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                step_key=step.step_key, reason=binding.reason,
            )
        if binding.capability not in MONITORABLE_CAPABILITIES:
            # Checked again here, not only at configuration: a step's bound
            # capability is a column, and a repeated side effect must be
            # impossible however the row came to name one.
            await self._record(
                task, TaskEventType.RUNNER_REFUSED,
                {"step": step.step_key, "reason": "capability_not_monitorable"},
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                step_key=step.step_key, reason="capability_not_monitorable",
            )

        sequence = int(task.check_count or 0) + 1
        if not await self._claim_check(task, sequence):
            return RunnerResult(
                outcome=RunnerOutcome.BLOCKED, task_id=task.id,
                state=task.state, reason="check_already_claimed",
            )
        await self._record(
            task, TaskEventType.MONITORING_CHECK_STARTED, {"check": sequence},
        )

        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name=binding.capability,
                    arguments=dict(step.arguments or {}),
                    # One identity per check: the check at 09:00 and the
                    # check at 10:00 are different actions.
                    idempotency_key=f"task:{task.id}:check:{sequence}"[:128],
                ),
                conversation_id=task.conversation_id,
            )
        except ExecutionError as refusal:
            return await self._check_failed(task, sequence, refusal.reason, None)

        await self._record(
            task, TaskEventType.EXECUTION_CREATED,
            {"check": sequence, "execution_id": str(execution.id)},
        )

        already_approved = execution.state is ExecutionState.APPROVED
        decision = await self._authorization.authorize_with_grants(
            ActionProposal(
                tool_name=binding.capability,
                arguments=dict(step.arguments or {}),
                source=ActionSource.MODEL,
            ),
            grants=self._grants,
        )
        if decision.standing_grant_id is not None:
            await self._record(
                task, TaskEventType.STANDING_GRANT_USED,
                {
                    "check": sequence,
                    "capability": decision.tool_name,
                    "grant_id": str(decision.standing_grant_id),
                },
            )
        if not (already_approved or not decision.requires_approval):
            # A person decides this one and none has. The check number goes
            # back, so the next attempt reuses this execution identity and
            # finds the approval once someone gives it -- the same reason
            # `advance` releases a step.
            await self._release_check(task, sequence)
            await self._record(
                task, TaskEventType.RUNNER_BLOCKED,
                {"check": sequence, "reason": "awaiting_human_approval"},
            )
            return RunnerResult(
                outcome=RunnerOutcome.BLOCKED, task_id=task.id,
                execution_id=execution.id, state=task.state,
                reason="awaiting_human_approval",
            )

        # A check is a tool call. `max_steps` is not spent: it is one step,
        # repeated, and counting it per check would end monitoring early for
        # no reason the budget describes.
        await self._spend(task, "max_tool_calls")

        try:
            if not already_approved:
                await self._executions.approve(execution.id)
            _, outcome = await self._executions.run_returning_outcome(execution.id)
        except ExecutionError as refusal:
            return await self._check_failed(task, sequence, refusal.reason, execution.id)

        data = getattr(outcome, "data", None) if outcome is not None else None
        evaluation = evaluate(spec.condition, data)
        await self._record(
            task, TaskEventType.OBSERVATION_RECORDED,
            {
                "check": sequence,
                "result": evaluation.result.value,
                "kind": spec.condition.kind.value,
                "operator": spec.condition.operator.value,
                # A number, or a length. Never observed text: it came from
                # outside Mai and belongs in neither the journal nor a log.
                "observed": evaluation.observed_number,
                "observed_chars": evaluation.observed_chars,
                "execution_id": str(execution.id),
            },
        )

        if evaluation.result is CheckResult.UNABLE:
            return await self._check_failed(
                task, sequence, evaluation.reason or "unable_to_evaluate", execution.id
            )

        if evaluation.result is CheckResult.NOT_SATISFIED:
            return RunnerResult(
                outcome=RunnerOutcome.CONDITION_NOT_MET, task_id=task.id,
                execution_id=execution.id, state=task.state,
            )

        # Satisfied. The step completes through its legal edges -- pending
        # to running to completed -- carrying the check that proved it, and
        # the task completes through the one path that may complete a task.
        now = datetime.now(timezone.utc)
        step.state = TaskStepState.RUNNING
        step.started_at = now
        await self._session.flush()
        step.state = TaskStepState.COMPLETED
        step.completed_at = now
        step.execution_id = execution.id
        step.updated_at = now
        await self._session.flush()
        await self._record(
            task, TaskEventType.MONITORING_TRIGGERED,
            {"check": sequence, "execution_id": str(execution.id)},
        )

        refreshed = await self._tasks.get_detail(task.id)
        completed = await self._complete(refreshed)
        return RunnerResult(
            outcome=(
                RunnerOutcome.CONDITION_MET
                if completed.outcome is RunnerOutcome.TASK_COMPLETED
                else completed.outcome
            ),
            task_id=task.id, step_key=step.step_key, execution_id=execution.id,
            state=completed.state, reason=completed.reason,
        )

    async def _claim_check(self, task, sequence: int) -> bool:
        """Claim check number `sequence`. Exactly one caller wins.

        Conditional on the previous number, so two callers that both read
        `check_count == n` both try to write `n + 1` and one matches nothing.
        """
        from app.tasks.models import Task

        result = await self._session.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.owner_id == self._tasks.owner_id,
                Task.check_count == sequence - 1,
            )
            .values(check_count=sequence, updated_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session="fetch")
        )
        return result.rowcount == 1

    async def _release_check(self, task, sequence: int) -> None:
        """Hand a claimed check number back. Conditional, like the claim."""
        from app.tasks.models import Task

        await self._session.execute(
            update(Task)
            .where(Task.id == task.id, Task.check_count == sequence)
            .values(check_count=sequence - 1)
            .execution_options(synchronize_session="fetch")
        )

    async def _check_failed(self, task, sequence, reason, execution_id) -> RunnerResult:
        """A check that told us nothing. Recorded as a failure, never as false."""
        await self._record(
            task, TaskEventType.MONITORING_CHECK_FAILED,
            {"check": sequence, "reason": reason},
        )
        return RunnerResult(
            outcome=RunnerOutcome.CHECK_FAILED, task_id=task.id,
            execution_id=execution_id, state=task.state, reason=reason,
        )

    # --- Gates ---------------------------------------------------------------

    async def _refuse_task(self, task) -> Optional[RunnerResult]:
        """Everything that stops a task advancing at all."""
        if is_terminal(task.state):
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="task_is_terminal",
            )
        if task.state is TaskState.CANCELLED:  # pragma: no cover - terminal
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="task_cancelled",
            )
        if task.authorized_at is None:
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="plan_not_authorized",
            )
        if not self._tasks.authorization_valid(task):
            await self._record(
                task, TaskEventType.RUNNER_REFUSED, {"reason": "authorization_expired"}
            )
            return RunnerResult(
                outcome=RunnerOutcome.REFUSED, task_id=task.id,
                state=task.state, reason="authorization_expired",
            )
        if task.state not in {TaskState.QUEUED, TaskState.RUNNING}:
            # `awaiting_approval` lands here, which is correct: a plan whose
            # capabilities need a person is not the runner's to start.
            return RunnerResult(
                outcome=RunnerOutcome.BLOCKED, task_id=task.id,
                state=task.state, reason="task_not_queued",
            )

        exceeded = self._tasks.budget_exceeded(task)
        if exceeded is not None:
            await self._record(
                task, TaskEventType.BUDGET_EXCEEDED, {"bound": exceeded}
            )
            return RunnerResult(
                outcome=RunnerOutcome.BUDGET_EXCEEDED, task_id=task.id,
                state=task.state, reason=exceeded,
            )
        return None

    async def _nothing_ready(self, task) -> RunnerResult:
        """No step is runnable. Either everything is done, or something is stuck."""
        states = [step.state for step in task.steps]
        # No `states and` here. `_complete` owns the emptiness check, and a
        # second copy made each one unobservable -- mutation testing showed
        # both surviving because either alone still refused the case.
        if all(state is TaskStepState.COMPLETED for state in states):
            completed = await self._complete(task)
            if completed.outcome is RunnerOutcome.TASK_COMPLETED:
                return completed

        # Blocked rather than failed. A dependent of a failed step stays
        # blocked forever, deliberately: Stage 6D has no skip-or-abort policy,
        # and inventing one would decide something a person should.
        await self._record(
            task, TaskEventType.RUNNER_BLOCKED,
            {
                "pending": sum(1 for s in states if s is TaskStepState.PENDING),
                "failed": sum(1 for s in states if s is TaskStepState.FAILED),
            },
        )
        return RunnerResult(
            outcome=RunnerOutcome.BLOCKED, task_id=task.id, state=task.state,
            reason="no_runnable_step",
        )

    # --- The claim -----------------------------------------------------------

    async def _claim(self, step: TaskStep) -> bool:
        """Take ownership of one pending step. Exactly one caller wins.

        The reminder scheduler's pattern: a conditional UPDATE naming the row
        *and the state it is being taken from*, with `rowcount` as the
        arbiter. Two concurrent runners issue the same statement and one
        matches nothing, so a step cannot be claimed twice.
        """
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            update(TaskStep)
            .where(
                TaskStep.id == step.id,
                TaskStep.state == TaskStepState.PENDING,
            )
            .values(state=TaskStepState.RUNNING, started_at=now, updated_at=now)
            .execution_options(synchronize_session="fetch")
        )
        return result.rowcount == 1

    # --- Running a claimed step ------------------------------------------------

    async def _run_claimed_step(self, task, step, binding) -> RunnerResult:
        """Create the execution, approve it if policy allows, and dispatch."""
        await self._record(
            task, TaskEventType.STEP_STARTED,
            {"step": step.step_key, "sequence": step.sequence},
        )

        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name=binding.capability,
                    arguments=dict(step.arguments or {}),
                    idempotency_key=f"task:{task.id}:{step.step_key}"[:128],
                ),
                conversation_id=task.conversation_id,
            )
        except ExecutionError as refusal:
            return await self._fail_step(
                task, step, refusal.reason, execution_id=None
            )

        step.execution_id = execution.id
        await self._session.flush()
        await self._record(
            task, TaskEventType.EXECUTION_CREATED,
            {"step": step.step_key, "execution_id": str(execution.id)},
        )

        # Who may approve this, and whether anyone has.
        #
        # Live verification in Stage 6D found the first version of this
        # wrong: it refused anything policy marked `approval_required` and
        # never looked at whether a person had since approved it, so a human
        # approval could never take effect and the step blocked forever.
        already_approved = execution.state is ExecutionState.APPROVED

        # Stage 6E. Asked of the authorization service, never of the grant
        # table: the runner has no idea what a grant is, and a structural
        # test asserts it never imports one. What comes back is an ordinary
        # `AuthorizationDecision` whose `requires_approval` a live grant may
        # have satisfied.
        decision = await self._authorization.authorize_with_grants(
            ActionProposal(
                tool_name=binding.capability,
                arguments=dict(step.arguments or {}),
                source=ActionSource.MODEL,
            ),
            grants=self._grants,
        )
        policy_allows = not decision.requires_approval

        if decision.standing_grant_id is not None:
            await self._record(
                task, TaskEventType.STANDING_GRANT_USED,
                {
                    "step": step.step_key,
                    "capability": decision.tool_name,
                    "grant_id": str(decision.standing_grant_id),
                },
            )

        if not (already_approved or policy_allows):
            # Policy says a person decides, and none has. The step goes back
            # to pending so a later invocation can pick it up once someone
            # approves it themselves -- leaving it `running` would be a step
            # nothing is working on.
            await self._release(step)
            await self._record(
                task, TaskEventType.RUNNER_BLOCKED,
                {"step": step.step_key, "reason": "awaiting_human_approval"},
            )
            return RunnerResult(
                outcome=RunnerOutcome.BLOCKED, task_id=task.id,
                step_key=step.step_key, execution_id=execution.id,
                state=task.state, reason="awaiting_human_approval",
            )

        # Spent here, and only here: past every gate, about to dispatch.
        #
        # Live verification found the first version counting an invocation
        # that blocked awaiting approval, so a two-step task recorded four
        # spent steps and an approval-gated task could exhaust its budget
        # purely by being polled. A budget must count work, not attempts.
        await self._spend(task, "max_steps")
        await self._spend(task, "max_tool_calls")

        try:
            if not already_approved:
                # Only for a capability policy already cleared. The runner
                # never approves on a person's behalf.
                await self._executions.approve(execution.id)
            await self._executions.run(execution.id)
        except ExecutionError as refusal:
            return await self._fail_step(
                task, step, refusal.reason, execution_id=execution.id
            )

        return await self._complete_step(task, step, execution.id)

    async def _complete_step(self, task, step, execution_id) -> RunnerResult:
        now = datetime.now(timezone.utc)
        step.state = TaskStepState.COMPLETED
        step.completed_at = now
        step.updated_at = now
        await self._session.flush()
        await self._record(
            task, TaskEventType.STEP_COMPLETED,
            {"step": step.step_key, "execution_id": str(execution_id)},
        )

        refreshed = await self._tasks.get_detail(task.id)
        if refreshed is not None and refreshed.steps and all(
            s.state is TaskStepState.COMPLETED for s in refreshed.steps
        ):
            await self._complete(refreshed)
            return RunnerResult(
                outcome=RunnerOutcome.TASK_COMPLETED, task_id=task.id,
                step_key=step.step_key, execution_id=execution_id,
                state=TaskState.COMPLETED,
            )

        return RunnerResult(
            outcome=RunnerOutcome.STEP_COMPLETED, task_id=task.id,
            step_key=step.step_key, execution_id=execution_id,
            state=task.state,
        )

    async def _fail_step(self, task, step, reason, execution_id) -> RunnerResult:
        now = datetime.now(timezone.utc)
        step.state = TaskStepState.FAILED
        step.completed_at = now
        step.updated_at = now
        await self._session.flush()
        await self._record(
            task, TaskEventType.STEP_FAILED,
            {"step": step.step_key, "reason": reason},
        )
        logger.info(
            "Step failed",
            # A reason code and ids. Never an argument value, never a tool's
            # output, never anything a capability returned.
            extra={"task_id": str(task.id), "reason": reason},
        )
        return RunnerResult(
            outcome=RunnerOutcome.STEP_FAILED, task_id=task.id,
            step_key=step.step_key, execution_id=execution_id,
            state=task.state, reason=reason,
        )

    async def _release(self, step) -> None:
        """Put a claimed step back, for a later invocation to take."""
        step.state = TaskStepState.PENDING
        step.started_at = None
        step.updated_at = datetime.now(timezone.utc)
        await self._session.flush()

    # --- Task completion --------------------------------------------------------

    async def _complete(self, task) -> RunnerResult:
        """Mark a task completed. The only path that may, and it needs proof.

        The proof is that every persisted step is `completed` -- read back
        from the database, not inferred from what this invocation did. A task
        with no steps is never completed here: nothing ran, so there is
        nothing to have finished.
        """
        if not task.steps or not all(
            s.state is TaskStepState.COMPLETED for s in task.steps
        ):
            return RunnerResult(
                outcome=RunnerOutcome.BLOCKED, task_id=task.id,
                state=task.state, reason="steps_incomplete",
            )

        now = datetime.now(timezone.utc)
        previous = task.state
        task.state = TaskState.COMPLETED
        task.completed_at = now
        task.updated_at = now
        await self._session.flush()
        await self._record(
            task, TaskEventType.TASK_COMPLETED,
            {"from": previous.value, "step_count": len(task.steps)},
        )
        return RunnerResult(
            outcome=RunnerOutcome.TASK_COMPLETED, task_id=task.id,
            state=TaskState.COMPLETED,
        )

    # --- Bookkeeping ---------------------------------------------------------------

    async def _spend(self, task, key: str) -> None:
        """Increment one budget counter. The first enforcement point in Mai.

        Only the two counters the application can actually measure. A bound
        nothing measures is not a bound, and recording one as though it were
        would be the fabrication this codebase exists to prevent.
        """
        spent = dict(task.spent or {})
        spent[key] = int(spent.get(key, 0)) + 1
        task.spent = spent
        await self._session.flush()

    async def _record(self, task, event_type, metadata) -> None:
        try:
            await journal.record(
                self._session, task.id, event_type, actor="system",
                metadata=metadata,
            )
        except _DB_ERRORS:  # pragma: no cover - the journal must not fail a run
            logger.error(
                "Could not record a runner event",
                extra={"task_id": str(task.id), "event": event_type.value},
            )

    async def mark_task_running(self, task_id: uuid.UUID) -> bool:
        """Move a queued task to `running`. The runner's own transition.

        Separate from `advance` so the state change is one auditable act
        rather than a side effect of the first step being claimed.
        """
        task = await self._tasks.get(task_id)
        if task is None or task.state is not TaskState.QUEUED:
            return False
        task.state = TaskState.RUNNING
        task.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self._record(
            task, TaskEventType.STATE_CHANGED,
            {"from": TaskState.QUEUED.value, "to": TaskState.RUNNING.value},
        )
        return True


__all__ = ["TaskRunner"]
