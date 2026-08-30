"""Failure isolation and the Stage 2A acceptance scenarios.

The governing rule: memory extraction runs after the chat turn has already
been answered, so nothing it does may change what the user receives.
"""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.errors import (
    LLMAuthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.memory.models import Memory


def payload(*memories) -> str:
    return json.dumps(
        {"should_store_memory": bool(memories), "memories": list(memories)}
    )


def candidate(content, memory_type="preference", importance=8, confidence=0.92) -> dict:
    return {
        "content": content,
        "memory_type": memory_type,
        "importance_score": importance,
        "confidence_score": confidence,
    }


async def memory_count(session) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(Memory))).scalar_one()
    )


# --- Failure isolation ------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        LLMTimeoutError(),
        LLMRateLimitError(),
        LLMAuthError(),
        LLMResponseError(),
        RuntimeError("extraction blew up"),
        ValueError("unexpected"),
    ],
)
async def test_extraction_failure_never_breaks_chat(
    client: AsyncClient, conversation_id, fake_provider, db_session, error
) -> None:
    """The user's turn must succeed regardless of what extraction does."""
    fake_provider.reply = "Here is your answer."
    fake_provider.extraction_error = error

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise explanations."},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["assistant_message"]["content"] == "Here is your answer."
    assert body["user_message"]["content"] == "I prefer concise explanations."
    assert await memory_count(db_session) == 0


async def test_conversation_survives_extraction_failure(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_error = LLMTimeoutError()

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise explanations."},
    )

    stored = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert len(stored["messages"]) == 2
    assert stored["messages"][0]["content"] == "I prefer concise explanations."


@pytest.mark.parametrize(
    "garbage",
    ["", "not json at all", '{"memories": [', "[1,2,3]", '{"memories":"nope"}'],
)
async def test_malformed_extraction_output_stores_nothing(
    client: AsyncClient, conversation_id, fake_provider, db_session, garbage
) -> None:
    fake_provider.extraction_reply = garbage

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise explanations."},
    )

    assert response.status_code == 201
    assert await memory_count(db_session) == 0


async def test_invalid_candidate_never_reaches_the_database(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    fake_provider.extraction_reply = payload(
        {
            "content": "User prefers concise explanations.",
            "memory_type": "not_a_real_type",
            "importance_score": 99,
            "confidence_score": 7.5,
        }
    )

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise explanations."},
    )

    assert response.status_code == 201
    assert await memory_count(db_session) == 0


async def test_chat_response_is_identical_with_and_without_memory(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    """Memory must not alter the user-visible response in any way."""
    fake_provider.reply = "A deterministic reply."
    fake_provider.extraction_reply = payload(
        candidate("User prefers concise explanations.")
    )
    with_memory = (
        await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "First message."},
        )
    ).json()

    settings.MEMORY_EXTRACTION_ENABLED = False
    without_memory = (
        await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "Second message."},
        )
    ).json()

    assert (
        with_memory["assistant_message"]["content"]
        == without_memory["assistant_message"]["content"]
    )
    assert set(with_memory) == set(without_memory)


async def test_extraction_is_skipped_when_disabled(
    client: AsyncClient, conversation_id, fake_provider, settings, db_session
) -> None:
    settings.MEMORY_EXTRACTION_ENABLED = False
    fake_provider.extraction_reply = payload(candidate("User prefers concise answers."))

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise answers."},
    )

    assert response.status_code == 201
    assert fake_provider.extraction_calls == []
    assert await memory_count(db_session) == 0


async def test_failed_chat_turn_triggers_no_extraction(
    client: AsyncClient, conversation_id, fake_provider, db_session
) -> None:
    """No memory may be derived from a turn that never completed."""
    fake_provider.raise_error = LLMTimeoutError()

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise explanations."},
    )

    assert response.status_code == 504
    assert fake_provider.extraction_calls == []
    assert await memory_count(db_session) == 0


# --- Stage 2A acceptance scenarios ------------------------------------------


async def test_acceptance_goal_is_remembered(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = payload(
        candidate(
            "User wants to transition their career toward AI product development.",
            "goal",
            9,
            0.95,
        )
    )

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I want to transition my career toward AI product development."},
    )
    assert response.status_code == 201

    memories = (await client.get("/api/memories")).json()
    assert memories["total"] == 1
    stored = memories["items"][0]
    assert stored["memory_type"] == "goal"
    assert stored["importance_score"] >= 7
    assert stored["confidence_score"] >= 0.9
    assert "AI product development" in stored["content"]


async def test_acceptance_small_talk_is_not_remembered(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = payload()  # model correctly returns nothing

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello, how are you?"},
    )

    assert response.status_code == 201
    assert (await client.get("/api/memories")).json()["total"] == 0


async def test_acceptance_preference_is_remembered(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = payload(
        candidate(
            "User prefers concise answers with practical examples.",
            "preference",
            7,
            0.93,
        )
    )

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I prefer concise answers with practical examples."},
    )

    stored = (await client.get("/api/memories")).json()["items"][0]
    assert stored["memory_type"] == "preference"
    assert "concise" in stored["content"]


async def test_acceptance_extraction_failure_is_isolated(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """Scenario 4: extraction API fails; chat and storage are unaffected."""
    fake_provider.reply = "Sure, I can help with that."
    fake_provider.extraction_error = LLMTimeoutError()

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "I have decided to use PostgreSQL for Mai."},
    )

    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == "Sure, I can help with that."

    conversation = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert len(conversation["messages"]) == 2
    assert (await client.get("/api/memories")).json()["total"] == 0
