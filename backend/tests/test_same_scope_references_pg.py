"""Stage 7B unit 10: migration 0025 (same-scope references) on real PostgreSQL.

The constraints only exist on PostgreSQL (see the migration), so this is
where they are proven:

- the upgrade from 0024 adds exactly the parent keys and the composite
  foreign keys, and changes no row;
- a scoped row pointing at another scope's row (or at an unattributed one)
  makes the upgrade refuse, changing nothing;
- afterwards the database itself refuses a reference across scopes and from
  a scoped row to a legacy one, and still accepts same-scope and legacy
  references;
- CASCADE still removes a child, and SET NULL nulls only the reference --
  the child keeps its scope;
- a clean downgrade restores 0024 exactly, and the upgrade repeats exactly;
- the ORM's constraints are the migrated ones.

Gated and fail-closed exactly like `tests/test_knowledge_scope_migration_pg.py`
(whose helpers it reuses). Rows are synthetic.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.scopes import ADMIN_SCOPE, COLLEAGUE_SCOPE
from tests.test_knowledge_scope_migration_pg import (
    fetch,
    legacy_at_0020,
    needs_postgres,
    rows,
    schema,
    version,
)
from tests.test_migration_compatibility import alembic, alembic_attempt

PARENTS = ("conversations", "entities", "memories", "relationships", "workflows")


def _sessions(url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _conversation(maker, scope=None):
    from app.database.models import Conversation

    async with maker() as session:
        row = Conversation(title="synthetic", **(scope.write_columns() if scope else {}))
        session.add(row)
        await session.commit()
        return row.id


async def _memory(maker, conversation_id, scope=None):
    from app.memory.models import Memory, MemoryStatus, MemoryType

    async with maker() as session:
        row = Memory(content=f"fact {uuid.uuid4().hex[:6]}.", normalized_content=uuid.uuid4().hex,
                     memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                     importance_score=8, confidence_score=0.9,
                     source_conversation_id=conversation_id,
                     **(scope.write_columns() if scope else {}))
        session.add(row)
        await session.commit()
        return row.id


async def _constraint_names(url) -> set:
    return {r[0] for r in await fetch(url,
        "SELECT conname FROM pg_constraint WHERE conname LIKE '%same_scope' OR conname LIKE '%_id_scope'")}


@needs_postgres
@pytest.mark.anyio
async def test_the_upgrade_adds_exactly_the_same_scope_constraints() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0025_same_scope_references.py"
    spec = importlib.util.spec_from_file_location("migration_0025", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async with legacy_at_0020() as url:
        alembic(url, "0024")
        before_schema, before_rows = await schema(url), await rows(url)
        alembic(url, "0025")
        assert await version(url) == "0025"
        assert await rows(url) == before_rows
        expected = {f"uq_{t}_id_scope" for t in PARENTS} | {
            f"fk_{t}_{c}_same_scope" for t, c, _p in migration.REFERENCES}
        assert await _constraint_names(url) == expected
        after = await schema(url)
        assert after["columns"] == before_schema["columns"]


@needs_postgres
@pytest.mark.anyio
async def test_a_reference_across_scopes_refuses_the_upgrade_and_changes_nothing() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0024")
        engine, maker = _sessions(url)
        try:
            theirs = await _conversation(maker, COLLEAGUE_SCOPE)
            await _memory(maker, theirs, ADMIN_SCOPE)          # possible before 0025
        finally:
            await engine.dispose()
        before_schema, before_rows = await schema(url), await rows(url)
        result = alembic_attempt(url, "upgrade", "0025")
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "0025 refused" in output and "memories.source_conversation_id: 1" in output
        assert await version(url) == "0024"
        assert await schema(url) == before_schema and await rows(url) == before_rows


@needs_postgres
@pytest.mark.anyio
async def test_the_database_refuses_references_across_scopes() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "head")
        engine, maker = _sessions(url)
        try:
            mine = await _conversation(maker, ADMIN_SCOPE)
            theirs = await _conversation(maker, COLLEAGUE_SCOPE)
            legacy = await _conversation(maker)
            await _memory(maker, mine, ADMIN_SCOPE)              # same scope: accepted
            await _memory(maker, legacy)                         # legacy to legacy: accepted
            with pytest.raises(IntegrityError):
                await _memory(maker, theirs, ADMIN_SCOPE)        # across scopes: refused
            with pytest.raises(IntegrityError):
                await _memory(maker, legacy, ADMIN_SCOPE)        # scoped to legacy: refused
        finally:
            await engine.dispose()


@needs_postgres
@pytest.mark.anyio
async def test_cascade_and_set_null_behave_as_before() -> None:
    from datetime import datetime, timezone

    from app.database.models import Conversation
    from app.memory.models import Memory
    from app.reminders.models import Recurrence, Reminder, ReminderState

    async with legacy_at_0020() as url:
        alembic(url, "head")
        engine, maker = _sessions(url)
        try:
            conversation = await _conversation(maker, ADMIN_SCOPE)
            memory = await _memory(maker, conversation, ADMIN_SCOPE)
            async with maker() as session:
                reminder = Reminder(text="x", state=ReminderState.SCHEDULED, recurrence=Recurrence.ONCE,
                                    next_run_at=datetime.now(timezone.utc), timezone_name="UTC",
                                    conversation_id=conversation, **ADMIN_SCOPE.write_columns())
                session.add(reminder)
                await session.commit()
                reminder_id = reminder.id
            async with maker() as session:
                await session.execute(Conversation.__table__.delete().where(
                    Conversation.__table__.c.id == conversation))
                await session.commit()
            async with maker() as session:
                assert (await session.execute(select(Memory.id).where(Memory.id == memory))).first() is None
                row = (await session.execute(select(Reminder).where(Reminder.id == reminder_id))).scalar_one()
                assert row.conversation_id is None and ADMIN_SCOPE.owns(row)   # scope kept
        finally:
            await engine.dispose()


@needs_postgres
@pytest.mark.anyio
async def test_a_clean_downgrade_restores_0024_and_the_upgrade_repeats_exactly() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0024")
        at_0024 = await schema(url)
        alembic(url, "0025")
        at_0025 = await schema(url)
        result = alembic_attempt(url, "downgrade", "0024")
        assert result.returncode == 0, result.stderr
        assert await version(url) == "0024" and await schema(url) == at_0024
        alembic(url, "0025")
        assert await schema(url) == at_0025


@needs_postgres
@pytest.mark.anyio
async def test_the_migrated_constraints_are_the_models() -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database.metadata import Base

    async with legacy_at_0020() as url:
        alembic(url, "head")
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                differences = await connection.run_sync(lambda sync: compare_metadata(
                    MigrationContext.configure(sync), Base.metadata))
        finally:
            await engine.dispose()
        about = [d for d in differences if "same_scope" in repr(d) or "_id_scope" in repr(d)]
        assert about == []
