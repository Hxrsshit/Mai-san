"""Stage 2D context retrieval and memory assembly.

Deterministic, database-driven retrieval of relevant long-term knowledge,
assembled into a bounded context package for the chat pipeline. Adds no model
calls.
"""

from app.retrieval.schemas import (
    ContextPackage,
    RetrievalResult,
    RetrievalMetadata,
    RetrievedEntity,
    RetrievedMemory,
    RetrievedRelationship,
)
from app.retrieval.service import RetrievalService

__all__ = [
    "ContextPackage",
    "RetrievalResult",
    "RetrievalMetadata",
    "RetrievedEntity",
    "RetrievedMemory",
    "RetrievedRelationship",
    "RetrievalService",
]
