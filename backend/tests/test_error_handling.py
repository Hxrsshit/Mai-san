"""Failure paths and input validation.

Each test here corresponds to a bug found during the Stage 1 audit.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from app.core.errors import DatabaseError
from app.services.conversation_service import ConversationService


# --- Blank input ------------------------------------------------------------
# min_length=1 admits "   ", which was being stored and sent to the model.


@pytest.mark.parametrize("blank", ["   ", "\t", "\n\n", " \t \n "])
async def test_whitespace_only_messages_are_rejected(
    client: AsyncClient, conversation_id, blank
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": blank}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_whitespace_only_message_never_reaches_the_model(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "   "}
    )
    assert fake_provider.calls == []


async def test_message_content_is_stripped(
    client: AsyncClient, conversation_id
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "  hello  "},
    )

    assert response.status_code == 201
    assert response.json()["user_message"]["content"] == "hello"


@pytest.mark.parametrize("blank", ["   ", "\t"])
async def test_blank_conversation_titles_are_rejected(
    client: AsyncClient, conversation_id, blank
) -> None:
    created = await client.post("/api/conversations", json={"title": blank})
    assert created.status_code == 422

    renamed = await client.patch(
        f"/api/conversations/{conversation_id}", json={"title": blank}
    )
    assert renamed.status_code == 422


# --- Database failures ------------------------------------------------------
# asyncpg raises a bare ConnectionRefusedError (an OSError) on connect failure.
# That is not a SQLAlchemyError, so it used to escape the service layer and
# surface as an opaque 500 instead of a 503.


class _BrokenSession:
    """Stands in for a session whose database has gone away."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def add(self, _obj) -> None:
        return None

    async def flush(self):
        raise self._error

    async def execute(self, *_args, **_kwargs):
        raise self._error

    async def get(self, *_args, **_kwargs):
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(61, "Connection refused"),
        OSError("network is unreachable"),
        OperationalError("SELECT 1", {}, Exception("boom")),
    ],
)
async def test_database_failures_become_database_errors(error) -> None:
    service = ConversationService(_BrokenSession(error))

    with pytest.raises(DatabaseError):
        await service.create_conversation()


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError(61, "Connection refused"), OSError("unreachable")],
)
async def test_database_failure_maps_to_503_not_500(error) -> None:
    """The status code the API actually returns."""
    service = ConversationService(_BrokenSession(error))

    with pytest.raises(DatabaseError) as caught:
        await service.list_conversations()

    assert caught.value.status_code == 503
    assert caught.value.code == "database_error"


async def test_database_failure_on_read_is_also_mapped() -> None:
    service = ConversationService(_BrokenSession(ConnectionRefusedError()))

    with pytest.raises(DatabaseError):
        await service.get_conversation(uuid.uuid4())
