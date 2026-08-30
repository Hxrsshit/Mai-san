"""Stage 3B: what the request path records, and what it must never record."""

import logging

import pytest
from httpx import AsyncClient

from tests.test_retrieval_integration import NOTHING_TO_STORE, seed_knowledge


@pytest.fixture
def chat_logs(caplog):
    """Capture the chat service's structured log records at INFO."""
    caplog.set_level(logging.INFO)
    return caplog


def record_named(caplog, message: str):
    for record in caplog.records:
        if record.getMessage() == message:
            return record
    raise AssertionError(
        f"no log record {message!r}; saw {[r.getMessage() for r in caplog.records]}"
    )


# --- Metrics ----------------------------------------------------------------


async def test_the_turn_records_prompt_size_and_latency(
    client: AsyncClient, fake_provider, session_factory, chat_logs
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    record = record_named(chat_logs, "Chat turn started")

    assert record.prompt_messages >= 3
    assert record.memories >= 1
    assert record.entities >= 1
    assert record.relationships >= 1
    assert record.reference_chars > 0
    assert record.prompt_chars > 0
    assert record.fallback_prompt is False

    # Latency, attributable per stage.
    assert record.assembly_ms >= 0
    assert record.format_ms >= 0
    assert record.pre_llm_ms >= 0


async def test_the_one_call_guarantee_is_recorded(
    client: AsyncClient, conversation_id, fake_provider, chat_logs
) -> None:
    """The invariant is asserted in the log, not only in the tests."""
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello."},
    )

    record = record_named(chat_logs, "Chat turn started")
    # Stage 4A split this counter in two. Generation stays at exactly one --
    # the Stage 3B guarantee -- and classification is counted separately so
    # the two bounds can be checked independently.
    assert record.request_path_generation_calls == 1
    assert record.request_path_classification_calls <= 1
    assert len(fake_provider.calls) == 1


async def test_a_fallback_prompt_is_visible_in_the_logs(
    client: AsyncClient, conversation_id, fake_provider, chat_logs, monkeypatch
) -> None:
    """A degraded turn must not look like a healthy one."""

    def boom(self, package):
        raise RuntimeError("formatting is broken")

    monkeypatch.setattr("app.prompt.formatter.PromptFormatter.format", boom)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "Hello."},
    )

    assert response.status_code == 201
    assert record_named(chat_logs, "Chat turn started").fallback_prompt is True
    assert any(
        "Prompt formatting failed" in record.getMessage()
        for record in chat_logs.records
    ), "the failure was swallowed silently"


async def test_context_assembly_still_reports_its_own_metrics(
    client: AsyncClient, fake_provider, session_factory, chat_logs
) -> None:
    """Stage 3A and Stage 2D logging survives the Stage 3B refactor."""
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    assembled = record_named(chat_logs, "Context assembled")
    assert assembled.memories >= 1
    assert assembled.duration_ms >= 0

    retrieved = record_named(chat_logs, "Context retrieval completed")
    assert retrieved.candidate_memories >= 1
    assert retrieved.selected_memories >= 1


# --- Secrets ----------------------------------------------------------------


async def test_no_secret_reaches_the_logs(
    client: AsyncClient, fake_provider, session_factory, settings, chat_logs
) -> None:
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    everything = "\n".join(
        [record.getMessage() for record in chat_logs.records]
        + [str(record.__dict__) for record in chat_logs.records]
    )

    assert settings.GROQ_API_KEY not in everything
    assert "test-key" not in everything
    assert "api_key" not in everything.lower()
    assert settings.DATABASE_URL not in everything


async def test_the_prompt_body_is_not_logged(
    client: AsyncClient, fake_provider, session_factory, chat_logs
) -> None:
    """Sizes and counts are logged; the prompt text itself is not.

    Retrieved memories are personal data. Recording their content in every
    request log would put it somewhere with a different retention policy and
    a much wider audience than the database it came from.
    """
    conversation_id = await seed_knowledge(session_factory)
    fake_provider.extraction_reply = NOTHING_TO_STORE

    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "What database does Mai use?"},
    )

    everything = "\n".join(
        str(record.__dict__) for record in chat_logs.records
    )

    assert "User selected PostgreSQL for local storage in Mai." not in everything
    assert "REFERENCE KNOWLEDGE" not in everything
