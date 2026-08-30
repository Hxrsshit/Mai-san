"""Stage 3C: the deterministic conflict rules, in isolation.

Pure functions, no database. These are the rules everything else is built on,
so they are tested exhaustively -- including the cases where the correct answer
is "no conflict", which is most of them.
"""

import pytest

from app.knowledge import policies
from app.relationships.models import RelationshipType


# --- Relationship policies --------------------------------------------------


def test_uses_is_not_exclusive() -> None:
    """The single most important policy decision in Stage 3C.

    `Mai USES PostgreSQL` and `Mai USES Groq` are both true at once. If USES
    were exclusive, storing the second would retire the first and Mai would
    forget its own database.
    """
    assert not policies.is_exclusive(RelationshipType.USES)


@pytest.mark.parametrize(
    "relationship_type",
    [
        RelationshipType.USES,
        RelationshipType.INTERESTED_IN,
        RelationshipType.BUILDS,
        RelationshipType.WORKS_ON,
        RelationshipType.WORKS_WITH,
        RelationshipType.OWNS,
        RelationshipType.DEPENDS_ON,
        RelationshipType.HAS_GOAL,
        RelationshipType.CREATED,
        RelationshipType.RELATED_TO,
    ],
)
def test_types_that_allow_many_targets(relationship_type) -> None:
    assert not policies.is_exclusive(relationship_type)


@pytest.mark.parametrize(
    "relationship_type",
    [RelationshipType.PREFERS, RelationshipType.LOCATED_IN],
)
def test_types_where_a_second_target_is_worth_flagging(relationship_type) -> None:
    assert policies.is_exclusive(relationship_type)


def test_every_relationship_type_has_a_policy() -> None:
    """A new relationship type must be classified, not silently defaulted."""
    classified = (
        policies.EXCLUSIVE_RELATIONSHIP_TYPES
        | policies.NON_EXCLUSIVE_RELATIONSHIP_TYPES
    )
    assert classified == set(RelationshipType)
    assert not (
        policies.EXCLUSIVE_RELATIONSHIP_TYPES
        & policies.NON_EXCLUSIVE_RELATIONSHIP_TYPES
    )


# --- Replacement language ---------------------------------------------------


@pytest.mark.parametrize(
    "text,old,new",
    [
        ("user switched from openrouter to groq", "openrouter", "groq"),
        ("user switched from openrouter to groq for inference", "openrouter", "groq"),
        ("i migrated mai from openrouter to groq", "openrouter", "groq"),
        ("user moved from remote work to hybrid work", "remote work", "hybrid work"),
        ("user replaced openrouter with groq", "openrouter", "groq"),
        ("user changed from sqlite to postgresql in mai", "sqlite", "postgresql"),
        ("mai uses groq instead of openrouter", "openrouter", "groq"),
        ("user prefers hybrid instead of remote work", "remote work", "hybrid"),
    ],
)
def test_replacement_language_is_recognised(text, old, new) -> None:
    found = policies.find_replacements(text)
    assert found, f"no replacement found in {text!r}"
    assert (found[0].old, found[0].new) == (old, new)


@pytest.mark.parametrize(
    "text",
    [
        # Plain statements of fact. No replacement is claimed.
        "user selected postgresql for local storage in mai",
        "mai uses groq for fast inference",
        "user is building mai as a personal ai environment",
        "user is interested in ai and robotics",
        "user likes python",
        "user enjoys javascript",
        "user uses postgresql",
        "user uses sqlite for tests",
        # Superficially similar wording that claims nothing.
        "user asked about the difference from openrouter to groq pricing",
        "user moved to bangalore",
        "",
    ],
)
def test_ordinary_statements_claim_no_replacement(text) -> None:
    """False conflicts are worse than missed ones. These must all abstain."""
    assert policies.find_replacements(text) == []


@pytest.mark.parametrize(
    "text,dropped",
    [
        ("user no longer uses openrouter", "openrouter"),
        ("user no longer prefers remote work", "remote work"),
        ("user stopped using openrouter for inference", "openrouter"),
    ],
)
def test_abandonment_language_is_recognised(text, dropped) -> None:
    found = policies.find_abandonments(text)
    assert found and found[0] == dropped


@pytest.mark.parametrize(
    "text",
    ["user uses openrouter", "user longer than expected", "user stopped by the office"],
)
def test_abandonment_is_not_over_read(text) -> None:
    assert policies.find_abandonments(text) == []


# --- Currency markers -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["user now prefers hybrid work", "user currently lives in bangalore"],
)
def test_present_tense_statements_are_recognised(text) -> None:
    assert policies.states_the_present(text)


@pytest.mark.parametrize("text", ["user prefers hybrid work", "user lives in bangalore", ""])
def test_a_bare_statement_does_not_claim_the_present(text) -> None:
    assert not policies.states_the_present(text)


# --- Fragment resolution ----------------------------------------------------


def test_candidate_names_offers_longest_first() -> None:
    """"openrouter for inference" must still be able to resolve to the entity."""
    assert policies.candidate_names("openrouter for inference") == [
        "openrouter for inference",
        "openrouter for",
        "openrouter",
    ]


def test_candidate_names_handles_an_empty_fragment() -> None:
    assert policies.candidate_names("") == []


# --- Temporal progress ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "user completed project a",
        "user finished the migration",
        "user shipped the first release",
        "user paused work on project a",
    ],
)
def test_progress_reports_are_recognised(text) -> None:
    """Recognised only so detection can abstain, never to create a conflict."""
    assert policies.looks_like_temporal_progress(text)


@pytest.mark.parametrize(
    "text", ["user is working on project a", "user switched from openrouter to groq"]
)
def test_non_progress_statements_are_not_flagged(text) -> None:
    assert not policies.looks_like_temporal_progress(text)


# --- Structural safety ------------------------------------------------------


def test_policies_make_no_model_call_and_touch_no_database() -> None:
    import ast
    import pathlib

    path = pathlib.Path(policies.__file__)
    tree = ast.parse(path.read_text())
    banned = ("app.llm", "sqlalchemy", "app.database")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not any(
                name == item or name.startswith(item + ".") for item in banned
            ), f"policies.py imports {name}"
