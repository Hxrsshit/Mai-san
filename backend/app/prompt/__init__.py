"""Stage 3B prompt integration.

The single owner of the conversion from Stage 3A's `ContextPackage` into the
generic LLM messages sent to a provider.

    ContextPackage -> PromptFormatter -> List[LLMMessage] -> LLMProvider

There is exactly one production path by which long-term knowledge reaches a
model prompt, and it runs through `PromptFormatter.format`. Stage 2D's inline
rendering, which used to inject knowledge directly from `ChatService`, was
retired here -- see `docs/stage3b_prompt_integration_architecture.md`.
"""

from app.prompt.formatter import (
    REFERENCE_HEADER,
    REFERENCE_PREAMBLE,
    PromptFormatter,
    detect_duplicates,
    knowledge_block,
    render_reference_block,
)
from app.prompt.schemas import (
    FormattedPrompt,
    PromptDebugRequest,
    PromptDebugResponse,
    PromptDuplicateReport,
    PromptPart,
    PromptSection,
    PromptStats,
)

__all__ = [
    "FormattedPrompt",
    "PromptDebugRequest",
    "PromptDebugResponse",
    "PromptDuplicateReport",
    "PromptFormatter",
    "PromptPart",
    "PromptSection",
    "PromptStats",
    "REFERENCE_HEADER",
    "REFERENCE_PREAMBLE",
    "detect_duplicates",
    "knowledge_block",
    "render_reference_block",
]
