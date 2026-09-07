"""Stage 4F-D: web research from the chat path, end to end.

Two turns, always. A research request is *proposed* and answered with a
question; only an explicit confirmation on the immediately following turn
sends anything to a search provider.

No test here contacts a real search service. The integration, the client and
the network policy are all real; only the socket is replaced. So what runs is
the code that would run live, which is what makes the assertions worth making.
"""

import json

import pytest
from httpx import AsyncClient

from app.execution.models import Execution
from app.execution.states import ExecutionState
from app.research.confirmation import Confirmation, interpret
from app.research.schemas import ResearchOutcome
from app.research.service import CHAT_CONFIRMABLE_TOOLS

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})
RESEARCH_REQUEST = "Search the web for the best coffee in Lisbon"


async def send(client: AsyncClient, conversation_id, content: str):
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


# --- The happy path ---------------------------------------------------------


async def test_a_research_request_proposes_and_does_not_search(
    research_client: AsyncClient, conversation_id
) -> None:
    """Turn one sends nothing anywhere."""
    body = await send(research_client, conversation_id, RESEARCH_REQUEST)

    assert body["research"]["outcome"] == "awaiting_confirmation"
    assert body["research"]["searched"] is False
    assert "coffee in Lisbon" in body["research"]["query"]
    # The reply is a question, and it names exactly what would be sent.
    assert "yes" in body["assistant_message"]["content"].lower()
    assert "coffee in Lisbon" in body["assistant_message"]["content"]

    assert research_client.search_transport.connections == []


async def test_the_proposal_turn_costs_no_model_call(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """A confirmation prompt is application text, so no model is asked.

    Not merely an optimisation. A model asked to phrase "may I search?" could
    phrase it as "I searched", and the whole point of the two-turn design is
    that the first turn provably did not.
    """
    before = len(fake_provider.calls)

    await send(research_client, conversation_id, RESEARCH_REQUEST)

    assert len(fake_provider.calls) == before


async def test_confirming_runs_the_search_and_synthesises(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    await send(research_client, conversation_id, RESEARCH_REQUEST)
    body = await send(research_client, conversation_id, "yes")

    assert body["research"]["outcome"] == "completed"
    assert body["research"]["searched"] is True
    assert body["research"]["result_count"] == 2

    # Exactly one request reached the provider.
    assert len(research_client.search_transport.connections) == 1

    # And the model was asked to synthesise, with the results in the prompt.
    from app.prompt.formatter import RESEARCH_HEADER

    prompt = fake_provider.last_call
    research_messages = [m for m in prompt if RESEARCH_HEADER in m.content]
    assert len(research_messages) == 1
    assert "source-1.example.org" in research_messages[0].content


async def test_the_results_section_sits_below_the_conversation(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Position, not just wording: external content is the least trusted."""
    from app.prompt.formatter import RESEARCH_HEADER, RUNTIME_FACTS_HEADER

    await send(research_client, conversation_id, RESEARCH_REQUEST)
    await send(research_client, conversation_id, "yes")

    contents = [m.content for m in fake_provider.last_call]
    facts_index = next(i for i, c in enumerate(contents) if RUNTIME_FACTS_HEADER in c)
    research_index = next(i for i, c in enumerate(contents) if RESEARCH_HEADER in c)

    assert facts_index < research_index
    # And below the conversation, not merely below the facts. Mutation
    # testing found that asserting only the first left the section free to
    # move above the user's own history -- which would rank a stranger's web
    # page over what the user actually said.
    conversation_indices = [
        index for index, content in enumerate(contents)
        if content in {"Search the web for the best coffee in Lisbon", "yes"}
    ]
    if conversation_indices:
        assert max(conversation_indices[:-1] or [-1]) < research_index

    # And the user's own question is still last.
    assert research_index < len(contents) - 1


async def test_research_results_do_not_leak_into_a_later_turn(
    research_client: AsyncClient, conversation_id, fake_provider
) -> None:
    """The formatter is shared across requests; results must not stick to it.

    `with_research` returns a new formatter for exactly this reason. Mutating
    the shared instance would put one conversation's search results into the
    next turn of another.
    """
    from app.prompt.formatter import RESEARCH_HEADER

    await send(research_client, conversation_id, RESEARCH_REQUEST)
    await send(research_client, conversation_id, "yes")

    # A later, unrelated turn.
    await send(research_client, conversation_id, "Thanks. What is 2 + 2?")

    assert not any(
        RESEARCH_HEADER in message.content for message in fake_provider.last_call
    )


def test_with_research_does_not_touch_the_formatter_it_was_called_on() -> None:
    """The contract, tested directly rather than through a request.

    Mutation testing found that mutating the shared formatter instead of
    cloning it changed nothing observable -- because the formatter is built
    per request today, so there is nothing to leak *into*. That makes the
    clone defence against a future change (a cached or module-level
    formatter), and a guard whose only proof is the current wiring is not a
    guard. Asserted at the unit level, where it holds regardless.
    """
    from datetime import datetime, timezone

    from app.context.schemas import ContextMetadata, ContextPackage
    from app.prompt.formatter import PromptFormatter
    from app.prompt.schemas import PromptSection

    shared = PromptFormatter(system_prompt="You are Mai.")
    package = ContextPackage(
        current_message="hello",
        metadata=ContextMetadata(assembled_at=datetime.now(timezone.utc)),
    )

    with_results = shared.with_research("[1] Example — example.org")

    assert with_results is not shared
    assert any(
        part.section is PromptSection.RESEARCH_RESULTS
        for part in with_results.format(package).parts
    )
    # The original is untouched, and stays untouched.
    assert not any(
        part.section is PromptSection.RESEARCH_RESULTS
        for part in shared.format(package).parts
    )


def test_the_fallback_prompt_can_never_carry_research_results() -> None:
    """Structural, not conditional.

    `fallback` does not call the research renderer at all, so a formatter
    carrying a block still produces a fallback without one. Mutation testing
    showed the difference between passing the shared formatter and the
    research-carrying one to `fallback` is unobservable -- which is the
    correct outcome, and worth pinning as a property rather than left as an
    accident of which variable a line happens to use.
    """
    from app.prompt.formatter import PromptFormatter
    from app.prompt.schemas import PromptSection

    carrying = PromptFormatter(system_prompt="You are Mai.").with_research(
        "[1] Example — example.org"
    )

    parts = carrying.fallback("hello").parts

    assert not any(
        part.section is PromptSection.RESEARCH_RESULTS for part in parts
    )


async def test_a_formatting_failure_drops_the_research_block(
    research_client: AsyncClient, conversation_id, fake_provider, monkeypatch
) -> None:
    """The degraded path carries no external content.

    If formatting failed, the safest prompt is the smallest one -- and text
    written by strangers is the last thing to reintroduce through a fallback
    nobody exercised.
    """
    from app.prompt.formatter import PromptFormatter, RESEARCH_HEADER

    await send(research_client, conversation_id, RESEARCH_REQUEST)

    def explode(self, package):
        raise RuntimeError("formatting failed")

    monkeypatch.setattr(PromptFormatter, "format", explode)

    await send(research_client, conversation_id, "yes")

    assert not any(
        RESEARCH_HEADER in message.content for message in fake_provider.last_call
    )


# --- Declining and abandoning ----------------------------------------------


async def test_declining_runs_nothing(
    research_client: AsyncClient, conversation_id
) -> None:
    await send(research_client, conversation_id, RESEARCH_REQUEST)
    body = await send(research_client, conversation_id, "no thanks")

    assert body["research"]["outcome"] == "declined"
    assert body["research"]["searched"] is False
    assert research_client.search_transport.connections == []


async def test_an_unrelated_reply_abandons_the_proposal(
    research_client: AsyncClient, conversation_id
) -> None:
    """The turn moved on, so the proposal is dropped rather than left armed."""
    await send(research_client, conversation_id, RESEARCH_REQUEST)
    body = await send(research_client, conversation_id, "actually, what is 2+2?")

    assert body["research"]["outcome"] == "abandoned"
    assert research_client.search_transport.connections == []


async def test_a_later_yes_cannot_confirm_an_abandoned_proposal(
    research_client: AsyncClient, conversation_id
) -> None:
    """The attack this design exists to prevent.

    A proposal that survived unrelated turns could be confirmed by a "yes"
    that meant something else entirely -- agreeing with a statement three
    messages later would send a query to a third party. A proposal is
    confirmable by the immediately following turn or not at all.
    """
    await send(research_client, conversation_id, RESEARCH_REQUEST)
    await send(research_client, conversation_id, "hold on, tell me about Lisbon")
    body = await send(research_client, conversation_id, "yes")

    # An ordinary turn: there is nothing pending, so "yes" is just a message.
    assert body["research"] is None
    assert research_client.search_transport.connections == []


async def test_declining_then_yes_runs_nothing(
    research_client: AsyncClient, conversation_id
) -> None:
    await send(research_client, conversation_id, RESEARCH_REQUEST)
    await send(research_client, conversation_id, "no")
    body = await send(research_client, conversation_id, "yes")

    # The proposal was withdrawn, so the later "yes" confirms nothing.
    assert body["research"] is None
    assert research_client.search_transport.connections == []


# --- A bare "yes" with nothing pending -------------------------------------


async def test_yes_with_no_pending_proposal_is_an_ordinary_turn(
    research_client: AsyncClient, conversation_id
) -> None:
    body = await send(research_client, conversation_id, "yes")

    assert body["research"] is None
    assert research_client.search_transport.connections == []


async def test_an_ordinary_message_proposes_nothing(
    research_client: AsyncClient, conversation_id
) -> None:
    body = await send(research_client, conversation_id, "How are you today?")

    assert body["research"] is None
    assert research_client.search_transport.connections == []


async def test_a_mention_of_searching_is_not_a_request(
    research_client: AsyncClient, conversation_id
) -> None:
    """Stage 4D's rule, still holding: a topic is not an imperative."""
    body = await send(
        research_client, conversation_id, "Tell me about web search engines"
    )

    assert body["research"] is None
    assert research_client.search_transport.connections == []


# --- The execution record ---------------------------------------------------


async def test_a_proposal_creates_one_execution_in_proposed_state(
    research_client: AsyncClient, conversation_id, db_session, session_factory
) -> None:
    from sqlalchemy import select

    await send(research_client, conversation_id, RESEARCH_REQUEST)

    async with session_factory() as session:
        rows = (await session.execute(select(Execution))).scalars().all()

    assert len(rows) == 1
    assert rows[0].state is ExecutionState.PROPOSED
    assert rows[0].tool_name == "web_search"
    # Linked to the conversation, which is how the next turn finds it.
    assert rows[0].conversation_id == conversation_id


async def test_confirming_leaves_a_complete_audit_trail(
    research_client: AsyncClient, conversation_id, session_factory
) -> None:
    from sqlalchemy import select

    from app.execution.models import ExecutionEvent

    await send(research_client, conversation_id, RESEARCH_REQUEST)
    await send(research_client, conversation_id, "yes")

    async with session_factory() as session:
        events = (
            await session.execute(
                select(ExecutionEvent).order_by(ExecutionEvent.sequence)
            )
        ).scalars().all()

    assert [event.event_type.value for event in events] == [
        "proposed", "approved", "execution_started", "execution_succeeded",
    ]


async def test_declining_is_recorded_rather_than_forgotten(
    research_client: AsyncClient, conversation_id, session_factory
) -> None:
    """An audit reader should see that a search was proposed and not run."""
    from sqlalchemy import select

    from app.execution.models import ExecutionEvent

    await send(research_client, conversation_id, RESEARCH_REQUEST)
    await send(research_client, conversation_id, "no")

    async with session_factory() as session:
        events = (
            await session.execute(
                select(ExecutionEvent).order_by(ExecutionEvent.sequence)
            )
        ).scalars().all()

    assert [event.event_type.value for event in events] == ["proposed", "revoked"]


# --- Confirmation matching --------------------------------------------------


@pytest.mark.parametrize(
    "reply", ["yes", "Yes", "YES", "yes!", " yes ", "go ahead", "do it",
              "please do", "confirm", "ok", "sure", "yep"],
)
def test_affirmatives_confirm(reply) -> None:
    assert interpret(reply) is Confirmation.CONFIRMED


@pytest.mark.parametrize(
    "reply", ["no", "No thanks", "nope", "cancel", "never mind", "stop",
              "not now", "forget it"],
)
def test_negatives_decline(reply) -> None:
    assert interpret(reply) is Confirmation.DECLINED


@pytest.mark.parametrize(
    "reply",
    [
        "yes and also delete my files",
        "yes, but search for something else instead",
        "I said yes earlier",
        "maybe", "possibly", "hmm",
        "yes " * 20,
        "", "   ",
        "no idea what you mean",
    ],
)
def test_anything_else_is_unrelated(reply) -> None:
    """Neither confirming nor declining. The proposal is dropped."""
    assert interpret(reply) is Confirmation.UNRELATED


def test_confirmation_takes_no_model_call() -> None:
    """Structural: the module imports nothing that could call one."""
    import ast
    import pathlib

    source = pathlib.Path("app/research/confirmation.py").read_text()
    for node in ast.walk(ast.parse(source)):
        modules = []
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for module in modules:
            assert not module.startswith("app.llm"), module
            assert not module.startswith("app.intent"), module
