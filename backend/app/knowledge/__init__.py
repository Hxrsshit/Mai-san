"""Stage 3C knowledge lifecycle: conflicts, supersession, and history.

Knowledge changes over time. A memory recorded six months ago may be true
history and false present. Stage 3C makes that distinction explicit without
ever destroying the record.

    new memory
        -> ConflictDetector   (read-only, deterministic, no model call)
        -> ConflictOutcome
        -> LifecycleWriter    (the only writer of status and conflict links)
        -> Memory.status / Relationship.status  +  KnowledgeConflict

Runs in the existing background pipeline, after relationship extraction. It is
never invoked from the request path: retrieval and context assembly read
lifecycle state, and never change it.
"""

from app.knowledge.conflicts import ConflictDetector
from app.knowledge.lifecycle import LifecycleWriter
from app.knowledge.models import (
    ConflictReason,
    ConflictResolution,
    KnowledgeConflict,
)
from app.knowledge.schemas import (
    ConflictLink,
    ConflictOutcome,
    EvaluationReport,
    MemoryLifecycle,
)
from app.knowledge.service import KnowledgeService

__all__ = [
    "ConflictDetector",
    "ConflictLink",
    "ConflictOutcome",
    "ConflictReason",
    "ConflictResolution",
    "EvaluationReport",
    "KnowledgeConflict",
    "KnowledgeService",
    "LifecycleWriter",
    "MemoryLifecycle",
]
