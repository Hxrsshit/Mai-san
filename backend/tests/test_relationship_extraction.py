"""Relationship extraction: types, direction, normalization, validation."""

import json

import pytest

from app.core.errors import LLMTimeoutError
from app.relationships.extractor import RelationshipExtractor
from app.relationships.models import RelationshipType
from app.relationships.normalizer import normalize_type


def payload(*relationships) -> str:
    return json.dumps({"relationships": list(relationships)})


def candidate(source, rel_type, target, confidence=0.93):
    return {
        "source_entity": source,
        "relationship_type": rel_type,
        "target_entity": target,
        "confidence_score": confidence,
    }


ENTITIES = ["Mai", "PostgreSQL", "Groq", "User", "AI Product Development", "John Doe"]


# --- The canonical Stage 2C cases -------------------------------------------


@pytest.mark.parametrize(
    ("memory", "source", "rel", "target"),
    [
        ("Mai uses PostgreSQL as its primary database.",
         "Mai", RelationshipType.USES, "PostgreSQL"),
        ("User is building Mai.",
         "User", RelationshipType.BUILDS, "Mai"),
        ("User is interested in AI product development.",
         "User", RelationshipType.INTERESTED_IN, "AI Product Development"),
        ("John Doe is collaborating with the user on Mai.",
         "John Doe", RelationshipType.WORKS_WITH, "User"),
        ("Mai depends on Groq for inference.",
         "Mai", RelationshipType.DEPENDS_ON, "Groq"),
    ],
)
async def test_supported_relationships_are_extracted(
    fake_provider, settings, memory, source, rel, target
) -> None:
    fake_provider.relationship_reply = payload(candidate(source, rel.value, target))
    extractor = RelationshipExtractor(fake_provider, settings)

    candidates = await extractor.extract(memory, "decision", ENTITIES)

    assert len(candidates) == 1
    assert candidates[0].source_entity == source
    assert candidates[0].relationship_type is rel
    assert candidates[0].target_entity == target


# --- Direction --------------------------------------------------------------


async def test_direction_is_preserved_exactly(fake_provider, settings) -> None:
    """Mai USES PostgreSQL must never become PostgreSQL USES Mai."""
    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "PostgreSQL")
    )
    extractor = RelationshipExtractor(fake_provider, settings)

    result = (await extractor.extract("Mai uses PostgreSQL.", "decision", ENTITIES))[0]

    assert result.source_entity == "Mai"
    assert result.target_entity == "PostgreSQL"
    assert (result.source_entity, result.target_entity) != ("PostgreSQL", "Mai")


async def test_reversed_direction_is_a_distinct_candidate(
    fake_provider, settings
) -> None:
    """The two directions are different claims, not duplicates."""
    fake_provider.relationship_reply = payload(
        candidate("Mai", "DEPENDS_ON", "Groq"),
        candidate("Groq", "DEPENDS_ON", "Mai"),
    )
    extractor = RelationshipExtractor(fake_provider, settings)

    candidates = await extractor.extract("x", "semantic", ENTITIES)
    assert len(candidates) == 2


async def test_the_prompt_states_direction_and_lists_only_known_entities(
    fake_provider, settings
) -> None:
    extractor = RelationshipExtractor(fake_provider, settings)
    await extractor.extract("Mai uses PostgreSQL.", "decision", ["Mai", "PostgreSQL"])

    sent = fake_provider.last_relationship_call
    system, user = sent[0].content, sent[1].content

    assert "DIRECTION IS CRITICAL" in system
    assert "Never invent an entity" in system
    assert "AVAILABLE ENTITIES" in user
    assert "Mai" in user and "PostgreSQL" in user
    # Neither the chat nor the other extractors were called.
    assert fake_provider.calls == []
    assert fake_provider.entity_calls == []


# --- Normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UTILIZES", RelationshipType.USES),
        ("utilizes", RelationshipType.USES),
        ("runs on", RelationshipType.USES),
        ("powered by", RelationshipType.USES),
        ("is building", RelationshipType.BUILDS),
        ("collaborating with", RelationshipType.WORKS_WITH),
        ("wants to", RelationshipType.HAS_GOAL),
        ("lives in", RelationshipType.LOCATED_IN),
        ("exploring", RelationshipType.INTERESTED_IN),
        ("likes", RelationshipType.PREFERS),
    ],
)
async def test_synonym_phrasings_normalize_to_the_vocabulary(
    fake_provider, settings, raw, expected
) -> None:
    fake_provider.relationship_reply = payload(candidate("Mai", raw, "PostgreSQL"))
    extractor = RelationshipExtractor(fake_provider, settings)

    result = (await extractor.extract("x", "decision", ENTITIES))[0]
    assert result.relationship_type is expected


def test_unmappable_types_are_rejected_not_guessed() -> None:
    for raw in ["FROBNICATES", "", None, "   ", "???", "relates_somehow"]:
        assert normalize_type(raw) is None


# --- Validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {"source_entity": "Mai", "relationship_type": "FROBNICATES", "target_entity": "PostgreSQL"},
        {"source_entity": "Mai", "relationship_type": "", "target_entity": "PostgreSQL"},
        {"source_entity": "Mai", "target_entity": "PostgreSQL"},
        {"source_entity": "", "relationship_type": "USES", "target_entity": "PostgreSQL"},
        {"source_entity": "Mai", "relationship_type": "USES", "target_entity": ""},
        {"source_entity": "Mai", "relationship_type": "USES"},
        {"relationship_type": "USES", "target_entity": "PostgreSQL"},
        {"source_entity": "Mai", "relationship_type": "USES", "target_entity": "Mai"},
        {"source_entity": "Mai", "relationship_type": "USES", "target_entity": "mai"},
        {"source_entity": "Mai", "relationship_type": "USES", "target_entity": "PostgreSQL", "confidence_score": 1.5},
        {"source_entity": "Mai", "relationship_type": "USES", "target_entity": "PostgreSQL", "confidence_score": -0.2},
        {"source_entity": "Mai", "relationship_type": "USES", "target_entity": "PostgreSQL", "confidence_score": "sure"},
    ],
)
async def test_invalid_candidates_are_rejected(
    fake_provider, settings, entry
) -> None:
    fake_provider.relationship_reply = payload(entry)
    extractor = RelationshipExtractor(fake_provider, settings)

    assert await extractor.extract("x", "decision", ENTITIES) == []


async def test_valid_candidates_survive_alongside_invalid_ones(
    fake_provider, settings
) -> None:
    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "PostgreSQL"),
        {"source_entity": "Mai", "relationship_type": "FROBNICATES", "target_entity": "Groq"},
    )
    extractor = RelationshipExtractor(fake_provider, settings)

    candidates = await extractor.extract("x", "decision", ENTITIES)
    assert len(candidates) == 1
    assert candidates[0].target_entity == "PostgreSQL"


# --- Fewer than two entities ------------------------------------------------


@pytest.mark.parametrize("entities", [[], ["Mai"]])
async def test_extraction_is_skipped_without_two_entities(
    fake_provider, settings, entities
) -> None:
    """Nothing to relate: the model must not even be asked."""
    fake_provider.relationship_reply = payload(candidate("Mai", "USES", "PostgreSQL"))
    extractor = RelationshipExtractor(fake_provider, settings)

    assert await extractor.extract("User prefers concise answers.", "preference", entities) == []
    assert fake_provider.relationship_calls == []


# --- Malformed output -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "no relationships", '{"relationships": [', "[1,2,3]", "null",
     '{"relationships": "nope"}'],
)
async def test_malformed_output_yields_no_candidates(
    fake_provider, settings, raw
) -> None:
    fake_provider.relationship_reply = raw
    extractor = RelationshipExtractor(fake_provider, settings)

    assert await extractor.extract("x", "decision", ENTITIES) == []


async def test_fenced_and_prefixed_json_are_recovered(
    fake_provider, settings
) -> None:
    inner = payload(candidate("Mai", "USES", "PostgreSQL"))
    extractor = RelationshipExtractor(fake_provider, settings)

    fake_provider.relationship_reply = f"```json\n{inner}\n```"
    assert len(await extractor.extract("x", "decision", ENTITIES)) == 1

    fake_provider.relationship_reply = f"Here you go:\n{inner}"
    assert len(await extractor.extract("x", "decision", ENTITIES)) == 1


@pytest.mark.parametrize(
    "error", [LLMTimeoutError(), RuntimeError("boom"), ValueError("bad")]
)
async def test_extraction_failure_returns_no_candidates(
    fake_provider, settings, error
) -> None:
    fake_provider.relationship_error = error
    extractor = RelationshipExtractor(fake_provider, settings)

    assert await extractor.extract("x", "decision", ENTITIES) == []


# --- Batch handling ---------------------------------------------------------


async def test_duplicates_within_one_batch_collapse(fake_provider, settings) -> None:
    fake_provider.relationship_reply = payload(
        candidate("Mai", "USES", "PostgreSQL", 0.8),
        candidate("Mai", "utilizes", "PostgreSQL", 0.95),
        candidate("mai", "USES", "postgresql", 0.7),
    )
    extractor = RelationshipExtractor(fake_provider, settings)

    candidates = await extractor.extract("x", "decision", ENTITIES)
    assert len(candidates) == 1
    # The most confident wins.
    assert candidates[0].confidence_score == pytest.approx(0.95)


async def test_over_extraction_is_capped(fake_provider, settings) -> None:
    settings.RELATIONSHIP_EXTRACTION_MAX_PER_MEMORY = 2
    fake_provider.relationship_reply = payload(
        *[candidate("Mai", "USES", f"Thing Number {i}") for i in range(7)]
    )
    extractor = RelationshipExtractor(fake_provider, settings)

    assert len(await extractor.extract("x", "decision", ENTITIES)) == 2


async def test_unknown_fields_from_the_model_are_dropped(
    fake_provider, settings
) -> None:
    """Stage 2D fields must not sneak in early."""
    entry = candidate("Mai", "USES", "PostgreSQL")
    entry["embedding"] = [0.1, 0.2]
    entry["weight"] = 3
    fake_provider.relationship_reply = payload(entry)
    extractor = RelationshipExtractor(fake_provider, settings)

    result = (await extractor.extract("x", "decision", ENTITIES))[0]
    assert not hasattr(result, "embedding")
    assert not hasattr(result, "weight")
