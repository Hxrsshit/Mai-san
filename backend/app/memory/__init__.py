"""Stage 2A memory foundation.

Memories are structured knowledge derived from conversations. Conversation
messages remain the raw source of truth; memories are extracted, validated,
deduplicated and stored separately.
"""

from app.memory.models import Memory, MemoryStatus, MemoryType
from app.memory.schemas import (
    MemoryCandidate,
    MemoryExtractionResult,
    MemoryList,
    MemoryRead,
)
from app.memory.service import MemoryService

__all__ = [
    "Memory",
    "MemoryStatus",
    "MemoryType",
    "MemoryCandidate",
    "MemoryExtractionResult",
    "MemoryList",
    "MemoryRead",
    "MemoryService",
]
