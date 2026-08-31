"""Stage 3B prompt schemas.

These types describe *what was built*, not how to build it. The formatter
returns a `FormattedPrompt`: the provider-agnostic messages the LLM will
receive, each tagged with the section it came from, plus deterministic size
accounting.

The section tag is the load-bearing part. It is what makes the structural
invariant testable: retrieved knowledge is always `REFERENCE_KNOWLEDGE`, and
`SYSTEM_INSTRUCTIONS` is only ever produced from the application's own
configured prompt -- never from anything retrieved or from stored
conversation text.
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from app.llm.base import LLMMessage
from app.context.schemas import ContextRole


class PromptSection(str, Enum):
    """Where a message in the final prompt came from.

    Ordering of the enum matches the ordering of the prompt itself.
    """

    SYSTEM_INSTRUCTIONS = "system_instructions"
    #: Authoritative facts about the running system: provider, model,
    #: database, capabilities. Application configuration, not retrieval --
    #: ranked above reference knowledge because it *is* authoritative, and
    #: placed immediately after the instructions for the same reason.
    RUNTIME_FACTS = "runtime_facts"
    REFERENCE_KNOWLEDGE = "reference_knowledge"
    CONVERSATION = "conversation"
    CURRENT_MESSAGE = "current_message"


#: Which `ContextRole` a retrieved item must carry to be rendered at all.
#: Anything else is dropped rather than promoted -- see `formatter.py`.
RENDERABLE_REFERENCE_ROLES = frozenset({ContextRole.REFERENCE})

#: Roles a stored conversation message may occupy in the final prompt.
#: Deliberately excludes "system": stored text must never become an
#: instruction, whatever a row in the database happens to contain.
ALLOWED_CONVERSATION_ROLES = frozenset({"user", "assistant"})


class PromptStats(BaseModel):
    """Deterministic accounting of the formatted prompt.

    Sizes are characters, matching Stage 3A's budgeting unit. Nothing here is
    sent to the model; it exists for logs and the debug endpoint.
    """

    total_messages: int = 0

    instruction_messages: int = 0
    runtime_fact_messages: int = 0
    reference_messages: int = 0
    conversation_messages: int = 0
    current_messages: int = 0

    memories_rendered: int = 0
    entities_rendered: int = 0
    relationships_rendered: int = 0

    instruction_chars: int = 0
    runtime_fact_chars: int = 0
    reference_chars: int = 0
    conversation_chars: int = 0
    current_message_chars: int = 0
    total_chars: int = 0

    #: True when the recent conversation ended with the current user message
    #: and that copy was removed. See `formatter._strip_echoed_current`.
    duplicate_current_message_dropped: bool = False
    #: Conversation rows whose role was not user/assistant, and retrieved
    #: items not marked as reference data. Both are dropped, never promoted.
    rejected_conversation_messages: int = 0
    rejected_reference_items: int = 0
    #: True when the safe minimal path produced this prompt.
    fallback_used: bool = False


@dataclass(frozen=True)
class PromptPart:
    """One message plus the section that produced it."""

    message: LLMMessage
    section: PromptSection


@dataclass
class FormattedPrompt:
    """The result of formatting: messages for the provider, plus accounting."""

    parts: List[PromptPart] = field(default_factory=list)
    stats: PromptStats = field(default_factory=PromptStats)

    @property
    def messages(self) -> List[LLMMessage]:
        """Exactly what is handed to `LLMProvider.generate_response`."""
        return [part.message for part in self.parts]

    @property
    def sections(self) -> List[PromptSection]:
        return [part.section for part in self.parts]

    @property
    def current_message(self) -> Optional[str]:
        for part in reversed(self.parts):
            if part.section is PromptSection.CURRENT_MESSAGE:
                return part.message.content
        return None


class PromptDuplicateReport(BaseModel):
    """What a duplication scan of a formatted prompt found.

    Computed from the finished prompt rather than from intent, so it catches
    duplication introduced by any path -- including one that should no longer
    exist.
    """

    current_message_occurrences: int = 0
    duplicate_conversation_messages: int = 0
    duplicate_reference_lines: int = 0
    reference_blocks: int = 0

    @property
    def has_duplicates(self) -> bool:
        return bool(
            self.current_message_occurrences > 1
            or self.duplicate_conversation_messages
            or self.duplicate_reference_lines
            or self.reference_blocks > 1
        )


class PromptDebugMessage(BaseModel):
    """One message as shown by the debug endpoint.

    `content` is omitted for system instructions: those are the application's
    own configuration, not user data, and the endpoint reports their size
    rather than their text.
    """

    index: int
    role: str
    section: PromptSection
    chars: int
    content: Optional[str] = None


class PromptDebugContext(BaseModel):
    """What Stage 3A supplied, summarised."""

    recent_message_count: int = 0
    memory_count: int = 0
    entity_count: int = 0
    relationship_count: int = 0
    context_chars: int = 0
    dropped_items: int = 0
    degraded_sources: List[str] = Field(default_factory=list)


class PromptDebugRequest(BaseModel):
    conversation_id: Optional[uuid.UUID] = None
    message: str = Field(..., min_length=1, max_length=8000)


class PromptDebugResponse(BaseModel):
    """A safe view of the prompt that would be sent for this message.

    Never includes credentials, provider configuration, or raw system
    instruction text.
    """

    total_messages: int
    current_message_index: int
    current_message_is_last: bool
    sections_included: List[PromptSection]
    messages: List[PromptDebugMessage]
    stats: PromptStats
    duplicates: PromptDuplicateReport
    has_duplicates: bool
    context: PromptDebugContext


__all__ = [
    "ALLOWED_CONVERSATION_ROLES",
    "RENDERABLE_REFERENCE_ROLES",
    "FormattedPrompt",
    "PromptDebugContext",
    "PromptDebugMessage",
    "PromptDebugRequest",
    "PromptDebugResponse",
    "PromptDuplicateReport",
    "PromptPart",
    "PromptSection",
    "PromptStats",
]
