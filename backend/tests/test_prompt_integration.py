"""Stage 3B: the chat request path end to end.

Covers the acceptance criteria that can only be checked against the running
application: the single knowledge-to-prompt path, the one synchronous model
call, duplication, failure isolation, and that the background learning loop
still runs afterwards.
"""

import ast
import json
import pathlib

import pytest
from httpx import AsyncClient

from app.prompt.formatter import REFERENCE_HEADER, knowledge_block
from app.prompt.schemas import PromptSection

from tests.test_retrieval_integration import NOTHING_TO_STORE, seed_knowledge

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


def sent(provider):
    return provider.last_call


# --- Exactly one synchronous request-path model call ------------------------


async def test_one_synchronous_generation_call_per_turn(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    # `calls` records only non-json_mode generation, i.e. the chat call.
    assert len(fake_provider.calls) == 1


async def test_stage_3b_adds_no_model_call_over_stage_2d(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """No relevance check, no summarisation pass, no second generation."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    for index in range(3):
        await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": f"Turn {index}: what does Mai use?"},
        )

    assert len(fake_provider.calls) == 3


async def test_a_turn_with_no_knowledge_still_makes_exactly_one_call(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello there!"},
    )

    assert len(fake_provider.calls) == 1


# --- One production path for long-term knowledge ----------------------------


async def test_knowledge_arrives_in_exactly_one_block(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    blocks = [m for m in sent(fake_provider) if REFERENCE_HEADER in m.content]
    assert len(blocks) == 1


async def test_the_retired_legacy_renderer_is_not_called_during_chat(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    """Instrument Stage 2D's old renderer: chat must never reach it.

    This is the direct test of the migration. `RetrievalService.render` still
    exists for the debug endpoints and the Stage 2D character budget, but the
    chat request path no longer has a route to it.
    """
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    calls = []
    from app.retrieval.service import RetrievalService

    original = RetrievalService.render

    def spy(self, package):
        calls.append(package)
        return original(self, package)

    monkeypatch.setattr(RetrievalService, "render", spy)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert knowledge_block(sent(fake_provider)) is not None, "knowledge did reach the model"
    assert calls == [], "legacy Stage 2D prompt rendering ran during a chat turn"


def test_chat_service_does_not_import_the_legacy_renderer() -> None:
    """Structural: the orchestrator has no access to Stage 2D rendering."""
    source = (APP_ROOT / "services" / "chat_service.py").read_text()
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "app.retrieval.service" not in imported
    assert "app.retrieval.context_builder" not in imported
    assert ".render(" not in source


def test_only_the_formatter_builds_prompt_messages() -> None:
    """Every `LLMMessage` in the app comes from a known, intended place.

    Chat prompts are built in exactly one module. The extractors build their
    own prompts, which are self-contained instructions to a model about a
    single memory and carry no retrieved knowledge -- they are listed here
    explicitly so a new construction site cannot appear unnoticed.
    """
    allowed = {
        "prompt/formatter.py",       # Stage 3B: the chat prompt
        "memory/extractor.py",       # background extraction
        "entities/extractor.py",     # background extraction
        "relationships/extractor.py",  # background extraction
        "intent/classifier.py",  # Stage 4A: classification, not the chat prompt
        "llm/providers/openai_compatible.py",  # health probe ping
    }

    found = set()
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "LLMMessage"
            ):
                found.add(str(path.relative_to(APP_ROOT)))

    assert found == allowed, f"unexpected LLMMessage construction: {found - allowed}"


# --- Duplication ------------------------------------------------------------


async def test_a_memory_appears_once_in_the_final_messages(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    whole = "\n".join(m.content for m in sent(fake_provider))
    assert whole.count("User selected PostgreSQL for local storage in Mai.") == 1


async def test_a_relationship_appears_once_in_the_final_messages(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """`Mai USES PostgreSQL` must not arrive through two paths."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    whole = "\n".join(m.content for m in sent(fake_provider))
    assert whole.count("Mai USES PostgreSQL") == 1


async def test_the_current_message_appears_exactly_once(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """The persisted user message must not also show up as history."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE
    question = "What database does Mai use?"

    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": question}
    )

    contents = [m.content for m in sent(fake_provider)]
    assert contents.count(question) == 1
    assert contents[-1] == question


async def test_history_is_not_duplicated_across_turns(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    for text in ["first", "second", "third"]:
        await client.post(
            f"/api/conversations/{conversation_id}/messages", json={"content": text}
        )

    contents = [m.content for m in sent(fake_provider)]
    assert contents.count("first") == 1
    assert contents.count("second") == 1
    assert contents.count("third") == 1


async def test_the_prompt_the_debug_endpoint_reports_matches_what_was_sent(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Debug and chat must agree, or debug is not inspecting production."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE
    question = "What database does Mai use?"

    debug = (
        await client.post(
            "/api/prompt/debug",
            json={"conversation_id": str(conversation_id), "message": question},
        )
    ).json()

    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": question}
    )

    assert [m["role"] for m in debug["messages"]] == [
        m.role for m in sent(fake_provider)
    ]
    assert debug["total_messages"] == len(sent(fake_provider))


# --- Failure isolation ------------------------------------------------------


async def test_retrieval_failure_still_answers_without_knowledge(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.reply = "Answering anyway."
    fake_provider.extraction_reply = NOTHING_TO_STORE

    async def boom(*args, **kwargs):
        raise RuntimeError("retrieval is down")

    monkeypatch.setattr("app.retrieval.service.RetrievalService.retrieve", boom)

    # Give the conversation history to fall back on.
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Earlier turn."},
    )
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Answering anyway."
    assert knowledge_block(sent(fake_provider)) is None
    contents = [m.content for m in sent(fake_provider)]
    assert "Earlier turn." in contents
    assert contents[-1] == "What database does Mai use?"


async def test_context_assembly_failure_still_answers(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.reply = "Fallback answer."
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Earlier turn."},
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("assembly is down")

    monkeypatch.setattr("app.context.service.ContextService.build", boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Fallback answer."
    messages = sent(fake_provider)
    assert knowledge_block(messages) is None, "fallback leaked long-term knowledge"
    contents = [m.content for m in messages]
    # System instructions, the recovered conversation, and the current message.
    assert messages[0].role == "system"
    assert "Earlier turn." in contents
    assert contents[-1] == "What database does Mai use?"
    assert contents.count("What database does Mai use?") == 1


async def test_prompt_formatting_failure_still_answers(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.reply = "Minimal answer."
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Earlier turn."},
    )

    def boom(self, package):
        raise RuntimeError("formatting is broken")

    monkeypatch.setattr("app.prompt.formatter.PromptFormatter.format", boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Minimal answer."
    messages = sent(fake_provider)
    assert messages[0].role == "system"
    assert knowledge_block(messages) is None
    contents = [m.content for m in messages]
    assert "Earlier turn." in contents
    assert contents[-1] == "What database does Mai use?"


async def test_no_fallback_path_resurrects_the_legacy_renderer(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    """Every degraded path, instrumented against the retired renderer."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    calls = []
    from app.retrieval.service import RetrievalService

    original = RetrievalService.render

    def spy(self, package):
        calls.append(package)
        return original(self, package)

    monkeypatch.setattr(RetrievalService, "render", spy)

    def broken_format(self, package):
        raise RuntimeError("formatting is broken")

    monkeypatch.setattr("app.prompt.formatter.PromptFormatter.format", broken_format)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assert response.status_code == 201
    assert calls == []
    assert knowledge_block(sent(fake_provider)) is None


async def test_conversation_history_failure_still_answers(
    client: AsyncClient, fake_provider, session_factory, monkeypatch
) -> None:
    """Both the assembly and the fallback lose history; the message survives."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.reply = "Current message only."
    fake_provider.extraction_reply = NOTHING_TO_STORE

    real_get_messages = (
        "app.services.conversation_service.ConversationService.get_messages"
    )

    async def boom(self, conversation_id, limit=None):
        raise OSError("history unavailable")

    monkeypatch.setattr(real_get_messages, boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Still answer me."},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Current message only."
    assert sent(fake_provider)[-1].content == "Still answer me."


# --- Background pipeline regression -----------------------------------------


async def test_the_background_learning_loop_still_runs(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Stage 2 extraction must survive the Stage 3B refactor intact."""
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "User is building Mai with PostgreSQL.",
                    "memory_type": "semantic",
                    "importance_score": 8,
                    "confidence_score": 0.95,
                }
            ],
        }
    )
    fake_provider.entity_reply = json.dumps(
        {
            "entities": [
                {"name": "Mai", "entity_type": "project", "confidence_score": 0.95},
                {
                    "name": "PostgreSQL",
                    "entity_type": "technology",
                    "confidence_score": 0.95,
                },
            ]
        }
    )
    fake_provider.relationship_reply = json.dumps(
        {
            "relationships": [
                {
                    "source_entity": "Mai",
                    "relationship_type": "USES",
                    "target_entity": "PostgreSQL",
                    "confidence_score": 0.93,
                }
            ]
        }
    )

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I am building Mai with PostgreSQL."},
    )
    assert response.status_code == 201

    assert (await client.get("/api/memories")).json()["total"] == 1
    entities = (await client.get("/api/entities")).json()["items"]
    assert {e["canonical_name"] for e in entities} == {"Mai", "PostgreSQL"}
    relationships = (await client.get("/api/relationships")).json()["items"]
    assert len(relationships) == 1
    assert relationships[0]["relationship_type"] == "USES"

    # Still exactly one synchronous generation call; the three extraction
    # calls are background and use json_mode.
    assert len(fake_provider.calls) == 1
    assert len(fake_provider.extraction_calls) == 1
    assert len(fake_provider.entity_calls) == 1
    assert len(fake_provider.relationship_calls) == 1


async def test_what_the_model_learns_becomes_reference_knowledge_next_turn(
    client: AsyncClient, fake_provider
) -> None:
    """The full loop: learn in one conversation, recall it in another."""
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "User chose Redis for caching in Mai.",
                    "memory_type": "decision",
                    "importance_score": 8,
                    "confidence_score": 0.95,
                }
            ],
        }
    )

    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages",
        json={"content": "I chose Redis for caching in Mai."},
    )

    fake_provider.extraction_reply = NOTHING_TO_STORE
    second = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{second}/messages",
        json={"content": "What did I choose for caching in Mai?"},
    )

    block = knowledge_block(sent(fake_provider))
    assert block is not None
    assert "Redis" in block


# --- End-to-end knowledge-aware chat ----------------------------------------


async def test_knowledge_from_four_conversations_reaches_a_fresh_one(
    client: AsyncClient, fake_provider
) -> None:
    """The Stage 3B headline case.

    Four separate conversations teach Mai four facts. A fifth conversation,
    with no history of its own, asks a question that touches all of them.

    What is verified here is that the knowledge reaches the final model call.
    Whether a real model then *uses* it well is a property of the model, not of
    this pipeline, and the suite runs against a fake provider with no network.
    """
    facts = [
        (
            "I am building Mai as my personal AI environment.",
            "User is building Mai as a personal AI environment.",
            "semantic",
        ),
        (
            "I decided to use PostgreSQL for local storage.",
            "User decided to use PostgreSQL for local storage in Mai.",
            "decision",
        ),
        (
            "I switched from OpenRouter to Groq for inference.",
            "User switched from OpenRouter to Groq for inference in Mai.",
            "decision",
        ),
        (
            "I am using Claude Code to build Mai.",
            "User is using Claude Code to build Mai.",
            "semantic",
        ),
    ]

    for message, remembered, kind in facts:
        fake_provider.extraction_reply = json.dumps(
            {
                "should_store_memory": True,
                "memories": [
                    {
                        "content": remembered,
                        "memory_type": kind,
                        "importance_score": 9,
                        "confidence_score": 0.95,
                    }
                ],
            }
        )
        conversation = (await client.post("/api/conversations", json={})).json()["id"]
        response = await client.post(
            f"/api/conversations/{conversation}/messages",
            json={"content": message},
        )
        assert response.status_code == 201

    assert (await client.get("/api/memories")).json()["total"] == 4

    fake_provider.extraction_reply = NOTHING_TO_STORE
    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    question = "What technology stack and development approach am I using for Mai?"
    await client.post(
        f"/api/conversations/{fresh}/messages", json={"content": question}
    )

    messages = sent(fake_provider)
    block = knowledge_block(messages)
    assert block is not None, "no knowledge reached a brand new conversation"
    assert "PostgreSQL" in block
    assert "Groq" in block
    assert "Claude Code" in block
    # The question itself is still the last thing the model sees.
    assert messages[-1].content == question
    assert messages[-1].role == "user"
    # And it cost one generation call.
    assert len(fake_provider.calls) == 5


async def test_old_knowledge_does_not_displace_the_current_message(
    client: AsyncClient, fake_provider
) -> None:
    """Stale reference data must stay visibly subordinate to what is said now."""
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "User uses OpenRouter for inference in Mai.",
                    "memory_type": "decision",
                    "importance_score": 8,
                    "confidence_score": 0.95,
                }
            ],
        }
    )
    first = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{first}/messages",
        json={"content": "I use OpenRouter for inference in Mai."},
    )

    fake_provider.extraction_reply = NOTHING_TO_STORE
    fresh = (await client.post("/api/conversations", json={})).json()["id"]
    await client.post(
        f"/api/conversations/{fresh}/messages",
        json={"content": "I switched to Groq for inference in Mai."},
    )

    messages = sent(fake_provider)
    block = knowledge_block(messages)
    assert block is not None
    assert "OpenRouter" in block, "the stale memory was retrieved"
    # The stale claim is inside the reference block, and the current statement
    # is the final message -- the structure the model reads precedence from.
    assert "OpenRouter" not in messages[-1].content
    assert messages[-1].content == "I switched to Groq for inference in Mai."
    assert messages[-1].role == "user"
    assert "it may be out of date" in block.lower()


# --- Context budgets --------------------------------------------------------


async def test_a_tighter_budget_produces_a_smaller_prompt(
    client: AsyncClient, fake_provider, session_factory, settings
) -> None:
    """The formatter honours Stage 3A's budget rather than working around it."""

    # Seeded once: the knowledge base is shared, only the budget changes.
    await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    async def prompt_content_chars(budget: int, limit: int) -> int:
        settings.CONTEXT_MAX_TOTAL_CHARS = budget
        settings.CONTEXT_RECENT_MESSAGE_LIMIT = limit
        conversation_id = (
            await client.post("/api/conversations", json={})
        ).json()["id"]

        for index in range(12):
            await client.post(
                f"/api/conversations/{conversation_id}/messages",
                json={
                    "content": f"Filler turn {index} about Mai and PostgreSQL. " * 8
                },
            )
        messages = sent(fake_provider)
        conversation = sum(
            len(m.content) for m in messages if m.role in {"user", "assistant"}
        )
        bullets = sum(
            len(line)
            for line in (knowledge_block(messages) or "").splitlines()
            if line.startswith("- ")
        )
        return conversation + bullets

    generous = await prompt_content_chars(10000, 12)
    tight = await prompt_content_chars(1200, 4)

    assert tight < generous, "the budget made no difference to the prompt"
    assert tight <= 1200 + len("What database does Mai use?")


async def test_formatter_framing_is_fixed_regardless_of_content_size(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    """Stage 3B adds bounded framing, never content that scales with input.

    The reference block carries a fixed header, preamble and category labels on
    top of the items Stage 3A authorised. That overhead is constant: it is the
    same number of characters for one memory as for ten, which is what makes
    "the formatter adds nothing unbounded" checkable rather than a claim.
    """
    from app.prompt.formatter import PromptFormatter

    from tests.test_prompt_formatter import full_package, memory, package

    formatter = PromptFormatter("You are Mai.")

    def framing(source) -> int:
        prompt = formatter.format(source)
        block = knowledge_block(prompt.messages) or ""
        bullets = sum(
            len(line) for line in block.splitlines() if line.startswith("- ")
        )
        return len(block) - bullets

    small = framing(package(memories=[memory("Tiny.", rank=1)]))
    large = framing(
        package(
            memories=[memory("A considerably longer memory. " * 20, rank=i)
                      for i in range(1, 11)]
        )
    )
    mixed = framing(full_package())

    # Bullet separators are the only part that grows, and by one newline each.
    assert large - small == 9, "framing grew with the number of items"
    assert mixed > small, "category labels are counted"
    assert small < 1200, "framing overhead is small enough to be fixed cost"


async def test_the_formatter_adds_no_knowledge_beyond_the_package(
    client: AsyncClient, fake_provider, session_factory, settings
) -> None:
    settings.CONTEXT_MAX_MEMORY_ITEMS = 1
    settings.CONTEXT_MAX_RELATIONSHIP_ITEMS = 1
    settings.CONTEXT_MAX_ENTITY_ITEMS = 1
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What does Mai use for storage and inference?"},
    )

    block = knowledge_block(sent(fake_provider))
    assert block is not None
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(bullets) == 3, f"expected one item per category, got {bullets}"
