"""Stage 3A context assembly.

Combines the current user message, recent conversation, and Stage 2D's ranked
retrieval result into a bounded, structured `ContextPackage`.

Stage 3A stops at the package. Rendering it into a prompt is Stage 3B.
"""

from app.context.assembler import ContextAssembler
from app.context.budget import BudgetLimits, ContextBudgeter, character_sizer
from app.context.schemas import (
    ContextEntity,
    ContextMemory,
    ContextMetadata,
    ContextPackage,
    ContextRelationship,
    ContextRole,
    DroppedItem,
    RecentMessage,
)
from app.context.service import ContextService

__all__ = [
    "ContextAssembler",
    "ContextBudgeter",
    "BudgetLimits",
    "character_sizer",
    "ContextEntity",
    "ContextMemory",
    "ContextMetadata",
    "ContextPackage",
    "ContextRelationship",
    "ContextRole",
    "DroppedItem",
    "RecentMessage",
    "ContextService",
]
