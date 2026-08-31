"""End-to-end chat flow: persistence, context building, and error paths."""

import uuid

import pytest
from httpx import AsyncClient

from app.core.errors import LLMRateLimitError, LLMTimeoutError
from app.prompt.formatter import RUNTIME_FACTS_HEADER


async def test_send_message_stores_both_turns_and_returns_them(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.reply = "I am Mai."

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Who are you?"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["conversation_id"] == str(conversation_id)
    assert body["user_message"]["role"] == "user"
    assert body["user_message"]["content"] == "Who are you?"
    assert body["assistant_message"]["role"] == "assistant"
    assert body["assistant_message"]["content"] == "I am Mai."


async def test_messages_are_persisted_and_read_back_in_order(
    client: AsyncClient, conversation_id
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "First"}
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Second"}
    )

    body = (await client.get(f"/api/conversations/{conversation_id}")).json()

    roles_and_content = [(m["role"], m["content"]) for m in body["messages"]]
    assert roles_and_content == [
        ("user", "First"),
        ("assistant", "Hello from Mai."),
        ("user", "Second"),
        ("assistant", "Hello from Mai."),
    ]


async def test_context_includes_the_system_prompt_and_full_history(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "First"}
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Second"}
    )

    sent = fake_provider.last_call

    assert sent[0].role == "system"
    assert sent[0].content == "You are Mai."
    # Stage 4D.1 added a second system message: the authoritative facts block,
    # which sits immediately after the instructions and above everything
    # retrieved.
    assert sent[1].role == "system"
    assert RUNTIME_FACTS_HEADER in sent[1].content
    # instructions + facts + (user, assistant) + user
    assert [m.role for m in sent] == [
        "system", "system", "user", "assistant", "user",
    ]
    assert sent[-1].content == "Second"


async def test_context_is_scoped_to_one_conversation(
    client: AsyncClient, fake_provider
) -> None:
    """Stage 1 has no cross-conversation memory; verify none leaks in."""
    first = (await client.post("/api/conversations", json={})).json()["id"]
    second = (await client.post("/api/conversations", json={})).json()["id"]

    await client.post(
        f"/api/conversations/{first}/messages", json={"content": "secret-in-first"}
    )
    await client.post(
        f"/api/conversations/{second}/messages", json={"content": "hello-in-second"}
    )

    contents = [m.content for m in fake_provider.last_call]
    assert "secret-in-first" not in contents
    assert "hello-in-second" in contents


async def test_context_window_is_capped(
    client: AsyncClient, conversation_id, fake_provider, settings
) -> None:
    """MAX_CONTEXT_MESSAGES still bounds replayed history after Stage 3B.

    The window means something slightly different now. Stage 3B assembles
    context *before* the user's message is stored, so the cap applies to
    history alone and the current message is always sent on top of it -- it is
    the one thing no budget may drop. Before Stage 3B the current message was
    counted inside the window, so the same setting produced one fewer
    historical message.
    """
    settings.MAX_CONTEXT_MESSAGES = 4

    for index in range(5):
        await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": f"msg-{index}"},
        )

    sent = fake_provider.last_call
    # 2 system messages (instructions + runtime facts) + the 4 most recent
    # stored messages + the current one.
    assert len(sent) == 7
    assert sent[-1].content == "msg-4"
    # The cap is real: 8 messages were stored by the time of the last turn.
    assert len([m for m in sent if m.role != "system"]) == 5


async def test_first_message_titles_the_conversation(
    client: AsyncClient, conversation_id
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Plan my week"},
    )

    body = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert body["title"] == "Plan my week"


async def test_long_first_message_produces_a_truncated_title(
    client: AsyncClient, conversation_id
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "word " * 100},
    )

    title = (await client.get(f"/api/conversations/{conversation_id}")).json()["title"]
    assert len(title) <= 60
    assert title.endswith("…")


async def test_send_message_to_unknown_conversation_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.post(
        f"/api/conversations/{uuid.uuid4()}/messages", json={"content": "Hi"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


async def test_empty_message_is_rejected(
    client: AsyncClient, conversation_id
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": ""}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (LLMTimeoutError(), 504, "llm_timeout"),
        (LLMRateLimitError(), 429, "llm_rate_limited"),
    ],
)
async def test_llm_failures_map_to_useful_http_errors(
    client: AsyncClient, conversation_id, fake_provider, error, expected_status,
    expected_code,
) -> None:
    fake_provider.raise_error = error

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Hi"}
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert response.json()["error"]["request_id"]


async def test_a_failed_turn_leaves_no_partial_messages_behind(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """The user message must not survive a rolled-back turn."""
    fake_provider.raise_error = LLMTimeoutError()

    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "Hi"}
    )

    body = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert body["messages"] == []


async def test_conversation_list_orders_by_most_recent_activity(
    client: AsyncClient,
) -> None:
    older = (await client.post("/api/conversations", json={"title": "Older"})).json()["id"]
    newer = (await client.post("/api/conversations", json={"title": "Newer"})).json()["id"]

    # Activity on the older conversation should float it to the top.
    await client.post(f"/api/conversations/{older}/messages", json={"content": "Hi"})

    items = (await client.get("/api/conversations")).json()["items"]
    assert items[0]["id"] == older
    assert items[1]["id"] == newer
