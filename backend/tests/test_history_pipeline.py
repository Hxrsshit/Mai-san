"""Stage 5C: the import pipeline end to end.

From a file in the import directory to archived history and derived memories,
through the real API and the real database.
"""

import json
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.history.models import (
    ImportedArchive,
    ImportedConversation,
    ImportedMessage,
    ImportedRole,
    ImportStatus,
)
from app.memory.models import Memory, MemoryOrigin
from tests.conftest import chatgpt_export

pytestmark = pytest.mark.anyio


#: One extracted memory, in the shape the extractor's parser expects.
ONE_MEMORY = json.dumps(
    {
        "should_store_memory": True,
        "memories": [
            {
                "content": "User prefers PostgreSQL for personal projects.",
                "memory_type": "preference",
                "importance_score": 7,
                "confidence_score": 0.9,
            }
        ],
    }
)

LONG_USER_TEXT = (
    "I have decided to use PostgreSQL for all of my personal projects from "
    "now on, mostly because of the JSON support and how well it handles "
    "concurrent writes compared with the alternatives I tried before. "
) * 2


async def start_import(client, filename):
    response = await client.post("/api/history/imports", json={"filename": filename})
    assert response.status_code in (201, 400), response.text
    return response


async def rows(import_session_factory, model):
    async with import_session_factory() as session:
        return (await session.execute(select(model))).scalars().all()


# --- The happy path -----------------------------------------------------------


async def test_an_export_is_archived_with_its_messages(
    import_client, write_export, import_session_factory
) -> None:
    name = write_export(
        [
            {
                "id": "c1",
                "title": "Databases",
                "turns": [("user", "I like PostgreSQL."), ("assistant", "Noted.")],
            }
        ]
    )
    response = await start_import(import_client, name)
    body = response.json()

    assert response.status_code == 201
    assert body["status"] in (ImportStatus.COMPLETED.value, ImportStatus.PARTIAL.value)
    assert body["conversations_imported"] == 1
    assert body["messages_imported"] == 2
    assert body["already_imported"] is False

    conversations = await rows(import_session_factory, ImportedConversation)
    messages = await rows(import_session_factory, ImportedMessage)
    assert len(conversations) == 1
    assert conversations[0].title == "Databases"
    assert {m.role for m in messages} == {ImportedRole.USER, ImportedRole.ASSISTANT}


async def test_imported_history_does_not_become_a_live_conversation(
    import_client, write_export, import_session_factory
) -> None:
    """The whole point of separate tables: history is not continuable."""
    from app.database.models import Conversation, Message

    name = write_export([{"id": "c1", "turns": [("user", "hello from 2023")]}])
    await start_import(import_client, name)

    assert await rows(import_session_factory, Conversation) == []
    assert await rows(import_session_factory, Message) == []

    listed = await import_client.get("/api/conversations")
    assert listed.json()["items"] == []


# --- Idempotency ----------------------------------------------------------------


async def test_importing_the_same_file_twice_does_nothing_the_second_time(
    import_client, write_export, import_session_factory
) -> None:
    name = write_export([{"id": "c1", "turns": [("user", "hello")]}])

    first = (await start_import(import_client, name)).json()
    second = (await start_import(import_client, name)).json()

    assert second["already_imported"] is True
    assert second["id"] == first["id"]
    assert len(await rows(import_session_factory, ImportedArchive)) == 1
    assert len(await rows(import_session_factory, ImportedConversation)) == 1


async def test_a_renamed_copy_is_recognised_as_the_same_export(
    import_client, import_dir, write_export, import_session_factory
) -> None:
    """Idempotency is content-addressed, not name-addressed."""
    name = write_export([{"id": "c1", "turns": [("user", "hello")]}])
    (import_dir / "renamed.zip").write_bytes((import_dir / name).read_bytes())

    first = (await start_import(import_client, name)).json()
    second = (await start_import(import_client, "renamed.zip")).json()

    assert second["already_imported"] is True
    assert second["id"] == first["id"]
    assert len(await rows(import_session_factory, ImportedArchive)) == 1


# --- Derived memories -------------------------------------------------------------


async def test_a_derived_memory_carries_imported_provenance(
    import_client, write_export, import_session_factory, fake_provider
) -> None:
    fake_provider.extraction_reply = ONE_MEMORY
    name = write_export([{"id": "c1", "turns": [("user", LONG_USER_TEXT)]}])

    body = (await start_import(import_client, name)).json()
    assert body["memories_derived"] == 1

    memories = await rows(import_session_factory, Memory)
    assert len(memories) == 1
    memory = memories[0]
    assert memory.origin is MemoryOrigin.IMPORTED
    assert memory.source_imported_message_id is not None
    assert memory.source_conversation_id is None


async def test_stated_at_comes_from_the_export_not_the_clock(
    import_client, write_export, import_session_factory, fake_provider
) -> None:
    """The column conflict resolution judges recency on."""
    fake_provider.extraction_reply = ONE_MEMORY
    # 2021-01-01T00:00:00Z
    name = write_export(
        [{"id": "c1", "created": 1609459200.0, "turns": [("user", LONG_USER_TEXT)]}]
    )
    await start_import(import_client, name)

    memory = (await rows(import_session_factory, Memory))[0]
    assert memory.stated_at.year == 2021
    assert memory.created_at.year >= 2026
    assert memory.stated_at < memory.created_at


async def test_only_user_messages_produce_memories(
    import_client, write_export, import_session_factory, fake_provider
) -> None:
    """The authority boundary, enforced by the role filter in the query.

    The assistant turn is long enough to clear the extraction threshold on its
    own. If assistant content were eligible, this conversation would produce a
    memory anchored to it.
    """
    fake_provider.extraction_reply = ONE_MEMORY
    name = write_export(
        [
            {
                "id": "c1",
                "turns": [
                    ("user", "hi"),
                    ("assistant", LONG_USER_TEXT),
                    ("system", LONG_USER_TEXT),
                ],
            }
        ]
    )
    body = (await start_import(import_client, name)).json()

    assert body["messages_imported"] == 3
    assert body["memories_derived"] == 0
    assert await rows(import_session_factory, Memory) == []


async def test_the_extractor_never_receives_assistant_text(
    import_client, write_export, fake_provider
) -> None:
    """Structural, not prompted: there is no assistant text to misread."""
    fake_provider.extraction_reply = ONE_MEMORY
    marker = "ASSISTANT SAID THIS SPECIFIC THING"
    name = write_export(
        [{"id": "c1", "turns": [("user", LONG_USER_TEXT), ("assistant", marker)]}]
    )
    await start_import(import_client, name)

    assert fake_provider.extraction_calls, "extraction should have run"
    for call in fake_provider.extraction_calls:
        for message in call:
            assert marker not in message.content


async def test_a_short_conversation_costs_no_model_call(
    import_client, write_export, fake_provider
) -> None:
    """Most of an export is small talk; it must not become a request each."""
    name = write_export([{"id": "c1", "turns": [("user", "thanks!")]}])
    await start_import(import_client, name)
    assert fake_provider.extraction_calls == []


async def test_the_extraction_budget_is_a_hard_cap(
    import_client, import_settings, write_export, fake_provider, import_session_factory
) -> None:
    fake_provider.extraction_reply = ONE_MEMORY
    name = write_export(
        [
            {"id": f"c{i}", "turns": [("user", LONG_USER_TEXT + f" case {i}")]}
            for i in range(6)
        ]
    )
    import_settings.IMPORT_MAX_EXTRACTION_CALLS = 2
    await start_import(import_client, name)

    assert len(fake_provider.extraction_calls) == 2


async def test_a_duplicate_of_an_existing_memory_is_not_stored_twice(
    import_client, write_export, import_session_factory, fake_provider
) -> None:
    """Imported knowledge goes through the live deduplication path."""
    fake_provider.extraction_reply = ONE_MEMORY
    first = write_export(
        [{"id": "c1", "turns": [("user", LONG_USER_TEXT)]}], name="one.zip"
    )
    second = write_export(
        [{"id": "c2", "turns": [("user", LONG_USER_TEXT + " again")]}],
        name="two.zip",
    )
    await start_import(import_client, first)
    await start_import(import_client, second)

    memories = await rows(import_session_factory, Memory)
    assert len(memories) == 1


# --- Failure and recovery -----------------------------------------------------------


async def test_a_broken_export_is_refused_without_an_archive_row(
    import_client, import_dir, import_session_factory
) -> None:
    path = import_dir / "broken.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", "{{{ not json")

    response = await import_client.post(
        "/api/history/imports", json={"filename": "broken.zip"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == ImportStatus.FAILED.value
    assert body["error_code"] == "malformed_json"
    # The failure is recorded so a retry can see it, and nothing was archived.
    assert len(await rows(import_session_factory, ImportedArchive)) == 1
    assert await rows(import_session_factory, ImportedConversation) == []


async def test_a_failed_run_records_a_code_not_an_exception_string(
    import_client, import_dir
) -> None:
    """A parser exception can embed the document fragment that broke it."""
    path = import_dir / "bad.json"
    path.write_bytes(b"\xff\xfe not utf8 at all")

    response = await import_client.post(
        "/api/history/imports", json={"filename": "bad.json"}
    )
    body = response.json()
    code = body.get("error_code") or response.json().get("detail")
    assert code in {"not_utf8", "malformed_json"}


async def test_an_unknown_filename_is_a_bad_request(import_client) -> None:
    response = await import_client.post(
        "/api/history/imports", json={"filename": "nope.zip"}
    )
    assert response.status_code == 400
    # The app wraps every HTTPException in its own envelope, so the reason
    # code travels in `error.message`. Asserted in the shape the API actually
    # has rather than the one a route author might assume.
    assert response.json()["error"]["message"] == "source_not_found"


# --- The API surface ------------------------------------------------------------------


async def test_sources_lists_metadata_only(
    import_client, write_export
) -> None:
    write_export([{"id": "c1", "turns": [("user", "secret content here")]}])
    response = await import_client.get("/api/history/sources")
    body = response.json()

    assert response.status_code == 200
    assert body["total"] == 1
    source = body["sources"][0]
    assert set(source) == {"filename", "size_bytes", "modified_at"}
    assert "secret content here" not in response.text


async def test_a_run_can_be_fetched_by_id(import_client, write_export) -> None:
    name = write_export([{"id": "c1", "turns": [("user", "hello")]}])
    created = (await start_import(import_client, name)).json()

    response = await import_client.get(f"/api/history/imports/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_an_unknown_run_is_a_404(import_client) -> None:
    response = await import_client.get(f"/api/history/imports/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_runs_are_listed_newest_first(import_client, write_export) -> None:
    first = write_export([{"id": "c1", "turns": [("user", "a")]}], name="a.zip")
    second = write_export([{"id": "c2", "turns": [("user", "b")]}], name="b.zip")
    await start_import(import_client, first)
    await start_import(import_client, second)

    body = (await import_client.get("/api/history/imports")).json()
    assert body["total"] == 2
    assert body["runs"][0]["source_filename"] == "b.zip"
