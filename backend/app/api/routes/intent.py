"""Intent inspection endpoint.

Shows what Mai understands a message to be asking for, without answering it,
storing it, or acting on it.

Unlike the other debug endpoints this one **does** make a model call -- exactly
one, the same bounded classification the chat path makes. That is the point:
it exercises the production classifier rather than a reconstruction of it.

It writes nothing. No message is stored, no memory extracted, no lifecycle
state touched. And it cannot execute: a response describing an ACTION is a
label, and Stage 4A has no executor for it to reach.
"""

from fastapi import APIRouter

from app.api.deps import Intent
from app.intent.schemas import IntentDebugRequest, IntentRead

router = APIRouter(prefix="/api/intent", tags=["intent"])


@router.post(
    "/debug",
    response_model=IntentRead,
    summary="Classify a message without answering or acting on it",
)
async def debug_intent(payload: IntentDebugRequest, intent: Intent) -> IntentRead:
    """Classify one message.

    `conversation_id` is optional: supply it so a short follow-up ("do that
    one") can be read against the preceding turns, or omit it to classify the
    message in isolation.

    Costs one bounded model call. Never retries, never recurses. A failure
    returns an `unknown` classification with every capability flag false
    rather than an error, because that is what the chat path would do.
    """
    result = await intent.understand(
        message=payload.message, conversation_id=payload.conversation_id
    )
    return IntentRead.model_validate(result, from_attributes=True)
