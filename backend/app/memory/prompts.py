"""Prompts for memory extraction.

Kept here rather than inline in a service or route so the wording can be
iterated on -- prompt quality is the main lever on memory quality.
"""

from typing import List

from app.memory.models import MemoryType

MEMORY_TYPE_GUIDE = """\
- semantic:   durable factual information about the user or their world.
              e.g. "User works on AI-related projects."
- preference: how the user likes things done.
              e.g. "User prefers concise explanations with practical examples."
- goal:       something the user wants to achieve.
              e.g. "User wants to transition their career toward AI product development."
- decision:   an explicit choice the user has made.
              e.g. "User decided to use PostgreSQL as Mai's initial database."
- episodic:   a genuinely significant event or milestone.
              e.g. "User completed Stage 1 of the Mai project."\
"""

EXTRACTION_SYSTEM_PROMPT = f"""\
You extract long-term memories from a conversation turn.

Most turns contain NOTHING worth remembering. Greetings, small talk, general
knowledge questions and passing remarks are not memories. Returning zero
memories is the correct and expected outcome for the majority of turns.

ONLY extract information that is:
- explicitly stated by the USER (never inferred from the assistant's reply),
- personally relevant to the user,
- likely to still matter in future conversations.

NEVER extract:
- greetings, pleasantries or small talk,
- general knowledge questions ("What is Python?"),
- anything the assistant claimed, assumed or inferred about the user,
- temporary state ("I'm tired today"),
- trivia with no future relevance.

The assistant's message is provided ONLY as context for understanding the
user. Never treat an assistant statement as a fact about the user. If the user
says "I might be interested in AI" and the assistant replies "You clearly want
to be an AI entrepreneur", the only supportable memory is the user's own
hedged interest -- or none at all.

MEMORY TYPES (use exactly one of these values):
{MEMORY_TYPE_GUIDE}

WRITING MEMORY CONTENT:
Each memory must be a standalone third-person statement that still makes sense
years later with no access to the original conversation.
  Bad:  "User said they might want AI."
  Bad:  "He agreed with that."
  Good: "User is interested in exploring AI product development."

SCORING:
- importance_score (integer 1-10): 1-3 trivial, 4-6 moderate, 7-8 important,
  9-10 defining. Be conservative; most real memories are 5-8. Reserve 9-10 for
  information that shapes who the user is or what they are building.
- confidence_score (float 0.0-1.0): how certain you are that the memory
  accurately reflects what the user actually said. Use 0.9+ only for explicit,
  unambiguous statements. Hedged language ("maybe", "I might") should score
  lower and often should not be stored at all.

OUTPUT:
Return ONLY a JSON object, with no markdown fencing and no commentary:

{{
  "should_store_memory": true,
  "memories": [
    {{
      "content": "User wants to transition their career toward AI product development.",
      "memory_type": "goal",
      "importance_score": 8,
      "confidence_score": 0.94
    }}
  ]
}}

When there is nothing worth remembering, return exactly:

{{"should_store_memory": false, "memories": []}}

Never return more than 5 memories for a single turn. Prefer fewer, better
memories over many weak ones.\
"""


def build_extraction_user_prompt(
    user_message: str,
    assistant_message: str,
    recent_context: List[str] = None,
) -> str:
    """Render the turn for analysis.

    Roles are labelled explicitly so the model cannot confuse who said what --
    the main source of false memories attributed to the user.
    """
    sections = []

    if recent_context:
        joined = "\n".join(recent_context)
        sections.append(
            "EARLIER CONTEXT (for understanding only -- do not extract memories "
            f"from this):\n{joined}"
        )

    sections.append(f"USER MESSAGE (the only valid source of memories):\n{user_message}")
    sections.append(
        "ASSISTANT REPLY (context only -- never a source of facts about the "
        f"user):\n{assistant_message}"
    )
    sections.append("Extract memories from the USER MESSAGE. Return JSON only.")

    return "\n\n".join(sections)


#: Valid values, surfaced for tests and validation error messages.
ALLOWED_MEMORY_TYPES = tuple(member.value for member in MemoryType)
