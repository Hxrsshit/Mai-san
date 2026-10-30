"""Stage 7B unit 9: an imported archive belongs to the member who imported it.

Archives carry the importer's scope. Another member, a member of another
organisation, and an unattributed (legacy) archive are invisible to a scope:
runs, a run by id, the "already imported" answer, and the imported
conversations and messages behind them. Derived memories go only through a
memory service of the same scope. The import endpoints stay Admin-only (the
sources are files mounted on the server). Rows are synthetic.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.history.models import ImportedArchive, ImportedConversation, ImportedMessage, ImportStatus
from app.history.service import HistoryImportService
from app.identity.scope import InvalidScope
from app.memory.service import MemoryService
from tests.scopes import ADMIN_SCOPE, COLLEAGUE_SCOPE, OUTSIDER_SCOPE

pytestmark = pytest.mark.anyio

OTHERS = [pytest.param(COLLEAGUE_SCOPE, id="colleague"), pytest.param(OUTSIDER_SCOPE, id="outsider")]
EXPORT = [{"id": "c1", "title": "Plans", "created": 1_700_000_000,
           "turns": [("user", "I am building Mai."), ("assistant", "Nice.")]}]


def _service(session, settings, scope):
    return HistoryImportService(session, settings=settings, scope=scope)


async def _import(session_factory, settings, scope, filename):
    async with session_factory() as session:
        outcome = await _service(session, settings, scope).import_file(filename)
        await session.commit()
        return outcome


def test_the_service_refuses_anything_but_a_scope() -> None:
    for bogus in (None, "personal", ADMIN_SCOPE.write_columns(), uuid.uuid4()):
        with pytest.raises(InvalidScope):
            HistoryImportService(object(), scope=bogus)  # type: ignore[arg-type]


def test_a_memory_service_of_another_scope_is_refused() -> None:
    with pytest.raises(InvalidScope):
        HistoryImportService(object(), memory_service=MemoryService(object(), scope=COLLEAGUE_SCOPE),
                             scope=ADMIN_SCOPE)


async def test_an_archive_carries_its_importers_scope(
    session_factory, import_settings, write_export
) -> None:
    outcome = await _import(session_factory, import_settings, ADMIN_SCOPE, write_export(EXPORT))
    assert ADMIN_SCOPE.owns(outcome.archive) and not outcome.already_imported


@pytest.mark.parametrize("other", OTHERS)
async def test_another_scope_never_sees_the_archive_or_what_it_holds(
    session_factory, import_settings, write_export, other
) -> None:
    mine = await _import(session_factory, import_settings, ADMIN_SCOPE, write_export(EXPORT))
    async with session_factory() as session:
        theirs = _service(session, import_settings, other)
        assert await theirs.list_runs() == ([], 0)
        assert await theirs.get_run(mine.archive.id) is None
        assert await theirs._conversations_for(mine.archive.id) == []
        [conversation_id] = (await session.execute(select(ImportedConversation.id))).scalars().all()
        assert await theirs._extractable_messages(conversation_id) == []
        own = _service(session, import_settings, ADMIN_SCOPE)
        assert [c.id for c in await own._conversations_for(mine.archive.id)] == [conversation_id]
        assert len(await own._extractable_messages(conversation_id)) == 1   # the user turn only


async def test_the_same_file_is_one_archive_per_scope_not_one_globally(
    session_factory, import_settings, write_export
) -> None:
    """Before unit 9 the colleague's import of the same bytes answered
    "already imported" with the Admin's archive."""
    filename = write_export(EXPORT)
    mine = await _import(session_factory, import_settings, ADMIN_SCOPE, filename)
    theirs = await _import(session_factory, import_settings, COLLEAGUE_SCOPE, filename)
    again = await _import(session_factory, import_settings, ADMIN_SCOPE, filename)
    assert not theirs.already_imported and theirs.archive.id != mine.archive.id
    assert COLLEAGUE_SCOPE.owns(theirs.archive)
    assert again.already_imported and again.archive.id == mine.archive.id


async def test_a_legacy_archive_is_quarantined(session_factory, import_settings, write_export) -> None:
    filename = write_export(EXPORT)
    async with session_factory() as session:
        from app.history.sources import resolve, sha256_of

        digest = sha256_of(resolve(filename, import_settings), import_settings)
        session.add(ImportedArchive(source_filename=filename, source_sha256=digest, source_bytes=1,
                                    import_format="chatgpt_zip", status=ImportStatus.COMPLETED))
        await session.commit()
    async with session_factory() as session:
        assert await _service(session, import_settings, ADMIN_SCOPE).list_runs() == ([], 0)
    fresh = await _import(session_factory, import_settings, ADMIN_SCOPE, filename)
    assert not fresh.already_imported and ADMIN_SCOPE.owns(fresh.archive)
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(ImportedMessage))).scalar_one() >= 1
