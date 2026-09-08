"""Composing existing capabilities, with security authoritative at each edge.

The whole of Stage 4F-E is this file plus a plan template, and the reason it
is small is that it adds no new authority. A workflow step **is** an
`Execution`, so every gate Stage 4E built applies to it unchanged:
authorization is asked of Stage 4C at dispatch, approval is bound to a payload
fingerprint, the claim is a conditional UPDATE, and the journal is append-only.
This module decides *what to attempt and in what order*. It decides nothing
about what is permitted.

Two consent points would be one too many, and none would be one too few
--------------------------------------------------------------------------

The user is shown both operations before either runs:

    turn N      "research X and write me a summary"
                -> "I'll search the web for X, then save the summary to
                    x.txt. Shall I?"
                -> nothing sent, nothing written

    turn N+1    "yes"
                -> research runs, synthesis happens, the file is written

One informed consent covering two disclosed operations, rather than a consent
for research that silently acquires a filesystem write. The distinction the
specification draws is between *disclosed* and *unrelated*, and the artifact
is disclosed -- with its exact path -- in the sentence the user answers.

What the approval binds, and what it cannot
-------------------------------------------

`plan_fingerprint` covers the workflow id, every step's position, kind, tool
and approval-time arguments. The artifact's **content** is not in it and could
not be: it is synthesised from research that has not happened when the user
answers. Its **path** is, and is re-checked against the approved plan before
the file is written -- so nothing downstream, and in particular nothing a
search result says, can move the file. Content is constrained by workspace
confinement and by being inert data. This is recorded in the architecture note
as a known property rather than left for a reader to find.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.errors import ExecutionError
from app.execution.models import Execution
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState
from app.intent.schemas import IntentResult
from app.research.confirmation import Confirmation, interpret
from app.research.service import ResearchService
from app.tools.schemas import AuthorizationStatus
from app.workflows.limits import MAX_ARTIFACT_CONTENT_CHARS
from app.workflows.models import Workflow
from app.workflows.plans import find_plan
from app.workflows.schemas import (
    StepKind,
    StepReport,
    WorkflowOutcome,
    WorkflowPlan,
    WorkflowResult,
    WorkflowStep,
    plan_fingerprint,
)
from app.workflows.states import WorkflowState, can_transition

logger = get_logger(__name__)

#: How long an approved workflow stays runnable.
#:
#: Shorter than an execution approval, because a workflow's second half runs
#: on the same turn as its first: if the run has not started within a few
#: minutes of consent, something has gone wrong rather than slowly.
APPROVAL_TTL_SECONDS = 300

#: Every integration a workflow step may require, by step kind.
_REQUIRED_INTEGRATION = {StepKind.RESEARCH: "web_search"}

#: Authorization outcomes from which a step may be attempted at all.
#:
#: `FORBIDDEN` and `UNKNOWN_TOOL` are absent, and no approval adds them: an
#: approval can satisfy a requirement for approval; it cannot lift a denial.
_ATTEMPTABLE = frozenset(
    {AuthorizationStatus.ALLOWED, AuthorizationStatus.APPROVAL_REQUIRED}
)


class WorkflowService:
    """Plans, proposes and runs the one composite workflow Mai supports."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Optional[Settings] = None,
        executions: Optional[ExecutionService] = None,
        integrations=None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._executions = executions or ExecutionService(
            session, settings=self._settings
        )
        self._integrations = integrations

    # --- One turn -----------------------------------------------------------

    async def handle(
        self, conversation_id: uuid.UUID, message: str, intent: IntentResult
    ) -> WorkflowResult:
        """Examine one turn. Never raises; degrades to NOT_WORKFLOW.

        A pending workflow is resolved first, for the reason Stage 4F-D
        resolves a pending research proposal first: "yes" must be read against
        what was proposed, not re-matched as a fresh request.
        """
        try:
            pending = await self._pending_for(conversation_id)
            if pending is not None:
                return await self._resolve(pending, message)
            return await self._maybe_propose(conversation_id, message)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Workflow handling failed; continuing as an ordinary turn",
                extra={"conversation_id": str(conversation_id)},
            )
            return WorkflowResult(outcome=WorkflowOutcome.NOT_WORKFLOW)

    # --- Turn N: propose ----------------------------------------------------

    async def _maybe_propose(
        self, conversation_id: uuid.UUID, message: str
    ) -> WorkflowResult:
        """Plan and disclose. Sends nothing, writes nothing."""
        plan = find_plan(message)
        if plan is None:
            return WorkflowResult(outcome=WorkflowOutcome.NOT_WORKFLOW)

        if not self._settings.EXECUTION_ENABLED:
            return WorkflowResult(
                outcome=WorkflowOutcome.DISABLED,
                reply=(
                    "I can't run that: action execution is switched off for "
                    "this deployment, so I can neither search the web nor "
                    "write a file. I can still help you think it through."
                ),
            )

        if not self._integration_available("web_search"):
            return WorkflowResult(
                outcome=WorkflowOutcome.NOT_CONFIGURED,
                reply=(
                    "I can't research that because no search provider is "
                    "configured for this instance. I can still help you think "
                    "the question through."
                ),
            )

        refusal = self._unauthorized_step(plan)
        if refusal is not None:
            # Refused before anything is recorded. Proposing a workflow whose
            # steps can never run would produce a grant that cannot be
            # honoured -- the same reason Stage 4E refuses to approve an
            # unimplemented tool.
            return WorkflowResult(
                outcome=WorkflowOutcome.FAILED,
                reason=refusal,
                reply="I can't do that: one of the steps isn't permitted.",
            )

        workflow = Workflow(
            conversation_id=conversation_id,
            kind="research_document",
            state=WorkflowState.PENDING,
            plan=plan.model_dump(mode="json"),
        )
        self._session.add(workflow)
        await self._session.flush()

        self._transition(workflow, WorkflowState.AWAITING_APPROVAL)
        await self._session.flush()

        query = plan.step(0).arguments.get("query", "")
        path = plan.step(2).arguments.get("path", "")

        logger.info(
            "Workflow proposed",
            extra={
                "workflow_id": str(workflow.id),
                "conversation_id": str(conversation_id),
                # Lengths, not contents. A research query can name a person or
                # a diagnosis, and Stage 3D's rule is that such data does not
                # reach INFO.
                "query_chars": len(query),
                "steps": len(plan.steps),
            },
        )

        return WorkflowResult(
            outcome=WorkflowOutcome.AWAITING_CONFIRMATION,
            workflow_id=workflow.id,
            reply=(
                f'Here is what I would do:\n\n'
                f'1. Search the web for: "{query}"\n'
                f'2. Write a summary of what I find to: {path}\n\n'
                f"The search sends that query to an external search provider, "
                f"and the file is written inside my workspace. Reply \"yes\" "
                f"to go ahead, or anything else to skip it."
            ),
        )

    # --- Turn N+1: resolve --------------------------------------------------

    async def _resolve(self, workflow: Workflow, message: str) -> WorkflowResult:
        """Apply the user's reply. Deterministic; no model is consulted."""
        decision = interpret(message)

        if decision is Confirmation.DECLINED:
            await self._cancel(workflow, "declined")
            return WorkflowResult(
                outcome=WorkflowOutcome.DECLINED,
                workflow_id=workflow.id,
                reply="Understood — I won't run that.",
            )

        if decision is Confirmation.UNRELATED:
            await self._cancel(workflow, "abandoned")
            return WorkflowResult(
                outcome=WorkflowOutcome.ABANDONED, workflow_id=workflow.id
            )

        return await self._approve_and_run(workflow)

    async def _approve_and_run(self, workflow: Workflow) -> WorkflowResult:
        """Grant approval over this exact plan, then run the research half.

        The artifact is written later, in `finalise`, once the synthesis it
        contains exists. Approval covers both -- the user was shown both --
        but the file is not created before there is something to put in it.
        """
        plan = self._plan_of(workflow)
        now = datetime.now(timezone.utc)

        self._transition(workflow, WorkflowState.APPROVED)
        workflow.approved_at = now
        workflow.approved_fingerprint = plan_fingerprint(workflow.id, plan)
        workflow.approval_expires_at = now + timedelta(seconds=APPROVAL_TTL_SECONDS)
        await self._session.flush()

        self._transition(workflow, WorkflowState.RUNNING)
        workflow.started_at = now
        await self._session.flush()

        research_step = plan.step(0)
        outcome, reports = await self._run_research(workflow, research_step)

        if outcome is None:
            await self._finish(workflow, WorkflowState.FAILED, "research_failed")
            return WorkflowResult(
                outcome=WorkflowOutcome.FAILED,
                workflow_id=workflow.id,
                reason="research_failed",
                steps=tuple(reports),
                reply=(
                    "I couldn't complete the web search, so I haven't written "
                    "anything. Nothing was retrieved."
                ),
            )

        block, count = outcome
        return WorkflowResult(
            # The planned path travels with the research result so the chat
            # layer can tell the model where its reply will be saved. It is
            # the approved path, not a new decision -- `finalise` reads the
            # same value from the same stored plan.
            artifact_path=str((plan.step(2).arguments or {}).get("path", "")),
            # Not COMPLETED yet: the artifact has not been written. The chat
            # layer synthesises, then calls `finalise`, and only the executor's
            # answer decides whether this becomes COMPLETED or PARTIAL.
            outcome=WorkflowOutcome.COMPLETED,
            workflow_id=workflow.id,
            research_block=block,
            result_count=count,
            steps=tuple(reports),
        )

    # --- After synthesis ----------------------------------------------------

    async def finalise(
        self, workflow_id: uuid.UUID, synthesis: str
    ) -> WorkflowResult:
        """Write the artifact. Called once the synthesis exists.

        `synthesis` is model output, and is treated as such: it becomes the
        *body* of a text file and nothing else. It cannot choose the path, and
        it is bounded well below the executor's own limit.
        """
        workflow = await self._session.get(Workflow, workflow_id)
        if workflow is None:
            return WorkflowResult(outcome=WorkflowOutcome.NOT_WORKFLOW)

        if workflow.state is not WorkflowState.RUNNING:
            # A workflow writes its artifact exactly once, on the turn it is
            # running. Anything else -- already succeeded, cancelled, never
            # started, or a state something else moved it to -- is refused
            # without transitioning, so a second call cannot produce a second
            # file and a tampered state cannot produce a first one.
            logger.info(
                "Workflow finalise refused: not running",
                extra={"workflow_id": str(workflow.id), "state": workflow.state.value},
            )
            return WorkflowResult(
                outcome=WorkflowOutcome.PARTIAL,
                workflow_id=workflow.id,
                reason="workflow_not_running",
                artifact_written=False,
            )

        plan = self._plan_of(workflow)
        artifact_step = plan.step(2)
        reports: List[StepReport] = []

        if not self._approval_still_valid(workflow, plan):
            await self._finish(workflow, WorkflowState.FAILED, "approval_invalid")
            return WorkflowResult(
                outcome=WorkflowOutcome.PARTIAL,
                workflow_id=workflow.id,
                reason="approval_invalid",
                artifact_written=False,
            )

        path = str((artifact_step.arguments or {}).get("path", ""))
        content = self._artifact_body(synthesis, plan)

        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name="create_text_file",
                    arguments={"path": path, "content": content, "overwrite": True},
                    idempotency_key=f"wf:{workflow.id}:2"[:128],
                ),
                conversation_id=workflow.conversation_id,
                workflow_id=workflow.id,
                step_index=2,
            )
            # No path re-check here, deliberately. An earlier version
            # compared the created execution's path against `path` -- but both
            # come from the same local variable, so the comparison could never
            # fail. Mutation testing found it: deleting the check changed
            # nothing, because it was unreachable.
            #
            # The real guard is `_approval_still_valid` above, which
            # recomputes the plan fingerprint and refuses if the stored plan
            # has moved since the user approved it. That one is load-bearing:
            # removing it makes a tampered path executable, and a test proves
            # it.
            await self._executions.approve(execution.id)
            execution, _ = await self._executions.run_returning_outcome(execution.id)
        except ExecutionError as refusal:
            reports.append(
                StepReport(index=2, kind=StepKind.ARTIFACT, status="failed",
                           detail=refusal.reason)
            )
            await self._finish(workflow, WorkflowState.FAILED, refusal.reason)
            logger.info(
                "Workflow artifact step refused",
                extra={"workflow_id": str(workflow.id), "reason": refusal.reason},
            )
            return WorkflowResult(
                outcome=WorkflowOutcome.PARTIAL,
                workflow_id=workflow.id,
                reason=refusal.reason,
                artifact_path=path,
                artifact_written=False,
                steps=tuple(reports),
            )

        # Written only if the executor said so. A model predicting success is
        # not success -- this reads the execution record, which the dispatcher
        # set.
        written = execution.state is ExecutionState.SUCCEEDED
        reports.append(
            StepReport(
                index=2, kind=StepKind.ARTIFACT,
                status="succeeded" if written else "failed",
            )
        )

        await self._finish(
            workflow,
            WorkflowState.SUCCEEDED if written else WorkflowState.FAILED,
            None if written else "artifact_failed",
        )

        return WorkflowResult(
            outcome=WorkflowOutcome.COMPLETED if written else WorkflowOutcome.PARTIAL,
            workflow_id=workflow.id,
            artifact_path=path if written else "",
            artifact_written=written,
            steps=tuple(reports),
        )

    # --- Steps --------------------------------------------------------------

    async def _run_research(
        self, workflow: Workflow, step: WorkflowStep
    ) -> Tuple[Optional[Tuple[str, int]], List[StepReport]]:
        """Run the research step through the ordinary execution path.

        No second Tavily client and no direct integration call: this creates
        an execution, approves it, and lets the Stage 4E dispatcher reach
        `WebSearchIntegration` -> `SecureHttpClient` -> `NetworkPolicy`.
        """
        reports: List[StepReport] = []
        query = str((step.arguments or {}).get("query", ""))

        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name="web_search",
                    arguments={"query": query},
                    idempotency_key=f"wf:{workflow.id}:0"[:128],
                ),
                conversation_id=workflow.conversation_id,
                workflow_id=workflow.id,
                step_index=0,
            )
            await self._executions.approve(execution.id)
            execution, outcome = await self._executions.run_returning_outcome(
                execution.id
            )
        except ExecutionError as refusal:
            reports.append(
                StepReport(index=0, kind=StepKind.RESEARCH, status="failed",
                           detail=refusal.reason)
            )
            reports.append(
                StepReport(index=1, kind=StepKind.SYNTHESISE, status="skipped")
            )
            reports.append(
                StepReport(index=2, kind=StepKind.ARTIFACT, status="skipped")
            )
            return None, reports

        if execution.state is not ExecutionState.SUCCEEDED or outcome is None:
            reports.append(
                StepReport(index=0, kind=StepKind.RESEARCH, status="failed")
            )
            return None, reports

        # Reused from the research layer rather than re-derived. The first
        # version of this reached for `outcome.data.content`, but `data` is a
        # dict whose `external` key holds the rendered block -- so it silently
        # produced an empty string, and the workflow reported success while
        # handing synthesis nothing at all. Live verification caught it.
        #
        # Extracting it in one place is also the same argument the research
        # layer makes about rendering: a second implementation is a second
        # place for the untrusted labelling to be lost.
        block, count = ResearchService._rendered_results(outcome)

        if not block:
            # The search ran and returned nothing readable. Writing a summary
            # of nothing would be a document asserting things no source said,
            # so this is a failure rather than an empty success.
            reports.append(
                StepReport(index=0, kind=StepKind.RESEARCH, status="failed",
                           detail="no_results")
            )
            return None, reports

        reports.append(
            StepReport(index=0, kind=StepKind.RESEARCH, status="succeeded")
        )
        return (block, count), reports

    # --- Internals ----------------------------------------------------------

    def _artifact_body(self, synthesis: str, plan: WorkflowPlan) -> str:
        """The file's contents: a provenance header, then the synthesis.

        The header exists because of what happens *later*. This text is
        derived from web pages, and once it is a file it is ordinary file
        content -- if it is read back through `read_text_file` it arrives with
        no untrusted marking at all. A line saying where it came from is the
        cheapest way to keep that visible to whoever, or whatever, reads it
        next.
        """
        query = str((plan.step(0).arguments or {}).get("query", ""))
        header = (
            "Summary written by Mai from web search results.\n"
            f"Search query: {query}\n"
            "The content below is derived from external web sources and has "
            "not been independently verified.\n"
            f"{'-' * 70}\n\n"
        )
        return (header + (synthesis or ""))[:MAX_ARTIFACT_CONTENT_CHARS]

    def _approval_still_valid(
        self, workflow: Workflow, plan: WorkflowPlan
    ) -> bool:
        """Whether the approval covers the plan as it stands now.

        Recomputed rather than remembered. If the stored plan changed after
        approval -- a different path, a different tool, an added step -- the
        fingerprint no longer matches and the approval does not apply.
        """
        if workflow.approved_fingerprint is None:
            return False
        if workflow.approval_expires_at is None:
            return False

        expires = workflow.approval_expires_at
        if expires.tzinfo is None:
            # SQLite returns naive datetimes and PostgreSQL aware ones.
            # Comparing the two raises, and an exception inside an expiry
            # check must not be the thing that decides whether something runs.
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires:
            return False

        return plan_fingerprint(workflow.id, plan) == workflow.approved_fingerprint

    def _unauthorized_step(self, plan: WorkflowPlan) -> Optional[str]:
        """The first step Stage 4C will not permit, as a reason code.

        Asked about the **tool**, not about a payload. The artifact step's
        arguments are deliberately incomplete at planning time -- its content
        is synthesised later -- so running them through full argument
        validation here reports `FORBIDDEN` for a missing field rather than
        for anything about permission. That is a wrong answer to the question
        being asked, and it refused every workflow before this was fixed.

        The complete payload *is* validated, by the dispatcher, at the moment
        each step runs. This is the earlier and weaker of the two checks: it
        exists so a workflow whose tools can never be permitted is refused
        before it is recorded, rather than after the user approves it.
        """
        from app.tools import policy
        from app.tools.registry import get_registry

        registry = get_registry()
        for step in plan.executable_steps:
            name = registry.canonical(step.tool_name or "")
            status, _ = policy.evaluate(registry.definition(name), name)
            if status not in _ATTEMPTABLE:
                return f"step_{step.index}_{status.value}"
        return None

    def _integration_available(self, name: str) -> bool:
        registry = self._integrations
        if registry is None:
            from app.integrations.registry import get_integration_registry

            registry = get_integration_registry()

        integration = registry.get(name)
        return bool(integration is not None and integration.available)

    def _plan_of(self, workflow: Workflow) -> WorkflowPlan:
        """Rebuild the plan from storage, validating it again on the way in.

        Re-validated rather than trusted: the row could have been edited by
        something other than this service, and a plan that no longer satisfies
        its own invariants must not become executable.
        """
        return WorkflowPlan.model_validate(workflow.plan or {})

    async def _pending_for(
        self, conversation_id: uuid.UUID
    ) -> Optional[Workflow]:
        """The workflow this conversation is waiting on, if any.

        Only `AWAITING_APPROVAL`, only this conversation, only the most
        recent. A workflow proposed elsewhere can never be confirmed here.
        """
        result = await self._session.execute(
            select(Workflow)
            .where(
                Workflow.conversation_id == conversation_id,
                Workflow.state == WorkflowState.AWAITING_APPROVAL,
            )
            .order_by(Workflow.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    def _transition(self, workflow: Workflow, target: WorkflowState) -> None:
        """One table decides every legal move. See `states.py`."""
        if not can_transition(workflow.state, target):
            raise ExecutionError(
                reason="invalid_workflow_transition",
                detail=f"{workflow.state.value} -> {target.value}",
            )
        workflow.state = target
        workflow.updated_at = datetime.now(timezone.utc)

    async def _cancel(self, workflow: Workflow, reason: str) -> None:
        self._transition(workflow, WorkflowState.CANCELLED)
        workflow.completed_at = datetime.now(timezone.utc)
        workflow.error_code = reason
        await self._session.flush()

    async def _finish(
        self, workflow: Workflow, state: WorkflowState, reason: Optional[str]
    ) -> None:
        self._transition(workflow, state)
        workflow.completed_at = datetime.now(timezone.utc)
        workflow.error_code = reason
        await self._session.flush()


__all__ = ["APPROVAL_TTL_SECONDS", "WorkflowService"]
