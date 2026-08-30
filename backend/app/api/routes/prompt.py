"""Prompt inspection endpoint.

Shows exactly what the chat request path would send to the model for a given
message: how many messages, in what order, which section each belongs to, what
it costs, and whether anything is duplicated.

Three guarantees, each covered by a test:

- **No model call.** Formatting is deterministic; nothing here touches
  `LLMProvider`.
- **No mutation.** Context assembly is read-only, and no message is stored --
  the message in the request body is never added to the conversation.
- **The production path.** It calls the same `ContextService.build` and
  `PromptFormatter.format` the chat turn calls, so what is displayed is what
  would actually be sent, not a reconstruction of it.

System instruction text is reported by size only. It is application
configuration rather than user data, and there is no reason for an inspection
endpoint to echo it back. No credential or provider setting is exposed by any
field here.
"""

from typing import List

from fastapi import APIRouter

from app.api.deps import Context, Formatter
from app.prompt.formatter import detect_duplicates
from app.prompt.schemas import (
    PromptDebugContext,
    PromptDebugMessage,
    PromptDebugRequest,
    PromptDebugResponse,
    PromptSection,
)

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


@router.post(
    "/debug",
    response_model=PromptDebugResponse,
    summary="Show the prompt that would be sent for a message",
)
async def debug_prompt(
    payload: PromptDebugRequest, context: Context, formatter: Formatter
) -> PromptDebugResponse:
    """Assemble and format, then describe the result without sending it.

    `conversation_id` is optional: omit it to inspect a prompt built from
    long-term knowledge alone, which is also how a brand new conversation
    behaves.
    """
    package = await context.build(
        current_message=payload.message, conversation_id=payload.conversation_id
    )
    prompt = formatter.format(package)

    messages: List[PromptDebugMessage] = []
    current_index = -1

    for index, part in enumerate(prompt.parts):
        if part.section is PromptSection.CURRENT_MESSAGE:
            current_index = index
        messages.append(
            PromptDebugMessage(
                index=index,
                role=part.message.role,
                section=part.section,
                chars=len(part.message.content),
                content=(
                    None
                    if part.section is PromptSection.SYSTEM_INSTRUCTIONS
                    else part.message.content
                ),
            )
        )

    # Ordered by first appearance, so the list reads as the prompt is laid out.
    sections_included: List[PromptSection] = []
    for part in prompt.parts:
        if part.section not in sections_included:
            sections_included.append(part.section)

    duplicates = detect_duplicates(prompt)
    metadata = package.metadata

    return PromptDebugResponse(
        total_messages=len(prompt.parts),
        current_message_index=current_index,
        current_message_is_last=current_index == len(prompt.parts) - 1,
        sections_included=sections_included,
        messages=messages,
        stats=prompt.stats,
        duplicates=duplicates,
        has_duplicates=duplicates.has_duplicates,
        context=PromptDebugContext(
            recent_message_count=metadata.recent_message_count,
            memory_count=metadata.memory_count,
            entity_count=metadata.entity_count,
            relationship_count=metadata.relationship_count,
            context_chars=metadata.characters.total,
            dropped_items=metadata.dropped_count,
            degraded_sources=list(metadata.degraded_sources),
        ),
    )
