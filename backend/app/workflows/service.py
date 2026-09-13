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
from app.workflows import briefing
from app.workflows.plans import find_plan
from app.workflows.schemas import (
    StepKind,
    StepReport,
    StepStatus,
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
_REQUIRED_INTEGRATION = {
    StepKind.RESEARCH: "web_search",
    StepKind.CALENDAR: "google_calendar",
}

#: Authorization outcomes from which a step may be attempted at all.
#:
#: `FORBIDDEN` and `UNKNOWN_TOOL` are absent, and no approval adds them: an
#: approval can satisfy a requirement for approval; it cannot lift a denial.
_ATTEMPTABLE = frozenset(
    {AuthorizationStatus.ALLOWED, AuthorizationStatus.APPROVAL_REQUIRED}
)


def _step_of_kind(plan: WorkflowPlan, kind: StepKind) -> Optional[WorkflowStep]:
    """The one step of this kind, or None.

    By kind rather than by index. Stage 4F-E had a single plan shape, so
    `plan.step(2)` was always the artifact; with a second shape the artifact
    sits at 2 or 3 depending on whether research was requested, and a
    positional lookup would have written the file from the wrong step's
    arguments -- or from the synthesis step, which has no path at all.

    The composition bounds guarantee at most one step of each kind, so "the
    one step" is well defined rather than a convenient assumption.
    """
    for step in plan.steps:
        if step.kind is kind:
            return step
    return None


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
        self,
        conversation_id: uuid.UUID,
        message: str,
        intent: IntentResult,
        normalised: Optional[str] = None,
    ) -> WorkflowResult:
        """Examine one turn. Never raises; degrades to NOT_WORKFLOW.

        A pending workflow is resolved first, for the reason Stage 4F-D
        resolves a pending research proposal first: "yes" must be read against
        what was proposed, not re-matched as a fresh request.
        """
        try:
            pending = await self._pending_for(conversation_id)
            if pending is not None:
                # The original: a confirmation is its own phrase table.
                return await self._resolve(pending, message)
            return await self._maybe_propose(conversation_id, message, normalised)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Workflow handling failed; continuing as an ordinary turn",
                extra={"conversation_id": str(conversation_id)},
            )
            return WorkflowResult(outcome=WorkflowOutcome.NOT_WORKFLOW)

    # --- Turn N: propose ----------------------------------------------------

    async def _maybe_propose(
        self,
        conversation_id: uuid.UUID,
        message: str,
        normalised: Optional[str] = None,
    ) -> WorkflowResult:
        """Plan and disclose. Sends nothing, writes nothing.

        `message` is what the user wrote and is what gets stored on the plan;
        `normalised` is the repaired text the grammars read. Keeping them
        apart is what stops a stored plan claiming the user typed something
        they did not.
        """
        reading = normalised or message
        plan = find_plan(reading)
        if plan is None:
            # Stage 4H's second shape. Tried after the research-document
            # template, not instead of it: the two grammars are disjoint --
            # one needs an artifact noun, the other a meeting and a time --
            # and trying the older one first keeps its behaviour identical.
            return await self._maybe_propose_briefing(
                conversation_id, message, reading
            )

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

    # --- Stage 4H: briefing composition -------------------------------------

    async def _maybe_propose_briefing(
        self,
        conversation_id: uuid.UUID,
        message: str,
        reading: Optional[str] = None,
    ) -> WorkflowResult:
        """Recognise a briefing request and either answer it or propose it.

        Two routes, and which one is taken preserves each capability's own
        consent rule rather than inventing a third:

        * **Calendar only.** A calendar read requires no approval -- Stage
          4G.1 decided that, and argued that prompting for every calendar
          question trains people to confirm without reading. So a briefing
          with no research runs immediately, exactly as "what's on my
          calendar tomorrow?" does.

        * **Calendar and research.** Research requires consent, so the whole
          composition is put to the user first, with both operations named in
          the sentence they answer. That is Stage 4F-E's rule: one informed
          consent covering disclosed operations, never a consent for one
          thing that silently acquires another.
        """
        request = briefing.recognise(reading or message, tz=self._timezone())
        if request is None:
            return WorkflowResult(outcome=WorkflowOutcome.NOT_WORKFLOW)

        if not self._settings.EXECUTION_ENABLED:
            return WorkflowResult(
                outcome=WorkflowOutcome.DISABLED,
                reply=(
                    "I can't put a briefing together: action execution is "
                    "switched off for this deployment, so I can't read your "
                    "calendar or search the web. I can still help you think "
                    "the meeting through."
                ),
            )

        if not self._integration_available("google_calendar"):
            return WorkflowResult(
                outcome=WorkflowOutcome.NOT_CONFIGURED,
                reason="calendar_unavailable",
                reply=(
                    "I can't put a briefing together because my calendar "
                    "integration isn't available. I can still help you think "
                    "the meeting through."
                ),
            )

        if request.needs_subject:
            # They asked for research and named nothing searchable -- "research
            # the company". Guessing from the calendar event is refused on
            # purpose (see app/workflows/briefing.py), so the honest move is
            # to ask. Nothing is read and no record is created.
            return WorkflowResult(
                outcome=WorkflowOutcome.CLARIFICATION_NEEDED,
                reply=(
                    "I can check your calendar and look something up before "
                    "the meeting — what should I research?"
                ),
            )

        plan = briefing.build_plan(message, request)
        if plan is None:
            return WorkflowResult(
                outcome=WorkflowOutcome.FAILED,
                reason="plan_rejected",
                reply="I couldn't put that briefing together.",
            )

        refusal = self._unauthorized_step(plan)
        if refusal is not None:
            return WorkflowResult(
                outcome=WorkflowOutcome.FAILED,
                reason=refusal,
                reply="I can't do that: one of the steps isn't permitted.",
            )

        workflow = Workflow(
            conversation_id=conversation_id,
            kind="briefing",
            state=WorkflowState.PENDING,
            plan=plan.model_dump(mode="json"),
        )
        self._session.add(workflow)
        await self._session.flush()

        if not request.wants_research:
            # No consent needed: this is a calendar read and nothing else.
            self._transition(workflow, WorkflowState.AWAITING_APPROVAL)
            await self._session.flush()
            return await self._approve_and_run(workflow)

        if not self._integration_available("web_search"):
            # Recognised, but the research half cannot run. Say so rather
            # than proposing something that would fail on approval.
            await self._session.delete(workflow)
            await self._session.flush()
            return WorkflowResult(
                outcome=WorkflowOutcome.NOT_CONFIGURED,
                reason="search_unavailable",
                reply=(
                    "I can check your calendar, but no search provider is "
                    "configured, so I can't research anything for the "
                    "briefing. Ask me what's on your calendar and I'll tell "
                    "you."
                ),
            )

        self._transition(workflow, WorkflowState.AWAITING_APPROVAL)
        await self._session.flush()

        research_step = _step_of_kind(plan, StepKind.RESEARCH)
        query = str((research_step.arguments or {}).get("query", ""))
        artifact_step = _step_of_kind(plan, StepKind.ARTIFACT)

        logger.info(
            "Briefing proposed",
            extra={
                "workflow_id": str(workflow.id),
                "conversation_id": str(conversation_id),
                # Lengths and counts. A meeting subject can name a client, an
                # employer or a diagnosis, and Stage 3D's rule is that such
                # text does not reach INFO.
                "query_chars": len(query),
                "steps": len(plan.steps),
            },
        )

        lines = [
            "Here is what I would do:",
            "",
            f"1. Read your calendar for {request.window_label}",
            f'2. Search the web for: "{query}"',
        ]
        if artifact_step is not None:
            path = str((artifact_step.arguments or {}).get("path", ""))
            lines.append(f"3. Write the briefing to: {path}")
        lines += [
            "",
            "The calendar read is read-only and the search sends that query "
            "to an external search provider. Reply \"yes\" to go ahead, or "
            "anything else to skip it.",
        ]

        return WorkflowResult(
            outcome=WorkflowOutcome.AWAITING_CONFIRMATION,
            workflow_id=workflow.id,
            reply="\n".join(lines),
        )

    async def _run_briefing(
        self, workflow: Workflow, plan: WorkflowPlan
    ) -> WorkflowResult:
        """Run the calendar step, then the research step if there is one.

        Every outcome below is read from an execution record. Nothing here
        infers success from the absence of an error, and a step that did not
        run is reported as not having run -- which is what stops a briefing
        claiming research it never did.
        """
        reports: List[StepReport] = []

        calendar_step = _step_of_kind(plan, StepKind.CALENDAR)
        research_step = _step_of_kind(plan, StepKind.RESEARCH)
        synthesis_step = _step_of_kind(plan, StepKind.SYNTHESISE)
        artifact_step = _step_of_kind(plan, StepKind.ARTIFACT)

        calendar_block, event_count, calendar_status = await self._run_calendar(
            workflow, calendar_step
        )
        reports.append(
            StepReport(
                index=calendar_step.index,
                kind=StepKind.CALENDAR,
                status=calendar_status,
            )
        )

        research_block, result_count = "", 0
        research_status = StepStatus.NOT_STARTED
        if research_step is not None:
            if calendar_status is not StepStatus.SUCCEEDED:
                # The dependency did not succeed. Skipped, not failed: nothing
                # was attempted, and saying "the search failed" would be a
                # false claim about an external service.
                research_status = StepStatus.SKIPPED
            else:
                research_block, result_count, research_status = (
                    await self._run_research_step(workflow, research_step)
                )
            reports.append(
                StepReport(
                    index=research_step.index,
                    kind=StepKind.RESEARCH,
                    status=research_status,
                )
            )

        if calendar_status is not StepStatus.SUCCEEDED and not research_block:
            # Nothing was retrieved at all. There is no briefing to give.
            await self._finish(workflow, WorkflowState.FAILED, "calendar_failed")
            reports.append(
                StepReport(index=synthesis_step.index, kind=StepKind.SYNTHESISE,
                           status=StepStatus.SKIPPED)
            )
            return WorkflowResult(
                outcome=WorkflowOutcome.FAILED,
                workflow_id=workflow.id,
                reason="calendar_failed",
                steps=tuple(reports),
                reply=(
                    "I couldn't read your calendar, so I haven't put a "
                    "briefing together. Nothing was retrieved."
                ),
            )

        partial = (
            research_step is not None
            and research_status is not StepStatus.SUCCEEDED
        )

        if artifact_step is None:
            # No later phase will move this workflow, so it is finished here.
            # A partial composition is recorded as SUCCEEDED at the workflow
            # level -- every step that was going to run has run -- while the
            # *result* stays PARTIAL, which is what the user is told. The two
            # answer different questions: whether the workflow is over, and
            # whether it got everything it went for.
            await self._finish(workflow, WorkflowState.SUCCEEDED, None)

        return WorkflowResult(
            outcome=(
                WorkflowOutcome.PARTIAL if partial else WorkflowOutcome.COMPLETED
            ),
            workflow_id=workflow.id,
            calendar_block=calendar_block,
            calendar_event_count=event_count,
            calendar_read=calendar_status is StepStatus.SUCCEEDED,
            research_block=research_block,
            result_count=result_count,
            researched=research_status is StepStatus.SUCCEEDED,
            research_attempted=research_step is not None,
            artifact_requested=artifact_step is not None,
            artifact_path=(
                str((artifact_step.arguments or {}).get("path", ""))
                if artifact_step is not None else ""
            ),
            steps=tuple(reports),
        )

    async def _run_calendar(
        self, workflow: Workflow, step: WorkflowStep
    ) -> Tuple[str, int, StepStatus]:
        """Read the window through the ordinary execution path.

        No second calendar client and no direct integration call: this creates
        an execution and lets the Stage 4E dispatcher reach the integration,
        so Stage 4C authorization, the network policy and the audit journal
        all apply exactly as they do to a bare calendar question.
        """
        arguments = dict(step.arguments or {})
        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name="calendar_list_events",
                    arguments=arguments,
                    idempotency_key=f"wf:{workflow.id}:{step.index}"[:128],
                ),
                conversation_id=workflow.conversation_id,
                workflow_id=workflow.id,
                step_index=step.index,
            )
            await self._executions.approve(execution.id)
            execution, outcome = await self._executions.run_returning_outcome(
                execution.id
            )
        except ExecutionError as refusal:
            logger.info(
                "Briefing calendar step refused",
                extra={"workflow_id": str(workflow.id), "reason": refusal.reason},
            )
            return "", 0, StepStatus.REFUSED

        if execution.state is not ExecutionState.SUCCEEDED or outcome is None:
            return "", 0, StepStatus.FAILED

        block, count = self._rendered_calendar(outcome)
        if not block:
            return "", 0, StepStatus.FAILED
        return block, count, StepStatus.SUCCEEDED

    async def _run_research_step(
        self, workflow: Workflow, step: WorkflowStep
    ) -> Tuple[str, int, StepStatus]:
        """The research step of a briefing. Same path as Stage 4F-E's."""
        query = str((step.arguments or {}).get("query", ""))
        try:
            execution = await self._executions.create(
                ExecutionRequest(
                    tool_name="web_search",
                    arguments={"query": query},
                    idempotency_key=f"wf:{workflow.id}:{step.index}"[:128],
                ),
                conversation_id=workflow.conversation_id,
                workflow_id=workflow.id,
                step_index=step.index,
            )
            await self._executions.approve(execution.id)
            execution, outcome = await self._executions.run_returning_outcome(
                execution.id
            )
        except ExecutionError as refusal:
            logger.info(
                "Briefing research step refused",
                extra={"workflow_id": str(workflow.id), "reason": refusal.reason},
            )
            return "", 0, StepStatus.REFUSED

        if execution.state is not ExecutionState.SUCCEEDED or outcome is None:
            return "", 0, StepStatus.FAILED

        block, count = ResearchService._rendered_results(outcome)
        if not block:
            return "", 0, StepStatus.FAILED
        return block, count, StepStatus.SUCCEEDED

    @staticmethod
    def _rendered_calendar(outcome) -> Tuple[str, int]:
        """Pull the rendered window out of the tool's outcome.

        The same extraction the calendar service uses: the content arrives as
        the `ExternalData` the integration built, already minimised, already
        flattened, already classified. Re-rendering it here would be a second
        place for the labelling to be forgotten.
        """
        if outcome is None:
            return "", 0
        external = (outcome.data or {}).get("external")
        if not isinstance(external, dict):
            return "", 0
        content = external.get("content")
        if not isinstance(content, str) or not content.strip():
            return "", 0
        count = sum(1 for line in content.split("\n") if line.startswith("["))
        return content, count

    def _timezone(self):
        """The zone a briefing's window is measured in. Settings, never a host."""
        from zoneinfo import ZoneInfo

        # A direct attribute read, not `getattr`. `MAI_TIMEZONE` is a
        # declared setting with a default, so the dynamic form bought nothing
        # -- and `getattr` is the string-to-code primitive that a layer
        # deciding *what to run* should not contain at all. The structural
        # audit asserts its absence here.
        name = self._settings.MAI_TIMEZONE or "UTC"
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            logger.warning("Unknown MAI_TIMEZONE; using UTC")
            return timezone.utc

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

        if workflow.kind == "briefing":
            # A second shape, dispatched on the stored kind rather than on
            # the plan's contents: the kind is what the application recorded
            # when it planned, and reading the shape back out of the steps
            # would make a tampered plan able to choose its own runner.
            return await self._run_briefing(workflow, plan)

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
            # This shape always writes a file, so the flag is unconditional
            # here. It is what tells the chat layer to call `finalise` at all.
            artifact_requested=True,
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
        artifact_step = _step_of_kind(plan, StepKind.ARTIFACT)
        if artifact_step is None:
            # Nothing to write, so nothing for this method to decide.
            #
            # An earlier version returned COMPLETED here, which overwrote the
            # outcome the run phase had already established: a briefing whose
            # search had failed came back PARTIAL, reached this line, and was
            # reported to the user as a completed composition. `finalise`
            # exists to write an artifact and must not be the thing that
            # decides whether a composition succeeded.
            #
            # The caller no longer reaches this for an artifact-free plan; it
            # stays as a guard, and it invents nothing.
            return WorkflowResult(
                outcome=WorkflowOutcome.NOT_WORKFLOW, workflow_id=workflow.id
            )
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
                    idempotency_key=f"wf:{workflow.id}:{artifact_step.index}"[:128],
                ),
                conversation_id=workflow.conversation_id,
                workflow_id=workflow.id,
                step_index=artifact_step.index,
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
                StepReport(index=artifact_step.index, kind=StepKind.ARTIFACT,
                           status=StepStatus.FAILED, detail=refusal.reason)
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
                artifact_requested=True,
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
                index=artifact_step.index, kind=StepKind.ARTIFACT,
                status=StepStatus.SUCCEEDED if written else StepStatus.FAILED,
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
            artifact_requested=True,
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
                StepReport(index=0, kind=StepKind.RESEARCH,
                           status=StepStatus.FAILED, detail=refusal.reason)
            )
            reports.append(
                StepReport(index=1, kind=StepKind.SYNTHESISE,
                           status=StepStatus.SKIPPED)
            )
            reports.append(
                StepReport(index=2, kind=StepKind.ARTIFACT,
                           status=StepStatus.SKIPPED)
            )
            return None, reports

        if execution.state is not ExecutionState.SUCCEEDED or outcome is None:
            reports.append(
                StepReport(index=0, kind=StepKind.RESEARCH,
                           status=StepStatus.FAILED)
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
                StepReport(index=0, kind=StepKind.RESEARCH,
                           status=StepStatus.FAILED, detail="no_results")
            )
            return None, reports

        reports.append(
            StepReport(index=0, kind=StepKind.RESEARCH,
                       status=StepStatus.SUCCEEDED)
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
        research_step = _step_of_kind(plan, StepKind.RESEARCH)
        query = str((research_step.arguments or {}).get("query", "")) if research_step else ""
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
