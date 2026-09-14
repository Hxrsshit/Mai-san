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
from app.orchestration.schemas import OrchestrationRead
from app.planning.schemas import PlanningRead
from app.memory.tasks import run_memory_extraction
from app.calendar.schemas import CalendarOutcome
from app.schemas.calendar import CalendarRead
from app.schemas.mail import MailRead
from app.schemas.workflow import WorkflowRead
from app.schemas.message import (
    ChatResponse,
    MessageCreate,
    MessageRead,
    ResearchRead,
)


def composition_touched_personal_data(workflow) -> bool:
    """Whether a composition turn reached the calendar or the web.

    Stage 4H compositions read the same private calendar and the same
    untrusted web content the single-capability paths read, so they inherit
    the same rule: nothing from this turn becomes a long-term memory merely
    because Mai went and looked at something.

    Keyed on the workflow having *attempted* either, not on success. A failed
    calendar read still means the assistant's reply is about the user's
    schedule, and a failed search still means the turn was a composition
    rather than a conversation.
    """
    if workflow is None:
        return False
    return bool(
        workflow.calendar_read
        or workflow.calendar_block
        or workflow.research_attempted
        or workflow.research_block
    )


def mail_touched_personal_data(mail) -> bool:
    """Whether this turn involved the user's mailbox at all.

    Stage 5B inherits the rule Stage 4F-G set for the calendar and Stage 4H
    extended to compositions: nothing from this turn becomes a long-term
    memory merely because Mai went and looked at something.

    It matters more here. An email routinely states a fact about the user --
    "your meeting with Netflix is tomorrow" -- and extracting it would teach
    Mai something the user never told it, from a message a stranger may have
    sent. Keyed on the turn having *touched* mail, not on the read having
    succeeded.
    """
    return bool(mail is not None and mail.touched_mail)


def touches_personal_data(calendar) -> bool:
    """Whether this turn involved the user's calendar at all.

    A named function rather than an inline boolean because it is a rule, not a
    condition: *any* calendar outcome except "this was not a calendar turn"
    suppresses memory extraction. Mutation testing is why -- narrowing it to
    `is COMPLETED` broke nothing, since no test could reach the rule directly
    and the end-to-end turns that could were the completed ones.

    Deliberately inclusive of the failure states. A turn that asked which day
    the user meant, or that failed to reach Google, is still a turn *about*
    their schedule, and the cost of suppressing extraction on it is one
    forgotten pleasantry. The cost of the other mistake is a permanent memory
    of a medical appointment.
    """
    if calendar is None:
        return False
    return calendar.outcome is not CalendarOutcome.NOT_CALENDAR


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
    (
        user_message,
        assistant_message,
        intent,
        planning,
        orchestration,
        research,
        workflow,
        calendar,
        mail,
    ) = await chat.send_message(
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
    # Stage 4F-G. A turn that read the calendar is not extracted from.
    #
    # The events never reach the extraction path directly, but the assistant's
    # reply does -- and on a calendar turn that reply *is* a summary of the
    # events. Without this, "you have a design review with Priya at 9" becomes
    # a permanent memory about Priya, which is exactly the automatic
    # persistence of personal data the stage forbids.
    #
    # Request-scoped means request-scoped. A user who wants Mai to remember
    # something from their calendar can say so, and that is an ordinary turn.
    reads_personal_data = (
        touches_personal_data(calendar)
        or composition_touched_personal_data(workflow)
        or mail_touched_personal_data(mail)
    )

    if (
        settings.MEMORY_ENABLED
        and settings.MEMORY_EXTRACTION_ENABLED
        and not reads_personal_data
    ):
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
        # Stage 4B plan, reported alongside the turn. Inert data: it never
        # reached the prompt, and there is nothing here that could run it.
        planning=PlanningRead.from_result(planning),
        # Stage 4D outcome. Reported with `executed: false` on the wire so
        # a client cannot mistake an authorization decision for a result.
        orchestration=OrchestrationRead.from_result(orchestration),
        # Stage 4F-D. Reported so a client can tell a confirmation prompt
        # apart from an answer -- and so "did a search actually run?" is a
        # field rather than something to infer from the reply text.
        research=ResearchRead.from_result(research),
        # Stage 4F-E. The minimum a client needs to distinguish a workflow
        # proposal from an answer, and to know whether a file was actually
        # written. No fingerprint, no plan internals, no filesystem path
        # beyond the workspace-relative name the user was already shown.
        workflow=WorkflowRead.from_result(workflow),
        mail=MailRead.from_result(mail),
        # Stage 4F-G. Whether a calendar read happened and how many
        # events it found -- never the events, which are private.
        calendar=CalendarRead.from_result(calendar),
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
