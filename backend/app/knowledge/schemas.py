"""Knowledge lifecycle schemas.

Read-only views over lifecycle state, for the debug endpoint and for the
service's own return values. Nothing here is sent to a model: lifecycle
metadata is developer-facing, and Stage 3B deliberately keeps ids, scores and
internal state out of the prompt.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.knowledge.models import ConflictReason, ConflictResolution


@dataclass
class ConflictOutcome:
    """One decision the evaluator reached, before it was written."""

    resolution: ConflictResolution
    reason: ConflictReason
    older_memory_id: Optional[uuid.UUID] = None
    newer_memory_id: Optional[uuid.UUID] = None
    older_relationship_id: Optional[uuid.UUID] = None
    newer_relationship_id: Optional[uuid.UUID] = None

    @property
    def is_memory(self) -> bool:
        return self.older_memory_id is not None

    @property
    def supersedes(self) -> bool:
        return self.resolution is ConflictResolution.SUPERSEDED


@dataclass
class EvaluationReport:
    """What one background evaluation pass did.

    Counts and ids only -- never memory text. Lifecycle logging must not
    become a second, longer-lived copy of the user's personal knowledge.
    """

    candidates_examined: int = 0
    conflicts_detected: int = 0
    memories_superseded: int = 0
    relationships_superseded: int = 0
    unresolved: int = 0
    links_created: int = 0
    #: Links a concurrent evaluation had already written. Not a failure.
    links_already_present: int = 0
    #: Supersessions refused because they would have closed a cycle.
    cycles_prevented: int = 0
    #: Status updates that could not be applied. The item stays ACTIVE.
    status_updates_failed: int = 0
    duration_ms: float = 0.0
    failed: bool = False

    def merge(self, other: "EvaluationReport") -> None:
        self.candidates_examined += other.candidates_examined
        self.conflicts_detected += other.conflicts_detected
        self.memories_superseded += other.memories_superseded
        self.relationships_superseded += other.relationships_superseded
        self.unresolved += other.unresolved
        self.links_created += other.links_created
        self.links_already_present += other.links_already_present
        self.cycles_prevented += other.cycles_prevented
        self.status_updates_failed += other.status_updates_failed
        self.failed = self.failed or other.failed


# --- Debug views ------------------------------------------------------------


class ConflictLink(BaseModel):
    """One lifecycle link, as shown by the debug endpoint."""

    id: uuid.UUID
    resolution: ConflictResolution
    reason: ConflictReason
    older_memory_id: Optional[uuid.UUID] = None
    newer_memory_id: Optional[uuid.UUID] = None
    older_relationship_id: Optional[uuid.UUID] = None
    newer_relationship_id: Optional[uuid.UUID] = None
    triggering_memory_id: Optional[uuid.UUID] = None
    detected_at: datetime


class MemoryLifecycle(BaseModel):
    """Everything known about one memory's lifecycle.

    Content is included because it is already exposed by `/api/memories`;
    nothing here widens what a caller can read. No score, provider setting or
    credential appears.
    """

    memory_id: uuid.UUID
    status: str
    content: str
    created_at: datetime
    updated_at: datetime

    #: Links where this memory is the older side: what replaced or contests it.
    superseded_by: List[ConflictLink] = Field(default_factory=list)
    #: Links where this memory is the newer side: what it replaced.
    supersedes: List[ConflictLink] = Field(default_factory=list)
    #: Links this memory triggered, about other knowledge.
    triggered: List[ConflictLink] = Field(default_factory=list)

    @property
    def is_historical(self) -> bool:
        return self.status != "active"


__all__ = [
    "ConflictLink",
    "ConflictOutcome",
    "EvaluationReport",
    "MemoryLifecycle",
]
