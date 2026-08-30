"""Retrieval schemas.

`ContextPackage` is what the chat pipeline receives: a compact, structured view
of relevant knowledge. Database rows are never handed to the model directly --
only the fields below, rendered as prose.

Every retrieved item carries its score and the signals that produced it. That
metadata is for debugging and tests; it is not sent to the model.
"""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MatchSignal(BaseModel):
    """Why an item was retrieved, and how strongly."""

    model_config = ConfigDict(frozen=True)

    name: str  # "keyword" | "entity" | "relationship" | "alias" | ...
    strength: float = Field(ge=0.0, le=1.0)
    detail: Optional[str] = None


class ScoreBreakdown(BaseModel):
    """The ranking formula's components, kept separate for testability."""

    text_relevance: float = 0.0
    entity_relevance: float = 0.0
    relationship_relevance: float = 0.0
    importance: float = 0.0
    confidence: float = 0.0
    recency: float = 0.0
    final_score: float = 0.0


class RetrievedMemory(BaseModel):
    id: uuid.UUID
    content: str
    memory_type: str
    importance_score: int
    confidence_score: float
    created_at: datetime

    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    signals: List[MatchSignal] = Field(default_factory=list)
    rank: int = 0


class RetrievedEntity(BaseModel):
    id: uuid.UUID
    canonical_name: str
    entity_type: str
    description: Optional[str] = None

    match_strength: float = 0.0
    matched_via: str = ""  # "canonical" | "alias" | "normalized"
    matched_text: str = ""
    rank: int = 0


class RetrievedRelationship(BaseModel):
    id: uuid.UUID
    source_name: str
    relationship_type: str
    target_name: str
    confidence_score: float

    score: float = 0.0
    signals: List[MatchSignal] = Field(default_factory=list)
    rank: int = 0

    def render(self) -> str:
        return f"{self.source_name} {self.relationship_type} {self.target_name}"


class RetrievalMetadata(BaseModel):
    """Everything needed to explain a retrieval decision."""

    normalized_query: str = ""
    keywords: List[str] = Field(default_factory=list)
    candidate_memories: int = 0
    candidate_relationships: int = 0
    selected_memories: int = 0
    selected_entities: int = 0
    selected_relationships: int = 0
    context_chars: int = 0
    budget_exhausted: bool = False
    duration_ms: float = 0.0
    degraded_sources: List[str] = Field(default_factory=list)
    enabled: bool = True


class RetrievalResult(BaseModel):
    """Where Stage 2D ends: ranked knowledge, not yet assembled context.

    Stage 3A consumes this and combines it with the current message and recent
    conversation to produce a `ContextPackage`.
    """

    query: str = ""
    matched_entities: List[RetrievedEntity] = Field(default_factory=list)
    memories: List[RetrievedMemory] = Field(default_factory=list)
    relationships: List[RetrievedRelationship] = Field(default_factory=list)
    metadata: RetrievalMetadata = Field(default_factory=RetrievalMetadata)

    @property
    def is_empty(self) -> bool:
        return not (self.memories or self.matched_entities or self.relationships)


class DebugScoredMemory(BaseModel):
    """A candidate with its full score breakdown, for the debug endpoint."""

    id: uuid.UUID
    content: str
    memory_type: str
    score: ScoreBreakdown
    signals: List[MatchSignal]
    selected: bool


class RetrievalDebugRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)


class RetrievalDebugResponse(BaseModel):
    query: str
    normalized_query: str
    keywords: List[str]
    matched_entities: List[RetrievedEntity]
    candidate_memories: List[DebugScoredMemory]
    candidate_relationships: List[RetrievedRelationship]
    selected_memories: List[RetrievedMemory]
    selected_entities: List[RetrievedEntity]
    selected_relationships: List[RetrievedRelationship]
    assembled_context: str
    metadata: RetrievalMetadata
    weights: Dict[str, float]


#: Stage 2D originally called this `ContextPackage`. The name now belongs to
#: Stage 3A's assembled context, so the retrieval-side type was renamed to
#: match the architecture. The alias keeps older imports working.
ContextPackage = RetrievalResult
