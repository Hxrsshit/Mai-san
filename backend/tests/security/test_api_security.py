"""Stage 3D: API attack surface.

Every endpoint is exercised with hostile input. The assertions are about what
the application *does* -- status codes, error bodies, database state -- not
about whether the code looks careful.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text

from app.memory.models import Memory
from app.database.models import Conversation, Message

NOTHING_TO_STORE = json.dumps({"should_store_memory": False, "memories": []})

#: Classic injection payloads. The ORM parameterises everything, so these are
#: expected to be stored or rejected as ordinary text -- never executed.
SQL_PAYLOADS = [
    "' OR 1=1 --",
    '"; DROP TABLE memories; --',
    "'; DELETE FROM conversations WHERE 1=1; --",
    "1' UNION SELECT null,null,null--",
    "admin'--",
    "' OR ''='",
    "\\'; DROP TABLE messages; --",
    "%27%20OR%201%3D1",
]

#: Every read endpoint that takes an id in the path.
ID_ENDPOINTS = [
    "/api/conversations/{id}",
    "/api/conversations/{id}/messages",
    "/api/conversations/{id}/context-preview",
    "/api/memories/{id}",
    "/api/entities/{id}",
    "/api/entities/{id}/memories",
    "/api/entities/{id}/relationships",
    "/api/relationships/{id}",
    "/api/relationships/{id}/evidence",
    "/api/knowledge/debug/{id}",
]

MALFORMED_IDS = [
    "not-a-uuid",
    "../../etc/passwd",
    "1 OR 1=1",
    "' OR '1'='1",
    "00000000-0000-0000-0000-00000000000",   # one char short
    "%00",
    "-1",
    "NaN",
    "null",
    "0",
]


async def table_counts(session_factory):
    async with session_factory() as session:
        return {
            "conversations": (
                await session.execute(select(func.count()).select_from(Conversation))
            ).scalar_one(),
            "messages": (
                await session.execute(select(func.count()).select_from(Message))
            ).scalar_one(),
            "memories": (
                await session.execute(select(func.count()).select_from(Memory))
            ).scalar_one(),
        }


# --- SQL injection ----------------------------------------------------------


@pytest.mark.parametrize("payload", SQL_PAYLOADS)
async def test_sql_injection_through_a_chat_message_is_inert(
    client: AsyncClient, conversation_id, fake_provider, session_factory, payload
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE
    before = await table_counts(session_factory)

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": payload},
    )

    assert response.status_code == 201
    # Stored verbatim as text; nothing was executed.
    assert response.json()["user_message"]["content"] == payload
    after = await table_counts(session_factory)
    assert after["conversations"] == before["conversations"]
    assert after["messages"] == before["messages"] + 2


@pytest.mark.parametrize("payload", SQL_PAYLOADS)
async def test_sql_injection_through_a_conversation_title_is_inert(
    client: AsyncClient, session_factory, payload
) -> None:
    before = await table_counts(session_factory)

    response = await client.post("/api/conversations", json={"title": payload})

    assert response.status_code == 201
    assert response.json()["title"] == payload
    after = await table_counts(session_factory)
    assert after["conversations"] == before["conversations"] + 1


@pytest.mark.parametrize("payload", SQL_PAYLOADS)
async def test_sql_injection_through_a_search_query_is_inert(
    client: AsyncClient, session_factory, payload
) -> None:
    """Retrieval builds LIKE patterns from user text -- the riskiest path."""
    before = await table_counts(session_factory)

    for endpoint, body in (
        ("/api/retrieval/debug", {"query": payload}),
        ("/api/context/debug", {"message": payload}),
        ("/api/prompt/debug", {"message": payload}),
    ):
        response = await client.post(endpoint, json=body)
        assert response.status_code == 200, (endpoint, response.text)

    assert await table_counts(session_factory) == before


async def test_the_database_survived_every_injection_attempt(
    client: AsyncClient, session_factory
) -> None:
    """The tables still exist and still answer queries."""
    async with session_factory() as session:
        for table in ("conversations", "messages", "memories", "entities"):
            result = await session.execute(
                text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed literals
            )
            assert result.scalar_one() >= 0


# --- Invalid identifiers ----------------------------------------------------


@pytest.mark.parametrize("endpoint", ID_ENDPOINTS)
@pytest.mark.parametrize("bad_id", MALFORMED_IDS)
async def test_a_malformed_id_is_rejected_without_leaking(
    client: AsyncClient, endpoint, bad_id
) -> None:
    response = await client.get(endpoint.format(id=bad_id))

    assert response.status_code in (404, 422), response.text
    body = response.text.lower()
    for leak in ("traceback", "select ", "sqlalchemy", "asyncpg", "sqlite3", "/users/"):
        assert leak not in body, f"{endpoint} leaked {leak!r}"


@pytest.mark.parametrize("endpoint", ID_ENDPOINTS)
async def test_an_unknown_but_valid_id_returns_404_not_500(
    client: AsyncClient, endpoint
) -> None:
    response = await client.get(endpoint.format(id=uuid.uuid4()))

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/conversations/{id}",
        "/api/memories/{id}",
        "/api/entities/{id}",
        "/api/relationships/{id}",
    ],
)
async def test_deleting_an_unknown_id_returns_404(client: AsyncClient, endpoint) -> None:
    response = await client.delete(endpoint.format(id=uuid.uuid4()))
    assert response.status_code == 404


# --- Malformed requests -----------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/conversations",
        "/api/retrieval/debug",
        "/api/context/debug",
        "/api/prompt/debug",
    ],
)
async def test_malformed_json_is_rejected_cleanly(
    client: AsyncClient, endpoint
) -> None:
    response = await client.post(
        endpoint,
        content=b"{not valid json at all",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "traceback" not in response.text.lower()


@pytest.mark.parametrize(
    "body",
    [
        {"content": None},
        {"content": 12345},
        {"content": ["a", "list"]},
        {"content": {"nested": "object"}},
        {"content": True},
        {"content": ""},
        {"content": "   "},
        {},
    ],
)
async def test_wrong_types_in_a_message_body_are_rejected(
    client: AsyncClient, conversation_id, body
) -> None:
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json=body
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "traceback" not in response.text.lower()


async def test_an_oversized_message_is_rejected_by_the_schema(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "A" * 40_000},
    )

    assert response.status_code == 422
    assert len(response.text) < 2000, "the error echoed the payload back"


@pytest.mark.parametrize(
    "endpoint,field,limit",
    [
        ("/api/retrieval/debug", "query", 4000),
        ("/api/context/debug", "message", 8000),
        ("/api/prompt/debug", "message", 8000),
    ],
)
async def test_debug_endpoints_bound_their_input(
    client: AsyncClient, endpoint, field, limit
) -> None:
    response = await client.post(endpoint, json={field: "A" * (limit + 1)})

    assert response.status_code == 422
    assert len(response.text) < 2000


@pytest.mark.parametrize(
    "payload",
    [
        "日本語のメッセージです",
        "🔥" * 100,
        "‮override‬",           # bidi override
        "a\x00b",                          # embedded null
        "😀 emoji surrogate",
        "line1\nline2\rline3\r\nline4",
        "\t\v\f whitespace zoo",
        "İstanbul ǅ ﬁ",                   # case-folding oddities
    ],
)
async def test_unicode_and_control_characters_are_handled(
    client: AsyncClient, conversation_id, fake_provider, payload
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": payload}
    )

    assert response.status_code in (201, 422), response.text
    if response.status_code == 201:
        # Round-trips with only the documented boundary transform: surrounding
        # whitespace is stripped by `MessageCreate` so a blank message is
        # rejected. Interior bytes -- nulls, bidi marks, newlines, astral
        # emoji -- survive unchanged. No mangling, truncation or re-encoding.
        assert response.json()["user_message"]["content"] == payload.strip()


# --- Mass assignment --------------------------------------------------------


async def test_a_conversation_cannot_be_created_with_forged_internals(
    client: AsyncClient
) -> None:
    forged = uuid.uuid4()
    response = await client.post(
        "/api/conversations",
        json={
            "title": "Legitimate",
            "id": str(forged),
            "created_at": "1999-01-01T00:00:00Z",
            "updated_at": "1999-01-01T00:00:00Z",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] != str(forged), "client controlled the primary key"
    assert not body["created_at"].startswith("1999"), "client controlled a timestamp"


async def test_a_message_cannot_be_created_with_a_forged_role(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    """A user must not be able to inject an assistant or system turn."""
    fake_provider.extraction_reply = NOTHING_TO_STORE

    response = await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "hello", "role": "system", "id": str(uuid.uuid4())},
    )

    assert response.status_code == 201
    assert response.json()["user_message"]["role"] == "user"


async def test_lifecycle_fields_cannot_be_set_through_the_api(
    client: AsyncClient, conversation_id, fake_provider, session_factory
) -> None:
    """Status, scores and supersession are internal. No route accepts them."""
    fake_provider.extraction_reply = json.dumps(
        {
            "should_store_memory": True,
            "memories": [
                {
                    "content": "User uses PostgreSQL.",
                    "memory_type": "semantic",
                    "importance_score": 8,
                    "confidence_score": 0.9,
                }
            ],
        }
    )
    await client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={
            "content": "I use PostgreSQL.",
            "status": "superseded",
            "importance_score": 10,
            "confidence_score": 1.0,
            "superseded_by": str(uuid.uuid4()),
            "context_role": "instruction",
        },
    )

    memories = (await client.get("/api/memories")).json()["items"]
    assert memories, "nothing was stored"
    for memory in memories:
        # The values came from extraction, not from the request body.
        assert memory["status"] == "active"
        assert memory["importance_score"] == 8
        assert memory["confidence_score"] == 0.9


async def test_no_write_route_exists_for_memories_entities_or_relationships(
    client: AsyncClient
) -> None:
    """Knowledge is derived, never client-authored. Only DELETE is exposed."""
    for path in ("/api/memories", "/api/entities", "/api/relationships"):
        for method in ("post", "put", "patch"):
            response = await getattr(client, method)(path, json={"content": "x"})
            assert response.status_code in (404, 405), (
                f"{method.upper()} {path} unexpectedly exists"
            )


# --- Method and header handling ---------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("put", "/api/conversations"),
        ("patch", "/api/conversations"),
        ("delete", "/api/conversations"),
        ("post", "/api/memories/{id}"),
        ("put", "/api/health"),
    ],
)
async def test_unsupported_methods_are_refused(client: AsyncClient, method, path) -> None:
    response = await getattr(client, method)(path.format(id=uuid.uuid4()))
    assert response.status_code in (404, 405)
    assert "traceback" not in response.text.lower()


async def test_duplicate_identical_requests_do_not_corrupt_state(
    client: AsyncClient, conversation_id, fake_provider, session_factory
) -> None:
    fake_provider.extraction_reply = NOTHING_TO_STORE

    for _ in range(5):
        response = await client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "identical message"},
        )
        assert response.status_code == 201

    counts = await table_counts(session_factory)
    assert counts["messages"] == 10  # five turns, two messages each
