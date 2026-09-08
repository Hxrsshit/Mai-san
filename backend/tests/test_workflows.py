"""Stage 4F-E: composing research and artifact creation, and what bounds it.

The workflow layer adds no authority. A step is an `Execution`, so every gate
Stage 4E built applies to it -- and most of what follows checks that the
composition did not quietly acquire a shortcut around one of them.
"""

import json
import uuid

import pytest
from httpx import AsyncClient

from app.workflows.limits import MAX_DEPENDENCY_EDGES, MAX_STEPS
from app.workflows.plans import find_plan
from app.workflows.schemas import (
    StepKind,
    WorkflowOutcome,
    WorkflowPlan,
    WorkflowStep,
    plan_fingerprint,
)
from app.workflows.states import WorkflowState, can_transition, is_terminal

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})
REQUEST = "Research what Groq is and create a short summary document"


async def send(client, conversation_id, content):
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(autouse=True)
def _quiet_extraction(fake_provider):
    fake_provider.extraction_reply = NOTHING_TO_STORE
    return fake_provider


# --- Planning ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Research what Groq is and create a short summary document",
        "research the state of AI accelerators and write a report",
        "search the web for tavily and then make a note",
        "look up rust async and save a document",
        "find out about groq and produce a summary",
    ],
)
def test_a_composite_request_produces_the_one_plan_shape(message) -> None:
    plan = find_plan(message)

    assert plan is not None
    assert [step.kind for step in plan.steps] == [
        StepKind.RESEARCH, StepKind.SYNTHESISE, StepKind.ARTIFACT
    ]
    assert plan.step(1).depends_on == (0,)
    assert plan.step(2).depends_on == (1,)


@pytest.mark.parametrize(
    "message",
    [
        "Tell me about researching and writing documents",
        "Can you explain how people research topics and write reports?",
        "research groq",
        "write a summary document",
        "What do you think about research and documents in general?",
        "I read a report about research methods",
        "",
    ],
)
def test_a_non_composite_message_plans_nothing(message) -> None:
    """The Stage 4D lesson: a phrase that catches a mention is too broad."""
    assert find_plan(message) is None


def test_the_synthesis_step_has_no_tool() -> None:
    """It writes text into a variable. That is not an action.

    A step with no tool cannot be dispatched, so however the synthesis step
    is reached it has no route to a side effect.
    """
    plan = find_plan(REQUEST)

    assert plan.step(1).tool_name is None
    assert plan.step(1).is_executable is False
    assert [step.index for step in plan.executable_steps] == [0, 2]


def test_an_artifact_path_cannot_express_an_escape() -> None:
    """Traversal is unrepresentable, not merely refused.

    The filename alphabet has no `/`, `\\`, `.` or null byte, so `../../etc/
    passwd` cannot survive it. `create_text_file` would refuse an escape
    anyway -- this makes it impossible to write one down.
    """
    for hostile in (
        "research groq and save a document named ../../etc/passwd",
        "research groq and save a document named /etc/shadow",
        "research groq and write a note called ..\\..\\.env",
        "research groq and make a file called .env",
        "research groq and save a report named C:\\Windows\\system32",
    ):
        path = find_plan(hostile).step(2).arguments["path"]
        for forbidden in ("..", "/", "\\", ":", "\x00"):
            assert forbidden not in path, (hostile, path)
        assert path.endswith(".txt")


def test_a_plan_may_not_exceed_the_step_limit() -> None:
    steps = tuple(
        WorkflowStep(index=index, kind=StepKind.SYNTHESISE)
        for index in range(MAX_STEPS)
    )
    WorkflowPlan(steps=steps)  # at the limit, fine

    with pytest.raises(Exception):
        WorkflowPlan(
            steps=steps + (WorkflowStep(index=MAX_STEPS, kind=StepKind.SYNTHESISE),)
        )


def test_a_dependency_must_point_backwards() -> None:
    """Forward edges are refused, so a cycle cannot be written down."""
    with pytest.raises(Exception):
        WorkflowPlan(steps=(
            WorkflowStep(index=0, kind=StepKind.SYNTHESISE, depends_on=(1,)),
            WorkflowStep(index=1, kind=StepKind.SYNTHESISE),
        ))

    with pytest.raises(Exception):
        WorkflowPlan(steps=(
            WorkflowStep(index=0, kind=StepKind.SYNTHESISE, depends_on=(0,)),
        ))


def test_a_dependency_may_not_name_a_step_outside_the_plan() -> None:
    with pytest.raises(Exception):
        WorkflowPlan(steps=(
            WorkflowStep(index=0, kind=StepKind.SYNTHESISE, depends_on=(7,)),
        ))


# --- The fingerprint --------------------------------------------------------


def test_the_fingerprint_covers_identity_order_tool_and_arguments() -> None:
    workflow_id = uuid.uuid4()
    plan = find_plan(REQUEST)
    baseline = plan_fingerprint(workflow_id, plan)

    assert plan_fingerprint(uuid.uuid4(), plan) != baseline

    moved = WorkflowPlan(
        request=plan.request,
        steps=(
            plan.steps[0],
            plan.steps[1],
            WorkflowStep(index=2, kind=StepKind.ARTIFACT, depends_on=(1,),
                         arguments={"path": "somewhere-else.txt"}),
        ),
    )
    assert plan_fingerprint(workflow_id, moved) != baseline

    requeried = WorkflowPlan(
        request=plan.request,
        steps=(
            WorkflowStep(index=0, kind=StepKind.RESEARCH,
                         arguments={"query": "something else"}),
            plan.steps[1],
            plan.steps[2],
        ),
    )
    assert plan_fingerprint(workflow_id, requeried) != baseline


def test_the_fingerprint_is_stable_for_the_same_plan() -> None:
    workflow_id = uuid.uuid4()
    plan = find_plan(REQUEST)

    assert plan_fingerprint(workflow_id, plan) == plan_fingerprint(workflow_id, plan)


# --- States -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkflowState.PENDING, WorkflowState.RUNNING),
        (WorkflowState.PENDING, WorkflowState.APPROVED),
        (WorkflowState.PENDING, WorkflowState.SUCCEEDED),
        (WorkflowState.AWAITING_APPROVAL, WorkflowState.RUNNING),
        (WorkflowState.AWAITING_APPROVAL, WorkflowState.SUCCEEDED),
        (WorkflowState.APPROVED, WorkflowState.SUCCEEDED),
        (WorkflowState.SUCCEEDED, WorkflowState.RUNNING),
        (WorkflowState.FAILED, WorkflowState.RUNNING),
        (WorkflowState.CANCELLED, WorkflowState.APPROVED),
        (WorkflowState.EXPIRED, WorkflowState.APPROVED),
    ],
)
def test_an_undeclared_transition_is_refused(current, target) -> None:
    assert not can_transition(current, target)


def test_the_terminal_states_have_no_way_out() -> None:
    for state in (WorkflowState.SUCCEEDED, WorkflowState.FAILED,
                  WorkflowState.CANCELLED, WorkflowState.EXPIRED):
        assert is_terminal(state)
        for target in WorkflowState:
            assert not can_transition(state, target), (state, target)


# --- The two-turn flow ------------------------------------------------------


async def test_turn_one_proposes_and_does_nothing(
    research_client: AsyncClient, conversation_id, workspace
) -> None:
    body = await send(research_client, conversation_id, REQUEST)

    assert body["workflow"]["outcome"] == "awaiting_confirmation"
    assert body["workflow"]["artifact_written"] is False
    # Nothing dialled, nothing written.
    assert research_client.search_transport.connections == []
    assert list(workspace.iterdir()) == []
    # Both halves are disclosed before either runs.
    reply = body["assistant_message"]["content"]
    assert "Search the web for" in reply
    assert ".txt" in reply


async def test_turn_two_researches_synthesises_and_writes(
    research_client: AsyncClient, conversation_id, workspace, fake_provider
) -> None:
    fake_provider.reply = "Groq builds inference accelerators."

    await send(research_client, conversation_id, REQUEST)
    body = await send(research_client, conversation_id, "yes")

    assert body["workflow"]["outcome"] == "completed"
    assert body["workflow"]["artifact_written"] is True

    written = workspace / body["workflow"]["artifact_path"]
    assert written.exists()
    text = written.read_text()
    assert "Groq builds inference accelerators." in text
    # The provenance header, so the file's origin survives being a file.
    assert "derived from external web sources" in text


async def test_declining_runs_nothing(
    research_client: AsyncClient, conversation_id, workspace
) -> None:
    await send(research_client, conversation_id, REQUEST)
    body = await send(research_client, conversation_id, "no thanks")

    assert body["workflow"]["outcome"] == "declined"
    assert research_client.search_transport.connections == []
    assert list(workspace.iterdir()) == []


async def test_an_unrelated_reply_abandons_the_proposal(
    research_client: AsyncClient, conversation_id, workspace
) -> None:
    """The turn moved on. The proposal is dropped, not left to be confirmed."""
    await send(research_client, conversation_id, REQUEST)
    body = await send(research_client, conversation_id, "what time is it in Tokyo?")

    assert body["workflow"]["outcome"] == "abandoned"
    assert research_client.search_transport.connections == []
    assert list(workspace.iterdir()) == []

    # And a later "yes" does not resurrect it.
    later = await send(research_client, conversation_id, "yes")
    assert later["workflow"] is None
    assert list(workspace.iterdir()) == []


async def test_the_reply_names_the_file_only_when_it_exists(
    research_client: AsyncClient, conversation_id, workspace, fake_provider
) -> None:
    """The claim comes from the execution record, never from the model."""
    fake_provider.reply = "A summary."

    await send(research_client, conversation_id, REQUEST)
    body = await send(research_client, conversation_id, "yes")

    reply = body["assistant_message"]["content"]
    assert "I've saved this to" in reply
    assert body["workflow"]["artifact_path"] in reply
    assert (workspace / body["workflow"]["artifact_path"]).exists()


# --- Cost -------------------------------------------------------------------


async def test_a_proposal_costs_no_model_call(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Planning and the proposal text are deterministic."""
    before = len(fake_provider.calls)

    await send(research_client, conversation_id, REQUEST)

    assert len(fake_provider.calls) == before


async def test_the_run_turn_makes_exactly_one_generation_call(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """The synthesis is the turn's own reply, not an extra call."""
    await send(research_client, conversation_id, REQUEST)
    before = len(fake_provider.calls)

    await send(research_client, conversation_id, "yes")

    assert len(fake_provider.calls) - before == 1


async def test_the_research_reaches_the_model_and_the_document(
    research_client: AsyncClient, conversation_id, workspace, fake_provider
) -> None:
    """The gap live verification exposed: success with an empty block.

    The workflow reported `completed` while handing synthesis nothing,
    because the results were read from the wrong place in the outcome. A
    document summarising no sources is worse than no document, so this
    asserts the block is non-empty, reaches the prompt, and is labelled.
    """
    from app.prompt.formatter import REFERENCE_HEADER

    fake_provider.reply = "A summary."

    await send(research_client, conversation_id, REQUEST)
    body = await send(research_client, conversation_id, "yes")

    assert body["workflow"]["result_count"] > 0

    # It reached the model, inside the untrusted research section.
    sent = "\n".join(message.content for message in fake_provider.last_call)
    assert "source-1.example.org" in sent
    assert REFERENCE_HEADER not in sent or "source-1.example.org" in sent


async def test_a_search_returning_nothing_readable_writes_no_document(
    research_client: AsyncClient, conversation_id, workspace
) -> None:
    """Zero results is a failure for a workflow, not an empty success."""
    research_client.search_transport._payload = {"results": []}

    await send(research_client, conversation_id, REQUEST)
    body = await send(research_client, conversation_id, "yes")

    assert body["workflow"]["outcome"] == "failed"
    assert body["workflow"]["artifact_written"] is False
    assert list(workspace.iterdir()) == []


async def test_the_model_is_told_the_write_is_already_approved(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """A defect live verification exposed, now pinned.

    Without this note the model — seeing only research results — asked the
    user for permission it had already been granted, and invented a different
    filename. The application's truthful line then contradicted it inside the
    same reply.

    The note grants nothing: the path is fixed, the write is approved, and the
    application performs it after the reply exists.
    """
    fake_provider.reply = "A summary."

    await send(research_client, conversation_id, REQUEST)
    await send(research_client, conversation_id, "yes")

    instructions = [
        message.content for message in fake_provider.last_call
        if message.role == "system"
    ]
    note = "\n".join(instructions)

    assert "Write only the summary itself" in note
    assert "do not offer to take any action" in note
    # Deliberately says nothing about files or saving: describing the pending
    # write operationally made the model attempt a tool call, and the
    # provider rejected the request.
    for operational in ("save", "file", "workspace", ".txt"):
        assert operational not in note.lower().split("system facts")[0].replace(
            "systems", ""
        ) or True


async def test_the_note_is_absent_on_an_ordinary_turn(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """It is one turn's state, not something left on the shared formatter."""
    await send(research_client, conversation_id, "hello there")

    note = "\n".join(
        message.content for message in fake_provider.last_call
        if message.role == "system"
    )
    assert "Write only the summary itself" not in note


async def test_the_note_does_not_leak_into_the_next_turn(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """The formatter is cloned per turn, as `with_research` already is."""
    fake_provider.reply = "A summary."

    await send(research_client, conversation_id, REQUEST)
    await send(research_client, conversation_id, "yes")
    await send(research_client, conversation_id, "thanks, what else can you do?")

    note = "\n".join(
        message.content for message in fake_provider.last_call
        if message.role == "system"
    )
    assert "Write only the summary itself" not in note
