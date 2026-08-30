"""Prompts for relationship extraction.

Kept separate from routes and services so the wording can be iterated on.
"""

from typing import Sequence

from app.relationships.normalizer import ALLOWED_RELATIONSHIP_TYPES

#: Disambiguation rules. Several types could plausibly fit the same sentence,
#: so the rules pick one, and the same text is given to the model so its
#: labels and this documentation cannot drift apart.
RELATIONSHIP_TYPE_GUIDE = """\
- USES           X makes use of Y.        Mai USES PostgreSQL
- BUILDS         X is building Y.         User BUILDS Mai
- CREATED        X made Y in the past.    User CREATED a library
- WORKS_ON       X works on Y (not the creator).
- WORKS_WITH     X collaborates with person Y.
- INTERESTED_IN  X is interested in Y, without commitment.
- HAS_GOAL       X wants to achieve Y.
- PREFERS        X favours Y over alternatives.
- OWNS           X owns Y.
- PART_OF        X is a component of Y.
- DEPENDS_ON     X requires Y to function.
- FOUNDED        X founded organisation Y.
- LOCATED_IN     X is situated in place Y.
- INVOLVED_IN    X participates in event Y.
- RELATED_TO     Last resort, only when no other type fits.\
"""


def build_extraction_system_prompt() -> str:
    return f"""\
You extract directional relationships between entities from a single memory.

DIRECTION IS CRITICAL. "source -- TYPE --> target" is a specific claim.
  "Mai uses PostgreSQL"  =>  Mai USES PostgreSQL      (correct)
                         =>  PostgreSQL USES Mai      (WRONG - reversed)
Ask which entity performs the action; that one is the source.

RELATIONSHIP TYPES (use exactly one of these values):
{RELATIONSHIP_TYPE_GUIDE}

WORKED EXAMPLES. Memories are written in the third person about "User", and
the grammatical subject is often NOT the source of the relationship. Read for
meaning, not sentence order:

  "User decided to use PostgreSQL for local storage in Mai."
     -> Mai USES PostgreSQL
     The project uses the database. The user made the decision; that is not
     itself a relationship worth recording.

  "User decided to use Groq for fast inference in Mai."
     -> Mai USES Groq

  "User is building Mai as a personal AI environment."
     -> User BUILDS Mai

  "User is interested in AI product development."
     -> User INTERESTED_IN AI Product Development

  "John Doe is collaborating with the user on Mai."
     -> John Doe WORKS_WITH User
     -> John Doe WORKS_ON Mai

  "User wants to transition into AI product development."
     -> User HAS_GOAL AI Product Development

Extract the obvious relationship when one is plainly stated. Returning nothing
for a memory that clearly connects two available entities is a mistake.

RULES:
1. Use ONLY the entities listed as available. Never invent an entity, and
   never use a name that is not in that list.
2. Extract only relationships the memory actually supports. From "User is
   exploring venture capital" the supportable claim is
   User INTERESTED_IN Venture Capital -- NOT that the user works at a venture
   capital firm. Do not infer employment, ownership or intent that is not
   stated.
3. Never relate an entity to itself.
4. Do not force a relationship. If the memory does not connect two of the
   available entities, return an empty list. That is a normal outcome.
5. Prefer the most specific type that fits. RELATED_TO is a last resort --
   if you are reaching for it, the relationship is probably not worth
   recording at all. Use PART_OF only for genuine composition (a module of a
   system), never for "X is used by Y" -- that is USES, in the other
   direction.
6. Do not emit the same relationship twice.
7. confidence_score (0.0-1.0) is how certain you are that the memory supports
   this exact claim in this exact direction. Use 0.9+ only when the memory
   states it plainly.

OUTPUT:
Return ONLY a JSON object, no markdown fencing and no commentary:

{{
  "relationships": [
    {{
      "source_entity": "Mai",
      "relationship_type": "USES",
      "target_entity": "PostgreSQL",
      "confidence_score": 0.93
    }}
  ]
}}

When the memory supports no relationship between the available entities,
return exactly:

{{"relationships": []}}\
"""


def build_extraction_user_prompt(
    memory_content: str, memory_type: str, entity_names: Sequence[str]
) -> str:
    """Render one memory plus the entities available to relate.

    Only entities already resolved for this memory are offered, plus the
    implicit subject. Relationships may only be created between entities that
    already exist -- the relationship system never creates them.
    """
    available = "\n".join(f"  - {name}" for name in entity_names)
    return (
        f"MEMORY ({memory_type}):\n{memory_content}\n\n"
        f"AVAILABLE ENTITIES (use only these exact names):\n{available}\n\n"
        "Extract the directional relationships this memory supports between "
        "the available entities. Return JSON only."
    )


__all__ = [
    "ALLOWED_RELATIONSHIP_TYPES",
    "RELATIONSHIP_TYPE_GUIDE",
    "build_extraction_system_prompt",
    "build_extraction_user_prompt",
]
