"""Entity extraction: what gets recognised, and what must not."""

import json

import pytest

from app.core.errors import LLMTimeoutError
from app.entities.extractor import EntityExtractor
from app.entities.models import EntityType


def payload(*entities) -> str:
    return json.dumps({"entities": list(entities)})


def candidate(name, entity_type, description=None, aliases=None, confidence=0.95):
    entry = {
        "name": name,
        "entity_type": entity_type,
        "confidence_score": confidence,
    }
    if description is not None:
        entry["description"] = description
    if aliases is not None:
        entry["aliases"] = aliases
    return entry


# --- The canonical Stage 2B cases -------------------------------------------


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        (
            "User decided to use PostgreSQL for Mai.",
            [("PostgreSQL", EntityType.TECHNOLOGY), ("Mai", EntityType.PROJECT)],
        ),
        (
            "User is experimenting with Groq for inference.",
            [("Groq", EntityType.COMPANY)],
        ),
        (
            "User is building a personal AI project called Mai.",
            [("Mai", EntityType.PROJECT)],
        ),
        (
            "John Doe is collaborating with the user on Mai.",
            [("John Doe", EntityType.PERSON), ("Mai", EntityType.PROJECT)],
        ),
        (
            "User is interested in venture capital and AI product development.",
            [
                ("Venture Capital", EntityType.CONCEPT),
                ("AI Product Development", EntityType.CONCEPT),
            ],
        ),
    ],
)
async def test_meaningful_entities_are_extracted(
    fake_provider, settings, memory, expected
) -> None:
    fake_provider.entity_reply = payload(
        *[candidate(name, kind.value) for name, kind in expected]
    )
    extractor = EntityExtractor(fake_provider, settings)

    candidates = await extractor.extract(memory, "decision")

    assert [(c.name, c.entity_type) for c in candidates] == expected


async def test_generic_nouns_produce_nothing(fake_provider, settings) -> None:
    """"User likes working on interesting projects." names nothing."""
    fake_provider.entity_reply = payload()
    extractor = EntityExtractor(fake_provider, settings)

    assert await extractor.extract(
        "User likes working on interesting projects.", "preference"
    ) == []


async def test_only_the_memory_is_sent_not_the_conversation(
    fake_provider, settings
) -> None:
    extractor = EntityExtractor(fake_provider, settings)
    await extractor.extract("User decided to use PostgreSQL for Mai.", "decision")

    sent = fake_provider.last_entity_call
    system, user = sent[0].content, sent[1].content

    assert "User decided to use PostgreSQL for Mai." in user
    assert "MEMORY (decision)" in user
    # The classification rules reach the model, so its labels match the docs.
    assert "identifiable entities" in system
    assert "technology" in system and "concept" in system
    # No chat call was made.
    assert fake_provider.calls == []


# --- Failure handling -------------------------------------------------------


@pytest.mark.parametrize(
    "error", [LLMTimeoutError(), RuntimeError("boom"), ValueError("bad")]
)
async def test_extraction_failure_returns_no_candidates(
    fake_provider, settings, error
) -> None:
    fake_provider.entity_error = error
    extractor = EntityExtractor(fake_provider, settings)

    assert await extractor.extract("User uses PostgreSQL.", "decision") == []


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "no entities here", '{"entities": [', "[1,2,3]", "null",
     '{"entities": "not-a-list"}'],
)
async def test_malformed_output_yields_no_candidates(
    fake_provider, settings, raw
) -> None:
    fake_provider.entity_reply = raw
    extractor = EntityExtractor(fake_provider, settings)

    assert await extractor.extract("User uses PostgreSQL.", "decision") == []


async def test_markdown_fenced_json_is_recovered(fake_provider, settings) -> None:
    inner = payload(candidate("PostgreSQL", "technology"))
    fake_provider.entity_reply = f"```json\n{inner}\n```"
    extractor = EntityExtractor(fake_provider, settings)

    assert len(await extractor.extract("User uses PostgreSQL.", "decision")) == 1


async def test_json_after_prose_is_recovered(fake_provider, settings) -> None:
    inner = payload(candidate("PostgreSQL", "technology"))
    fake_provider.entity_reply = f"Sure, here you go:\n{inner}"
    extractor = EntityExtractor(fake_provider, settings)

    assert len(await extractor.extract("User uses PostgreSQL.", "decision")) == 1


# --- Invalid candidates -----------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_type", "database"),
        ("entity_type", "relationship"),
        ("entity_type", ""),
        ("name", ""),
        ("name", "   "),
        ("name", "x"),
        ("name", "2024"),
        ("name", "---"),
        ("confidence_score", -0.1),
        ("confidence_score", 1.5),
        ("confidence_score", "sure"),
    ],
)
async def test_invalid_candidates_are_rejected(
    fake_provider, settings, field, value
) -> None:
    bad = candidate("PostgreSQL", "technology")
    bad[field] = value
    fake_provider.entity_reply = payload(bad)
    extractor = EntityExtractor(fake_provider, settings)

    assert await extractor.extract("User uses PostgreSQL.", "decision") == []


async def test_valid_candidates_survive_alongside_invalid_ones(
    fake_provider, settings
) -> None:
    fake_provider.entity_reply = payload(
        candidate("PostgreSQL", "technology"),
        candidate("junk", "not_a_type"),
    )
    extractor = EntityExtractor(fake_provider, settings)

    candidates = await extractor.extract("User uses PostgreSQL.", "decision")
    assert [c.name for c in candidates] == ["PostgreSQL"]


async def test_over_extraction_is_capped(fake_provider, settings) -> None:
    settings.ENTITY_EXTRACTION_MAX_PER_MEMORY = 3
    fake_provider.entity_reply = payload(
        *[candidate(f"Entity Number {i}", "concept") for i in range(9)]
    )
    extractor = EntityExtractor(fake_provider, settings)

    assert len(await extractor.extract("Lots of things.", "semantic")) == 3


async def test_unknown_fields_from_the_model_are_dropped(
    fake_provider, settings
) -> None:
    """Stage 2C/2D fields must not sneak in early."""
    entry = candidate("PostgreSQL", "technology")
    entry["relationships"] = [{"target": "Mai", "type": "used_by"}]
    entry["embedding"] = [0.1, 0.2]
    fake_provider.entity_reply = payload(entry)
    extractor = EntityExtractor(fake_provider, settings)

    candidates = await extractor.extract("User uses PostgreSQL.", "decision")
    assert len(candidates) == 1
    assert not hasattr(candidates[0], "relationships")
    assert not hasattr(candidates[0], "embedding")


# --- Descriptions and aliases -----------------------------------------------


async def test_description_is_kept_when_supplied(fake_provider, settings) -> None:
    fake_provider.entity_reply = payload(
        candidate("Mai", "project", description="User's personal AI environment.")
    )
    extractor = EntityExtractor(fake_provider, settings)

    candidates = await extractor.extract("User builds Mai.", "semantic")
    assert candidates[0].description == "User's personal AI environment."


@pytest.mark.parametrize("placeholder", ["null", "none", "N/A", "unknown", "...", "  "])
async def test_placeholder_descriptions_become_none(
    fake_provider, settings, placeholder
) -> None:
    fake_provider.entity_reply = payload(
        candidate("Mai", "project", description=placeholder)
    )
    extractor = EntityExtractor(fake_provider, settings)

    assert (await extractor.extract("User builds Mai.", "semantic"))[0].description is None


async def test_aliases_are_cleaned_and_deduplicated(fake_provider, settings) -> None:
    fake_provider.entity_reply = payload(
        candidate(
            "PostgreSQL",
            "technology",
            aliases=["postgres", "Postgres", "PostgreSQL", "x", "", "postgres"],
        )
    )
    extractor = EntityExtractor(fake_provider, settings)

    aliases = (await extractor.extract("User uses PostgreSQL.", "decision"))[0].aliases
    # "Postgres"/"postgres" collapse, "PostgreSQL" duplicates the name, and
    # "x"/"" are not usable names.
    assert aliases == ["postgres"]


async def test_alias_list_is_bounded(fake_provider, settings) -> None:
    fake_provider.entity_reply = payload(
        candidate(
            "PostgreSQL",
            "technology",
            aliases=[f"Alias Number {i}" for i in range(20)],
        )
    )
    extractor = EntityExtractor(fake_provider, settings)

    assert len((await extractor.extract("x", "semantic"))[0].aliases) <= 5
