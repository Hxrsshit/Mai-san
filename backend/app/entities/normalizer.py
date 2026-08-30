"""Deterministic entity-name normalization.

Two forms are kept for every entity:

- **canonical_name** -- the display form, case preserved: "PostgreSQL".
- **normalized_name** -- the matching form: "postgresql".

Only the second is lowercased. Canonical names must never be flattened, or
"PostgreSQL" degrades to "postgresql" in every future display.

Nothing here calls a model. Normalization must be reproducible: the same input
always yields the same output, so resolution cannot drift between runs.
"""

import re
import unicodedata
from typing import Optional

# Leading articles carry no identity: "the Mai project" and "Mai project"
# are the same thing.
_LEADING_ARTICLES = ("the ", "a ", "an ")

# Trailing nouns that describe the *kind* of thing rather than its identity.
# "PostgreSQL database" and "PostgreSQL" are the same entity.
_TRAILING_DESCRIPTORS = (
    " database",
    " project",
    " platform",
    " library",
    " framework",
    " language",
    " company",
    " corporation",
    " application",
    " app",
    " tool",
    " service",
    " system",
)

_PUNCTUATION_EDGES = re.compile(r"^[^\w(]+|[^\w)]+$")
_WHITESPACE = re.compile(r"\s+")

# An entity name has to be substantial enough to identify something.
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 200


def clean_display_name(raw: str) -> str:
    """Tidy a name for display without changing its case.

    Trims, collapses internal whitespace, and strips edge punctuation. This is
    what gets stored as `canonical_name`.
    """
    text = unicodedata.normalize("NFKC", raw).strip()
    text = _WHITESPACE.sub(" ", text)
    text = _PUNCTUATION_EDGES.sub("", text)
    return text.strip()


def normalize_name(raw: str) -> str:
    """Produce the matching form used for resolution.

    Lowercases, strips edge punctuation, drops a leading article, and removes a
    trailing descriptor noun. Applied to both entity names and aliases so the
    two are directly comparable.
    """
    text = clean_display_name(raw).lower()
    if not text:
        return ""

    for article in _LEADING_ARTICLES:
        if text.startswith(article):
            text = text[len(article) :]
            break

    # Only strip a descriptor if something identifying remains: the entity
    # "Database" must not normalise to the empty string.
    for descriptor in _TRAILING_DESCRIPTORS:
        if text.endswith(descriptor):
            stripped = text[: -len(descriptor)].strip()
            if len(stripped) >= MIN_NAME_LENGTH:
                text = stripped
            break

    # Possessives add nothing: "Mai's" and "Mai" are one entity.
    if text.endswith("'s") or text.endswith("’s"):
        text = text[:-2].strip()

    return _WHITESPACE.sub(" ", text).strip()


def is_valid_name(raw: Optional[str]) -> bool:
    """Reject names that cannot identify anything.

    Guards against the model returning fragments, pure punctuation, bare
    numbers, or a placeholder.
    """
    if not raw or not isinstance(raw, str):
        return False

    display = clean_display_name(raw)
    if not (MIN_NAME_LENGTH <= len(display) <= MAX_NAME_LENGTH):
        return False

    normalized = normalize_name(raw)
    if len(normalized) < MIN_NAME_LENGTH:
        return False

    # Must contain a letter: "2024", "---" and "42" are not entities.
    if not any(character.isalpha() for character in normalized):
        return False

    return True
