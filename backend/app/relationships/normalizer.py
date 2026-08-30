"""Deterministic relationship-type normalization.

Models express the same connection many ways -- "utilizes", "runs on",
"powered by" all mean USES. The database only ever stores the controlled
vocabulary, so equivalent phrasings must collapse before storage or they
become duplicate relationships.

Nothing here calls a model. The mapping is fixed, so the same phrasing always
resolves to the same type.
"""

import re
from typing import Optional

from app.relationships.models import RelationshipType

_SEPARATORS = re.compile(r"[\s\-/]+")
_NON_WORD = re.compile(r"[^a-z0-9_]+")

#: Synonym phrasings mapped to the controlled type. Keys are already
#: normalized (lowercase, underscore-separated). Only unambiguous synonyms
#: belong here -- a wrong mapping silently changes what the data means.
_SYNONYMS = {
    # USES
    "utilizes": RelationshipType.USES,
    "utilises": RelationshipType.USES,
    "using": RelationshipType.USES,
    "used": RelationshipType.USES,
    "uses": RelationshipType.USES,
    "runs_on": RelationshipType.USES,
    "running_on": RelationshipType.USES,
    "powered_by": RelationshipType.USES,
    "built_on": RelationshipType.USES,
    "built_with": RelationshipType.USES,
    "leverages": RelationshipType.USES,
    "employs": RelationshipType.USES,
    "adopted": RelationshipType.USES,
    # BUILDS
    "building": RelationshipType.BUILDS,
    "builds": RelationshipType.BUILDS,
    "develops": RelationshipType.BUILDS,
    "developing": RelationshipType.BUILDS,
    "is_building": RelationshipType.BUILDS,
    # CREATED
    "creates": RelationshipType.CREATED,
    "made": RelationshipType.CREATED,
    "authored": RelationshipType.CREATED,
    # WORKS_ON
    "working_on": RelationshipType.WORKS_ON,
    "work_on": RelationshipType.WORKS_ON,
    "contributes_to": RelationshipType.WORKS_ON,
    # WORKS_WITH
    "working_with": RelationshipType.WORKS_WITH,
    "collaborates_with": RelationshipType.WORKS_WITH,
    "collaborating_with": RelationshipType.WORKS_WITH,
    "partners_with": RelationshipType.WORKS_WITH,
    # INTERESTED_IN
    "interested_in": RelationshipType.INTERESTED_IN,
    "exploring": RelationshipType.INTERESTED_IN,
    "curious_about": RelationshipType.INTERESTED_IN,
    "researching": RelationshipType.INTERESTED_IN,
    # PREFERS
    "prefers": RelationshipType.PREFERS,
    "likes": RelationshipType.PREFERS,
    "favours": RelationshipType.PREFERS,
    "favors": RelationshipType.PREFERS,
    # HAS_GOAL
    "wants": RelationshipType.HAS_GOAL,
    "wants_to": RelationshipType.HAS_GOAL,
    "aims_to": RelationshipType.HAS_GOAL,
    "goal": RelationshipType.HAS_GOAL,
    "aspires_to": RelationshipType.HAS_GOAL,
    # OWNS
    "owns": RelationshipType.OWNS,
    "has": RelationshipType.OWNS,
    # PART_OF
    "part_of": RelationshipType.PART_OF,
    "belongs_to": RelationshipType.PART_OF,
    "component_of": RelationshipType.PART_OF,
    "subset_of": RelationshipType.PART_OF,
    # DEPENDS_ON
    "depends_on": RelationshipType.DEPENDS_ON,
    "requires": RelationshipType.DEPENDS_ON,
    "needs": RelationshipType.DEPENDS_ON,
    # LOCATED_IN
    "located_in": RelationshipType.LOCATED_IN,
    "lives_in": RelationshipType.LOCATED_IN,
    "based_in": RelationshipType.LOCATED_IN,
    "situated_in": RelationshipType.LOCATED_IN,
    # INVOLVED_IN
    "involved_in": RelationshipType.INVOLVED_IN,
    "participates_in": RelationshipType.INVOLVED_IN,
    "attended": RelationshipType.INVOLVED_IN,
    # FOUNDED
    "founded": RelationshipType.FOUNDED,
    "co_founded": RelationshipType.FOUNDED,
    "started": RelationshipType.FOUNDED,
    # RELATED_TO
    "related_to": RelationshipType.RELATED_TO,
    "associated_with": RelationshipType.RELATED_TO,
    "connected_to": RelationshipType.RELATED_TO,
}


def normalize_type(raw: Optional[str]) -> Optional[RelationshipType]:
    """Resolve a raw relationship label to a controlled type.

    Returns None when the label cannot be mapped -- the candidate is then
    rejected rather than guessed at. Missing a relationship is preferable to
    recording the wrong one.
    """
    if not raw or not isinstance(raw, str):
        return None

    text = _SEPARATORS.sub("_", raw.strip().lower())
    text = _NON_WORD.sub("", text).strip("_")
    if not text:
        return None

    # An exact controlled-vocabulary value.
    try:
        return RelationshipType(text.upper())
    except ValueError:
        pass

    return _SYNONYMS.get(text)


#: Values the model is allowed to emit, surfaced for prompts and tests.
ALLOWED_RELATIONSHIP_TYPES = tuple(member.value for member in RelationshipType)
