"""Stage 3B: the prompt inspection endpoint.

`POST /api/prompt/debug` must describe the real prompt without producing any
of its side effects: no model call, no writes, and no leaked configuration.
"""

import json

from httpx import AsyncClient

from app.prompt.schemas import PromptSection

from tests.test_retrieval_integration import NOTHING_TO_STORE, seed_knowledge


async def debug(client: AsyncClient, message: str, conversation_id=None):
    payload = {"message": message}
    if conversation_id is not None:
        payload["conversation_id"] = str(conversation_id)
    response = await client.post("/api/prompt/debug", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# --- No side effects --------------------------------------------------------


async def test_debug_makes_no_model_call(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)

    await debug(client, "What database does Mai use?", conversation_id)

    assert fake_provider.calls == []
    assert fake_provider.extraction_calls == []
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []


async def test_debug_mutates_nothing(
    client: AsyncClient, fake_provider, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)

    before = {
        "memories": (await client.get("/api/memories")).json()["total"],
        "entities": (await client.get("/api/entities")).json()["total"],
        "relationships": (await client.get("/api/relationships")).json()["total"],
        "messages": len(
            (await client.get(f"/api/conversations/{conversation_id}")).json()[
                "messages"
            ]
        ),
        "conversations": (await client.get("/api/conversations")).json()["total"],
    }

    await debug(client, "What database does Mai use?", conversation_id)

    after = {
        "memories": (await client.get("/api/memories")).json()["total"],
        "entities": (await client.get("/api/entities")).json()["total"],
        "relationships": (await client.get("/api/relationships")).json()["total"],
        "messages": len(
            (await client.get(f"/api/conversations/{conversation_id}")).json()[
                "messages"
            ]
        ),
        "conversations": (await client.get("/api/conversations")).json()["total"],
    }

    assert before == after
    # In particular, the inspected message was not stored as a turn.
    assert after["messages"] == before["messages"]


# --- What it reports --------------------------------------------------------


async def test_debug_reports_ordering_and_the_current_message_position(
    client: AsyncClient, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)

    body = await debug(client, "What database does Mai use?", conversation_id)

    assert body["sections_included"] == [
        PromptSection.SYSTEM_INSTRUCTIONS.value,
        PromptSection.REFERENCE_KNOWLEDGE.value,
        PromptSection.CURRENT_MESSAGE.value,
    ]
    assert body["current_message_is_last"] is True
    assert body["current_message_index"] == body["total_messages"] - 1
    assert body["messages"][-1]["role"] == "user"
    assert body["messages"][-1]["content"] == "What database does Mai use?"


async def test_debug_reports_context_counts(
    client: AsyncClient, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)

    body = await debug(client, "What database does Mai use?", conversation_id)

    context = body["context"]
    assert context["memory_count"] >= 1
    assert context["entity_count"] >= 1
    assert context["relationship_count"] >= 1
    stats = body["stats"]
    assert stats["memories_rendered"] == context["memory_count"]
    assert stats["entities_rendered"] == context["entity_count"]
    assert stats["relationships_rendered"] == context["relationship_count"]


async def test_debug_reports_character_counts(
    client: AsyncClient, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)

    body = await debug(client, "What database does Mai use?", conversation_id)

    stats = body["stats"]
    assert stats["current_message_chars"] == len("What database does Mai use?")
    assert stats["reference_chars"] > 0
    assert stats["total_chars"] == (
        stats["instruction_chars"]
        + stats["reference_chars"]
        + stats["conversation_chars"]
        + stats["current_message_chars"]
    )
    assert sum(m["chars"] for m in body["messages"]) == stats["total_chars"]


async def test_debug_reports_no_duplicates_for_a_normal_prompt(
    client: AsyncClient, session_factory
) -> None:
    conversation_id = await seed_knowledge(session_factory)

    body = await debug(client, "What database does Mai use?", conversation_id)

    assert body["has_duplicates"] is False
    assert body["duplicates"]["current_message_occurrences"] == 1
    assert body["duplicates"]["reference_blocks"] == 1
    assert body["duplicates"]["duplicate_reference_lines"] == 0
    assert body["duplicates"]["duplicate_conversation_messages"] == 0


async def test_debug_includes_conversation_history(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "My name is DebugUser."},
    )

    body = await debug(client, "What is my name?", conversation_id)

    conversation = [
        m for m in body["messages"] if m["section"] == PromptSection.CONVERSATION.value
    ]
    assert [m["content"] for m in conversation] == [
        "My name is DebugUser.",
        "Hello from Mai.",
    ]
    assert body["context"]["recent_message_count"] == 2
    assert body["messages"][-1]["content"] == "What is my name?"


async def test_debug_works_without_a_conversation(
    client: AsyncClient, session_factory
) -> None:
    """A brand new conversation has no history but still gets knowledge."""
    await seed_knowledge(session_factory)

    body = await debug(client, "What database does Mai use?")

    assert body["context"]["recent_message_count"] == 0
    assert body["context"]["memory_count"] >= 1
    assert PromptSection.CONVERSATION.value not in body["sections_included"]
    assert body["current_message_is_last"] is True


# --- What it must not expose ------------------------------------------------


async def test_debug_does_not_echo_the_system_instructions(
    client: AsyncClient, settings, session_factory
) -> None:
    """Application configuration is reported by size, not by content."""
    settings.MAI_SYSTEM_PROMPT = "SECRET-IMPLEMENTATION-DETAIL in the system prompt."
    await seed_knowledge(session_factory)

    body = await debug(client, "What database does Mai use?")

    instructions = [
        m
        for m in body["messages"]
        if m["section"] == PromptSection.SYSTEM_INSTRUCTIONS.value
    ]
    assert len(instructions) == 1
    assert instructions[0]["content"] is None
    assert instructions[0]["chars"] == len(settings.MAI_SYSTEM_PROMPT)
    assert "SECRET-IMPLEMENTATION-DETAIL" not in json.dumps(body)


async def test_debug_exposes_no_credentials_or_provider_settings(
    client: AsyncClient, settings, session_factory
) -> None:
    await seed_knowledge(session_factory)

    body = json.dumps(await debug(client, "What database does Mai use?"))

    assert settings.GROQ_API_KEY not in body
    assert "api_key" not in body.lower()
    assert "test-key" not in body
    assert "fake-model" not in body


async def test_debug_exposes_no_database_identifiers(
    client: AsyncClient, session_factory
) -> None:
    """The reference block the model sees carries no ids, and neither does this."""
    conversation_id = await seed_knowledge(session_factory)
    memories = (await client.get("/api/memories")).json()["items"]

    body = json.dumps(await debug(client, "What database does Mai use?", conversation_id))

    for memory in memories:
        assert memory["id"] not in body
