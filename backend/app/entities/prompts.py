"""Prompts for entity extraction.

Kept separate from routes and services so the wording can be iterated on --
prompt quality is the main lever on extraction quality.
"""

from app.entities.models import EntityType

#: Classification rules. The specification notes that something like "Groq"
#: could be labelled company or technology and asks for one consistent
#: approach; these rules are that approach, and the same text is given to the
#: model so its labels match the documentation.
CLASSIFICATION_RULES = """\
- person:       a named individual. e.g. "John Doe"
- company:      a named business. e.g. "Groq", "Anthropic", "OpenAI"
- organization: a named non-commercial body -- university, NGO, government
- project:      a named body of work someone is building. e.g. "Mai"
- product:      a named commercial offering or model. e.g. "Claude", "ChatGPT"
- technology:   a named tool, language, framework, database or protocol.
                e.g. "PostgreSQL", "Python", "FastAPI"
- place:        a named geographic location. e.g. "Bangalore"
- concept:      a named field, domain or practice.
                e.g. "AI Product Development", "Venture Capital"
- event:        a named occurrence. e.g. "Stage 1 completion"
- other:        identifiable, but none of the above

When a name could fit more than one type, prefer the more specific rule above:
a vendor is a `company`, its released model is a `product`, and an open tool or
language is a `technology`.\
"""

EXTRACTION_SYSTEM_PROMPT = f"""\
You extract identifiable entities from a single memory statement.

An entity is a specific, nameable thing: a person, company, project, product,
technology, place, named concept or event. Generic nouns are NOT entities.

From "User likes working on interesting projects." extract NOTHING.
There is no named thing there -- "working", "projects" and "interesting" are
ordinary words, not entities.

From "User is building Mai using PostgreSQL and Groq." extract exactly three:
Mai, PostgreSQL, Groq.

ENTITY TYPES (use exactly one of these values):
{CLASSIFICATION_RULES}

RULES:
1. Extract only entities actually named in the memory. Never infer entities
   that are not mentioned.
2. Never invent facts. The description must be supported by the memory text
   alone -- if the memory does not say what something is, omit the description
   entirely. "Groq is the fastest AI infrastructure company" is a fabrication;
   omitting the description is correct.
3. Use the name as it appears, with its normal capitalisation: "PostgreSQL",
   not "postgresql". Do not append descriptor words -- "PostgreSQL", not
   "PostgreSQL database".
4. Add an alias only when the memory itself shows an alternative form, or when
   the abbreviation is unambiguous and widely used ("AI" for "Artificial
   Intelligence"). Do not invent alias lists. Most entities need none.
5. Do not extract the words "user", "I", "me" or "they" -- the user is implicit
   and is never an entity.
6. Prefer fewer, higher-quality entities. Extracting nothing is correct when a
   memory names nothing specific.
7. confidence_score (0.0-1.0) is how certain you are that this is a genuine,
   correctly-typed entity named in the memory. Use 0.9+ only for explicit,
   unambiguous mentions.

OUTPUT:
Return ONLY a JSON object, no markdown fencing and no commentary:

{{
  "entities": [
    {{
      "name": "PostgreSQL",
      "entity_type": "technology",
      "description": "Database used in the user's Mai project.",
      "aliases": ["Postgres"],
      "confidence_score": 0.97
    }}
  ]
}}

When the memory names nothing specific, return exactly:

{{"entities": []}}\
"""


def build_extraction_user_prompt(memory_content: str, memory_type: str) -> str:
    """Render one memory for entity extraction.

    Only the memory statement is sent -- not the conversation. Memories are
    already filtered and validated, which is what keeps entity extraction low
    noise.
    """
    return (
        f"MEMORY ({memory_type}):\n{memory_content}\n\n"
        "Extract the identifiable entities named in this memory. "
        "Return JSON only."
    )


#: Valid values, surfaced for tests and error messages.
ALLOWED_ENTITY_TYPES = tuple(member.value for member in EntityType)
