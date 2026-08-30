"""Memory extraction: what gets remembered, and what must not."""

import json

import pytest

from app.core.errors import LLMTimeoutError
from app.memory.extractor import MemoryExtractor
from app.memory.models import MemoryType


def extraction_payload(*memories) -> str:
    return json.dumps(
        {"should_store_memory": bool(memories), "memories": list(memories)}
    )


def candidate(content, memory_type, importance=8, confidence=0.92) -> dict:
    return {
        "content": content,
        "memory_type": memory_type,
        "importance_score": importance,
        "confidence_score": confidence,
    }


# --- The four canonical Stage 2A cases --------------------------------------


@pytest.mark.parametrize(
    ("user_text", "content", "expected_type"),
    [
        (
            "I want to transition my career toward AI product development.",
            "User wants to transition their career toward AI product development.",
            MemoryType.GOAL,
        ),
        (
            "I prefer concise and practical explanations.",
            "User prefers concise answers with practical examples.",
            MemoryType.PREFERENCE,
        ),
        (
            "I've decided to use PostgreSQL for Mai.",
            "User decided to use PostgreSQL as Mai's database.",
            MemoryType.DECISION,
        ),
        (
            "I work on AI-related projects most days.",
            "User works on AI-related projects.",
            MemoryType.SEMANTIC,
        ),
        (
            "I finished Stage 1 of Mai today.",
            "User completed Stage 1 of the Mai project.",
            MemoryType.EPISODIC,
        ),
    ],
)
async def test_meaningful_input_produces_the_right_memory_type(
    fake_provider, settings, user_text, content, expected_type
) -> None:
    fake_provider.extraction_reply = extraction_payload(
        candidate(content, expected_type.value)
    )
    extractor = MemoryExtractor(fake_provider, settings)

    candidates = await extractor.extract(user_text, "Understood.")

    assert len(candidates) == 1
    assert candidates[0].memory_type is expected_type
    assert candidates[0].content == content


async def test_trivial_input_produces_no_memory(fake_provider, settings) -> None:
    """The model returning nothing is the expected outcome for small talk."""
    fake_provider.extraction_reply = extraction_payload()
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("Hello!", "Hi there!") == []


async def test_should_store_false_is_respected(fake_provider, settings) -> None:
    """A populated list is ignored when the model says not to store."""
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": False,
            "memories": [candidate("User likes coffee.", "semantic")],
        }
    )
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("Hello!", "Hi!") == []


# --- The prompt must not let the assistant invent facts ---------------------


async def test_extraction_labels_roles_and_sends_json_mode(
    fake_provider, settings
) -> None:
    extractor = MemoryExtractor(fake_provider, settings)
    await extractor.extract("I like Python.", "You are clearly a Python expert.")

    sent = fake_provider.last_extraction_call
    system, user = sent[0].content, sent[1].content

    # The chat provider must not have been called at all.
    assert fake_provider.calls == []
    # Roles are explicitly separated so the model cannot confuse who said what.
    assert "USER MESSAGE" in user and "ASSISTANT REPLY" in user
    assert "I like Python." in user
    assert "never a source of facts" in user
    assert "never inferred from the assistant" in system


# --- Failure handling: extraction must never raise --------------------------


async def test_llm_failure_returns_no_candidates(fake_provider, settings) -> None:
    fake_provider.extraction_error = LLMTimeoutError()
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("I prefer X.", "Noted.") == []


async def test_unexpected_provider_error_is_contained(fake_provider, settings) -> None:
    fake_provider.extraction_error = RuntimeError("provider exploded")
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("I prefer X.", "Noted.") == []


# --- Malformed model output --------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "I could not find any memories.",
        '{"should_store_memory": true, "memories": [',
        "[1, 2, 3]",
        '{"memories": "not-a-list"}',
        "null",
    ],
)
async def test_malformed_output_yields_no_candidates(
    fake_provider, settings, raw
) -> None:
    fake_provider.extraction_reply = raw
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("I prefer X.", "Noted.") == []


async def test_markdown_fenced_json_is_recovered(fake_provider, settings) -> None:
    inner = extraction_payload(candidate("User prefers dark mode.", "preference"))
    fake_provider.extraction_reply = f"```json\n{inner}\n```"
    extractor = MemoryExtractor(fake_provider, settings)

    assert len(await extractor.extract("I prefer dark mode.", "Noted.")) == 1


async def test_json_after_prose_is_recovered(fake_provider, settings) -> None:
    inner = extraction_payload(candidate("User prefers dark mode.", "preference"))
    fake_provider.extraction_reply = f"Here you go:\n{inner}"
    extractor = MemoryExtractor(fake_provider, settings)

    assert len(await extractor.extract("I prefer dark mode.", "Noted.")) == 1


# --- Invalid candidates must never survive ----------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_type", "invented_type"),
        ("memory_type", ""),
        ("importance_score", 0),
        ("importance_score", 11),
        ("importance_score", -3),
        ("importance_score", "high"),
        ("confidence_score", -0.1),
        ("confidence_score", 1.5),
        ("confidence_score", "very sure"),
        ("content", ""),
        ("content", "  "),
        ("content", "hi"),
    ],
)
async def test_invalid_candidate_fields_are_rejected(
    fake_provider, settings, field, value
) -> None:
    bad = candidate("User prefers concise explanations.", "preference")
    bad[field] = value
    fake_provider.extraction_reply = extraction_payload(bad)
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("I prefer X.", "Noted.") == []


async def test_missing_content_field_is_rejected(fake_provider, settings) -> None:
    bad = candidate("x", "preference")
    del bad["content"]
    fake_provider.extraction_reply = extraction_payload(bad)
    extractor = MemoryExtractor(fake_provider, settings)

    assert await extractor.extract("I prefer X.", "Noted.") == []


async def test_valid_candidates_survive_alongside_invalid_ones(
    fake_provider, settings
) -> None:
    """One bad entry must not discard the whole batch."""
    good = candidate("User prefers concise explanations.", "preference")
    bad = candidate("User likes things.", "not_a_type")
    fake_provider.extraction_reply = extraction_payload(good, bad)
    extractor = MemoryExtractor(fake_provider, settings)

    candidates = await extractor.extract("I prefer X.", "Noted.")

    assert len(candidates) == 1
    assert candidates[0].content == "User prefers concise explanations."


async def test_over_extraction_is_capped(fake_provider, settings) -> None:
    """A long list signals the model is over-extracting."""
    many = [candidate(f"User fact number {i} about work.", "semantic") for i in range(9)]
    fake_provider.extraction_reply = extraction_payload(*many)
    extractor = MemoryExtractor(fake_provider, settings)

    assert len(await extractor.extract("Lots of things.", "Noted.")) == 5


async def test_unknown_fields_from_the_model_are_dropped(
    fake_provider, settings
) -> None:
    entry = candidate("User prefers concise explanations.", "preference")
    entry["entity_ids"] = ["should-not-exist-yet"]
    entry["embedding"] = [0.1, 0.2]
    fake_provider.extraction_reply = extraction_payload(entry)
    extractor = MemoryExtractor(fake_provider, settings)

    candidates = await extractor.extract("I prefer X.", "Noted.")

    assert len(candidates) == 1
    assert not hasattr(candidates[0], "embedding")
