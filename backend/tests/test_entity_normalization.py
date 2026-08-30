"""Entity normalization and name validation.

Normalization is fully deterministic -- no model involved -- so these are the
tightest guarantees in the entity system.
"""

import pytest

from app.entities.normalizer import (
    clean_display_name,
    is_valid_name,
    normalize_name,
)
from app.entities.resolver import compact_form


# --- Case and whitespace ----------------------------------------------------


@pytest.mark.parametrize(
    "variants",
    [
        ["postgresql", "POSTGRESQL", "PostgreSQL", "  PostgreSQL  ", "PostgreSQL."],
        ["Mai", "mai", "MAI", " mai "],
        ["Artificial Intelligence", "artificial   intelligence", "ARTIFICIAL INTELLIGENCE"],
        ["Groq", "groq", "GROQ"],
    ],
)
def test_case_and_spacing_variants_normalize_together(variants) -> None:
    assert len({normalize_name(v) for v in variants}) == 1


# --- Descriptor and article stripping ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PostgreSQL database", "postgresql"),
        ("the Mai project", "mai"),
        ("Mai's", "mai"),
        ("The Groq platform", "groq"),
        ("Python language", "python"),
        ("FastAPI framework", "fastapi"),
    ],
)
def test_descriptors_and_articles_are_stripped(raw, expected) -> None:
    assert normalize_name(raw) == expected


def test_a_descriptor_that_is_the_whole_name_survives() -> None:
    """"Database" is itself a plausible entity; it must not vanish."""
    assert normalize_name("Database") == "database"
    assert is_valid_name("Database")


# --- Canonical case is preserved --------------------------------------------


@pytest.mark.parametrize(
    "name", ["PostgreSQL", "Mai", "Groq", "FastAPI", "AI Product Development"]
)
def test_display_name_keeps_its_capitalisation(name) -> None:
    """Canonical names must never be flattened to lowercase."""
    assert clean_display_name(name) == name
    assert normalize_name(name) == name.lower()


def test_display_name_is_tidied_without_recasing() -> None:
    assert clean_display_name("  PostgreSQL   database  ") == "PostgreSQL database"
    assert clean_display_name("«PostgreSQL»") == "PostgreSQL"


# --- Distinct entities must not collapse ------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Claude", "Claude Code"),
        ("Mai", "Mai Chen"),
        ("AI", "API"),
        ("Groq", "Grok"),
        ("PostgreSQL", "MySQL"),
        ("OpenAI", "Open Source"),
    ],
)
def test_different_entities_stay_distinct(left, right) -> None:
    """A false merge is worse than a duplicate."""
    assert normalize_name(left) != normalize_name(right)
    assert compact_form(left) != compact_form(right)


# --- Compact form -----------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [("PostgreSQL", "Postgre-SQL"), ("PostgreSQL", "Postgre SQL"), ("AI", "A.I.")],
)
def test_punctuation_variants_share_a_compact_form(left, right) -> None:
    assert compact_form(left) == compact_form(right)


# --- Validation -------------------------------------------------------------


@pytest.mark.parametrize("name", ["PostgreSQL", "Mai", "AI", "John Doe", "Database"])
def test_valid_names_are_accepted(name) -> None:
    assert is_valid_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "   ", "x", "-", "---", "2024", "42", "...", None, 123, "a" * 250],
)
def test_unusable_names_are_rejected(name) -> None:
    """Fragments, punctuation and bare numbers are not entities."""
    assert not is_valid_name(name)


def test_normalizing_is_idempotent() -> None:
    """Resolution depends on normalisation not drifting between runs."""
    for name in ["PostgreSQL database", "the Mai project", "  Groq  "]:
        once = normalize_name(name)
        assert normalize_name(once) == once
