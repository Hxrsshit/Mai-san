"""Stage 3C: lifecycle-aware retrieval and the end-to-end evolution scenario.

Verifies the point of the whole stage: a current question gets current
knowledge, a question about the past can still reach what was retired, and the
retrieval path never writes.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.knowledge.models import KnowledgeConflict
from app.memory.models import Memory, MemoryStatus
from app.prompt.formatter import knowledge_block
from app.retrieval.query_normalizer import analyse
from app.relationships.models import Relationship, RelationshipStatus

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})


def remembers(content, kind="decision", importance=9):
    return json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": content,
                    "memory_type": kind,
                    "importance_score": importance,
                    "confidence_score": 0.95,
                }
            ],
        }
    )


def entities(*pairs):
    return json.dumps(
        {
            "entities": [
                {"name": name, "entity_type": kind, "confidence_score": 0.95}
                for name, kind in pairs
            ]
        }
    )


def relationships(*triples):
    return json.dumps(
        {
            "relationships": [
                {
                    "source_entity": source,
                    "relationship_type": kind,
                    "target_entity": target,
                    "confidence_score": 0.93,
                }
                for source, kind, target in triples
            ]
        }
    )


async def teach(client, provider, message, memory, entity_pairs=(), triples=()):
    """One conversation that teaches Mai a single fact."""
    provider.extraction_reply = remembers(memory)
    provider.entity_reply = entities(*entity_pairs) if entity_pairs else '{"entities": []}'
    provider.relationship_reply = (
        relationships(*triples) if triples else '{"relationships": []}'
    )
    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    response = await client.post(
        f"/api/conversations/{conversation}/messages", json={"content": message}
    )
    assert response.status_code == 201
    return conversation


async def ask(client, provider, question):
    """A fresh conversation asking one question. Returns the prompt sent."""
    provider.extraction_reply = NOTHING_TO_STORE
    provider.entity_reply = '{"entities": []}'
    provider.relationship_reply = '{"relationships": []}'
    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{conversation}/messages", json={"content": question}
    )
    return provider.last_call


# --- Historical intent detection --------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What provider did I use before?",
        "What provider did I previously use?",
        "What was my old technology stack?",
        "Which provider was I using in the past?",
        "Which database did I use earlier?",
        "What was I using formerly?",
    ],
)
def test_questions_about_the_past_are_recognised(question) -> None:
    assert analyse(question).historical_intent is True


@pytest.mark.parametrize(
    "question",
    [
        "What am I using?",
        "What is my current stack?",
        "Which database does Mai use now?",
        "What technology stack am I currently using for Mai?",
        "What was my name?",
        "What did I decide about the database?",
        # "use to" is not "used to": this asks what tool serves a purpose now.
        "What did I use to run inference?",
    ],
)
def test_ordinary_questions_are_not_historical(question) -> None:
    """A past-tense verb is not historical intent.

    "What was my name?" asks about the present, and "what did I use to run
    inference" asks which tool serves that purpose -- neither is a question
    about retired knowledge. Marking them historical would surface superseded
    memories on ordinary questions, which is the failure mode this narrow
    marker set exists to avoid.
    """
    assert analyse(question).historical_intent is False


# --- The evolution scenario -------------------------------------------------


@pytest.fixture
async def evolved(client: AsyncClient, fake_provider):
    """Three conversations, one of which retires the first."""
    await teach(
        client, fake_provider,
        "Mai uses OpenRouter for inference.",
        "Mai uses OpenRouter for inference.",
        entity_pairs=[("Mai", "project"), ("OpenRouter", "company")],
        triples=[("Mai", "USES", "OpenRouter")],
    )
    await teach(
        client, fake_provider,
        "I migrated Mai from OpenRouter to Groq.",
        "User migrated Mai from OpenRouter to Groq.",
        entity_pairs=[("Mai", "project"), ("Groq", "company")],
        triples=[("Mai", "USES", "Groq")],
    )
    await teach(
        client, fake_provider,
        "I use PostgreSQL for Mai's database.",
        "User uses PostgreSQL for Mai's database.",
        entity_pairs=[("Mai", "project"), ("PostgreSQL", "technology")],
        triples=[("Mai", "USES", "PostgreSQL")],
    )
    return client


async def test_the_migration_retired_the_old_provider(
    evolved, session_factory
) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(select(Memory.content, Memory.status))
        ).all()
        by_content = {content: status for content, status in rows}

    assert by_content["Mai uses OpenRouter for inference."] is MemoryStatus.SUPERSEDED
    assert (
        by_content["User migrated Mai from OpenRouter to Groq."] is MemoryStatus.ACTIVE
    )
    assert (
        by_content["User uses PostgreSQL for Mai's database."] is MemoryStatus.ACTIVE
    )


async def test_a_current_question_gets_the_current_stack(
    evolved, fake_provider
) -> None:
    """The headline case: Groq and PostgreSQL, not OpenRouter."""
    sent = await ask(
        evolved, fake_provider,
        "What technology stack am I currently using for Mai?",
    )

    block = knowledge_block(sent)
    assert block is not None, "no knowledge reached the model"
    assert "Groq" in block
    assert "PostgreSQL" in block
    assert "Mai uses OpenRouter for inference." not in block, (
        "a superseded memory was presented as current"
    )


async def test_a_historical_question_can_reach_the_retired_knowledge(
    evolved, fake_provider
) -> None:
    sent = await ask(
        evolved, fake_provider, "What provider did I use before Groq?"
    )

    block = knowledge_block(sent)
    assert block is not None
    assert "OpenRouter" in block


async def test_history_is_only_offered_when_it_is_asked_for(
    evolved, fake_provider
) -> None:
    """The same subject, two phrasings, two different candidate pools."""
    current = knowledge_block(
        await ask(evolved, fake_provider, "Which inference provider does Mai use?")
    ) or ""
    historical = knowledge_block(
        await ask(
            evolved, fake_provider,
            "Which inference provider did Mai previously use?",
        )
    ) or ""

    assert "Mai uses OpenRouter for inference." not in current
    assert "OpenRouter" in historical


async def test_disabling_historical_retrieval_hides_the_past(
    evolved, fake_provider, settings
) -> None:
    settings.HISTORICAL_RETRIEVAL_ENABLED = False

    sent = await ask(evolved, fake_provider, "What provider did I use before?")

    block = knowledge_block(sent) or ""
    assert "Mai uses OpenRouter for inference." not in block


async def test_the_retired_relationship_is_not_offered_as_current(
    evolved, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Relationship.status).where(
                    Relationship.status == RelationshipStatus.SUPERSEDED
                )
            )
        ).scalars().all()
    assert rows, "the OpenRouter relationship was never retired"

    block = knowledge_block(
        await ask(evolved, fake_provider, "What does Mai use for inference?")
    ) or ""
    assert "Mai USES OpenRouter" not in block


async def test_the_lifecycle_decision_is_traceable_through_the_api(
    evolved,
) -> None:
    memories = (await evolved.get("/api/memories?status=superseded")).json()
    assert memories["total"] >= 1
    retired = memories["items"][0]

    body = (await evolved.get(f"/api/knowledge/debug/{retired['id']}")).json()

    assert body["status"] == "superseded"
    assert body["superseded_by"], "no explanation for why this is historical"
    link = body["superseded_by"][0]
    assert link["resolution"] == "superseded"
    assert link["reason"] == "explicit_replacement"
    assert link["triggering_memory_id"] is not None


# --- The request path never writes ------------------------------------------


async def test_retrieval_does_not_change_lifecycle_state(
    evolved, fake_provider, session_factory
) -> None:
    """Asking a question must not supersede anything."""

    async def snapshot():
        async with session_factory() as session:
            memories = dict(
                (await session.execute(select(Memory.id, Memory.status))).all()
            )
            relationships = dict(
                (
                    await session.execute(
                        select(Relationship.id, Relationship.status)
                    )
                ).all()
            )
            links = (
                await session.execute(select(KnowledgeConflict.id))
            ).scalars().all()
        return memories, relationships, sorted(str(link) for link in links)

    before = await snapshot()

    # A question that contradicts stored knowledge, asked without storing.
    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation = (await evolved.post("/api/conversations", json={})).json()["id"]
    await evolved.post(
        f"/api/conversations/{conversation}/messages",
        json={"content": "I switched Mai from Groq to something else."},
    )

    assert await snapshot() == before


async def test_the_prompt_debug_endpoint_does_not_change_lifecycle_state(
    evolved, session_factory
) -> None:
    async def link_count():
        async with session_factory() as session:
            return len(
                (await session.execute(select(KnowledgeConflict.id))).scalars().all()
            )

    before = await link_count()
    await evolved.post(
        "/api/prompt/debug",
        json={"message": "I switched Mai from Groq to Anthropic."},
    )
    assert await link_count() == before


async def test_the_lifecycle_debug_endpoint_makes_no_model_call(
    evolved, fake_provider, session_factory
) -> None:
    async with session_factory() as session:
        memory_id = (
            await session.execute(select(Memory.id).limit(1))
        ).scalars().one()

    calls_before = len(fake_provider.calls)
    response = await evolved.get(f"/api/knowledge/debug/{memory_id}")

    assert response.status_code == 200
    assert len(fake_provider.calls) == calls_before


async def test_the_lifecycle_debug_endpoint_404s_for_an_unknown_memory(
    evolved,
) -> None:
    response = await evolved.get(f"/api/knowledge/debug/{uuid.uuid4()}")
    assert response.status_code == 404


# --- Current message authority ----------------------------------------------


async def test_the_current_message_stays_authoritative_over_stored_knowledge(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Stored: OpenRouter. Current message: "I switched to Groq."."""
    await teach(
        client, fake_provider,
        "Mai uses OpenRouter for inference.",
        "Mai uses OpenRouter for inference.",
        entity_pairs=[("Mai", "project"), ("OpenRouter", "company")],
        triples=[("Mai", "USES", "OpenRouter")],
    )

    async def link_count():
        async with session_factory() as session:
            return len(
                (await session.execute(select(KnowledgeConflict.id))).scalars().all()
            )

    before = await link_count()

    fake_provider.extraction_reply = NOTHING_TO_STORE
    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    question = "I switched to Groq."
    await client.post(
        f"/api/conversations/{conversation}/messages", json={"content": question}
    )

    sent = fake_provider.last_call
    # The message is intact, last, and exactly once.
    assert sent[-1].role == "user"
    assert sent[-1].content == question
    assert [m.content for m in sent].count(question) == 1
    # The stale memory is reference data only.
    block = knowledge_block(sent) or ""
    assert "OpenRouter" not in sent[-1].content
    if block:
        assert "it may be out of date" in block.lower()
    # And nothing was written during context assembly.
    assert await link_count() == before


# --- Zero additional model calls --------------------------------------------


async def test_conflict_evaluation_adds_no_request_path_model_call(
    client: AsyncClient, fake_provider
) -> None:
    await teach(
        client, fake_provider,
        "Mai uses OpenRouter.", "Mai uses OpenRouter.",
        entity_pairs=[("Mai", "project"), ("OpenRouter", "company")],
        triples=[("Mai", "USES", "OpenRouter")],
    )
    calls_before = len(fake_provider.calls)

    fake_provider.extraction_reply = remembers(
        "User migrated Mai from OpenRouter to Groq."
    )
    fake_provider.entity_reply = entities(("Mai", "project"), ("Groq", "company"))
    fake_provider.relationship_reply = relationships(("Mai", "USES", "Groq"))

    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{conversation}/messages",
        json={"content": "I migrated Mai from OpenRouter to Groq."},
    )

    # Exactly one synchronous generation call for the turn. The three
    # extraction calls are background and use json_mode; conflict evaluation
    # adds none of either kind.
    assert len(fake_provider.calls) == calls_before + 1


def test_the_knowledge_package_never_calls_a_model() -> None:
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "knowledge"
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not (
                    name == "app.llm" or name.startswith("app.llm.")
                ), f"{path.name} imports {name}"


def test_the_request_path_never_imports_the_lifecycle_writer() -> None:
    """Retrieval, assembly and formatting must not be able to mutate."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    request_path = ["retrieval", "context", "prompt", "services"]

    for package in request_path:
        for path in sorted((root / package).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                for name in names:
                    assert not name.startswith("app.knowledge"), (
                        f"{package}/{path.name} imports {name}"
                    )
