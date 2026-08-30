"""Conversation and message endpoints."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Query, Response, status

from app.api.deps import (
    AppSettings,
    Chat,
    Conversations,
    DbSession,
    Provider,
    SessionFactory,
)
from app.schemas.common import ErrorResponse
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetail,
    ConversationList,
    ConversationRead,
    ConversationUpdate,
)
from app.intent.schemas import IntentRead
from app.memory.tasks import run_memory_extraction
from app.schemas.message import ChatResponse, MessageCreate, MessageRead

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Conversation not found"}}


@router.post(
    "",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a conversation",
)
async def create_conversation(
    payload: ConversationCreate, conversations: Conversations
) -> ConversationRead:
    conversation = await conversations.create_conversation(title=payload.title)
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=ConversationList, summary="List conversations")
async def list_conversations(
    conversations: Conversations,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConversationList:
    items = await conversations.list_conversations(limit=limit, offset=offset)
    total = await conversations.count_conversations()
    return ConversationList(
        items=[ConversationRead.model_validate(item) for item in items],
        total=total,
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    responses=NOT_FOUND,
    summary="Get a conversation with its messages",
)
async def get_conversation(
    conversation_id: uuid.UUID, conversations: Conversations
) -> ConversationDetail:
    conversation = await conversations.get_conversation(conversation_id)
    messages = await conversations.get_messages(conversation_id)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[MessageRead.model_validate(message) for message in messages],
    )


@router.patch(
    "/{conversation_id}",
    response_model=ConversationRead,
    responses=NOT_FOUND,
    summary="Rename a conversation",
)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    conversations: Conversations,
) -> ConversationRead:
    conversation = await conversations.rename_conversation(
        conversation_id, payload.title
    )
    return ConversationRead.model_validate(conversation)


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    summary="Delete a conversation",
)
async def delete_conversation(
    conversation_id: uuid.UUID, conversations: Conversations
) -> Response:
    await conversations.delete_conversation(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{conversation_id}/messages",
    response_model=ChatResponse,
    status_code=status.HTTP_201_CREATED,
    responses=NOT_FOUND,
    summary="Send a message and get Mai's reply",
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    chat: Chat,
    background_tasks: BackgroundTasks,
    settings: AppSettings,
    provider: Provider,
    session_factory: SessionFactory,
    session: DbSession,
) -> ChatResponse:
    user_message, assistant_message, intent = await chat.send_message(
        conversation_id=conversation_id, content=payload.content
    )

    # Commit the turn before queueing extraction. FastAPI closes dependency
    # scopes *after* background tasks run, so without this the request's
    # transaction would still be open when extraction starts: on PostgreSQL
    # the task would not see the messages it is meant to analyse, and on
    # SQLite the two connections deadlock until the busy timeout expires.
    await session.commit()

    # Memory extraction runs after this response is sent, on its own session.
    # It cannot delay, alter or fail the chat turn -- see app/memory/tasks.py.
    if settings.MEMORY_ENABLED and settings.MEMORY_EXTRACTION_ENABLED:
        background_tasks.add_task(
            run_memory_extraction,
            conversation_id=conversation_id,
            user_message=user_message.content,
            assistant_message=assistant_message.content,
            source_message_id=user_message.id,
            settings=settings,
            # Passed explicitly so dependency overrides are honoured.
            provider=provider,
            session_factory=session_factory,
        )

    return ChatResponse(
        conversation_id=conversation_id,
        user_message=MessageRead.model_validate(user_message),
        assistant_message=MessageRead.model_validate(assistant_message),
        # Stage 4A understanding, reported alongside the turn. Application
        # state: it never reached the prompt, and it authorises nothing.
        intent=IntentRead.model_validate(intent, from_attributes=True),
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=list[MessageRead],
    responses=NOT_FOUND,
    summary="List a conversation's messages",
)
async def list_messages(
    conversation_id: uuid.UUID, conversations: Conversations
) -> list[MessageRead]:
    await conversations.get_conversation(conversation_id)
    messages = await conversations.get_messages(conversation_id)
    return [MessageRead.model_validate(message) for message in messages]
