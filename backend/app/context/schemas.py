"""Context assembly schemas.

`ContextPackage` is what Stage 3A produces: the current message, recent
conversation, and long-term knowledge kept as **separate categories** rather
than flattened into one blob.

Two invariants shape the design:

- **The current message always survives.** Every budget is applied to the
  optional material around it, never to the message itself.
- **Retrieved knowledge is reference data, not instructions.** Memories,
  entities and relationships live in their own fields and are marked as such.
  Nothing here places retrieved content anywhere privileged; Stage 3B renders
  them as reference material.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ContextRole(str, Enum):
    """How a piece of context may be used downstream.

    The distinction is structural, not cosmetic: `INSTRUCTION` content may
    direct behaviour, `REFERENCE` content may only inform it. Stage 3A never
    assigns `INSTRUCTION` to anything retrieved.
    """

    INSTRUCTION = "instruction"
    CONVERSATION = "conversation"
    REFERENCE = "reference"


class RecentMessage(BaseModel):
    """One message of short-term context, in its original form."""

    model_config = ConfigDict(from_attributes=True)

    role: str
    content: str
    created_at: Optional[datetime] = None

    #: Conversation turns are conversation, never instructions.
    context_role: ContextRole = ContextRole.CONVERSATION


class ContextMemory(BaseModel):
    """A retrieved memory, carried through without alteration."""

    id: uuid.UUID
    content: str
    memory_type: str
    importance_score: int
    confidence_score: float
    created_at: Optional[datetime] = None

    #: Stage 2D's position. Preserved, never recomputed.
    retrieval_rank: int = 0
    retrieval_score: float = 0.0
    context_role: ContextRole = ContextRole.REFERENCE


class ContextEntity(BaseModel):
    """A matched entity, kept deliberately compact.

    No database internals beyond the id, no timestamps, no alias list --
    entities should provide context, not consume it.
    """

    id: uuid.UUID
    name: str
    entity_type: str
    description: Optional[str] = None

    retrieval_rank: int = 0
    match_strength: float = 0.0
    directly_matched: bool = True
    context_role: ContextRole = ContextRole.REFERENCE

    def render(self) -> str:
        return f"{self.name} ({self.entity_type})"


class ContextRelationship(BaseModel):
    """A relationship, reduced to the claim itself.

    Evidence is storage provenance; the claim is what carries meaning, so
    evidence rows are deliberately not included here.
    """

    id: uuid.UUID
    source_name: str
    relationship_type: str
    target_name: str
    confidence_score: float = 0.0

    retrieval_rank: int = 0
    connects_matched_entities: bool = False
    context_role: ContextRole = ContextRole.REFERENCE

    def render(self) -> str:
        return f"{self.source_name} {self.relationship_type} {self.target_name}"


class DroppedItem(BaseModel):
    """Something the budget excluded, recorded so it can be explained."""

    category: str  # "memory" | "entity" | "relationship" | "recent_message"
    identifier: str
    reason: str  # "category_limit" | "total_budget"
    retrieval_rank: int = 0


class ContextBudget(BaseModel):
    """The limits in force, echoed back for debugging."""

    recent_message_limit: int
    max_memory_items: int
    max_entity_items: int
    max_relationship_items: int
    max_total_chars: int


class ContextCharacterCounts(BaseModel):
    """Deterministic size accounting, per category.

    Characters, not tokens. The accounting is isolated behind one sizing
    function so token-based budgeting can replace it without touching the
    assembly logic.
    """

    current_message: int = 0
    recent_conversation: int = 0
    memories: int = 0
    entities: int = 0
    relationships: int = 0
    total: int = 0


class ContextMetadata(BaseModel):
    """Everything needed to explain an assembly decision."""

    assembled_at: datetime
    conversation_id: Optional[uuid.UUID] = None

    recent_message_count: int = 0
    memory_count: int = 0
    entity_count: int = 0
    relationship_count: int = 0

    characters: ContextCharacterCounts = Field(
        default_factory=ContextCharacterCounts
    )
    budget: Optional[ContextBudget] = None
    dropped_items: List[DroppedItem] = Field(default_factory=list)

    #: Sources that failed and were skipped. Assembly degrades rather than
    #: fails, so this records what was unavailable.
    degraded_sources: List[str] = Field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_items)


class ContextPackage(BaseModel):
    """The assembled context. Categories stay separate by design.

    Stage 3A stops here. Rendering this into a prompt belongs to Stage 3B, so
    nothing in this package is pre-formatted for a model.
    """

    current_message: str
    recent_conversation: List[RecentMessage] = Field(default_factory=list)
    memories: List[ContextMemory] = Field(default_factory=list)
    entities: List[ContextEntity] = Field(default_factory=list)
    relationships: List[ContextRelationship] = Field(default_factory=list)
    metadata: ContextMetadata

    @property
    def has_long_term_knowledge(self) -> bool:
        return bool(self.memories or self.entities or self.relationships)

    @property
    def has_recent_conversation(self) -> bool:
        return bool(self.recent_conversation)

    @property
    def is_minimal(self) -> bool:
        """True when only the current message survived."""
        return not (self.has_long_term_knowledge or self.has_recent_conversation)


class ContextDebugRequest(BaseModel):
    conversation_id: Optional[uuid.UUID] = None
    message: str = Field(..., min_length=1, max_length=8000)
