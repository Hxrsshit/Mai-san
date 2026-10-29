"""Stage 7B L0: migration 0021 (knowledge scope) on real PostgreSQL.

The permanent form of the 2026-10-28 disposable-container verification
(security review M4). SQLite hides PostgreSQL-specific behaviour (`AGENTS.md`
4.6), so the scope migration is exercised here on the dialect it ships to:

- the upgrade from 0020 adds exactly the scope columns, CHECKs and indexes,
  and nothing else changes;
- every legacy row survives unchanged, with only `organisation_id` backfilled;
- every CHECK accepts and refuses as declared, on all ten tables;
- the upgrade refuses, changing nothing, without the default organisation;
- a failure part-way through rolls back every table already altered;
- the downgrade refuses -- on every table -- a scoped row or a row of another
  organisation, changing nothing; otherwise it is clean and re-upgradable;
- the migrated schema agrees with the ORM's scope definitions.

**Gated and fail-closed.** Runs only when `TEST_POSTGRES_URL` is set; it never
reads `.env` or the application's `DATABASE_URL`, and never falls back to
either. A configured URL that is not PostgreSQL, names the `mai` database, or
names a database without "test" in it **fails** the tests rather than
skipping them. Each test creates its own sibling database
(`<configured>_s0021_<random>`), so the role needs `CREATEDB`; the configured
database itself is only used to issue `CREATE`/`DROP DATABASE`. All rows are
synthetic.

    TEST_POSTGRES_URL=postgresql+asyncpg://USER:PASS@127.0.0.1:5432/mai_test
"""

import contextlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from tests.test_migration_compatibility import alembic, alembic_attempt

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")

needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")

DEFAULT_ORG = uuid.UUID("00000000-0000-0000-0000-00000000b1b1")
ADMIN_MEMBER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
SCOPED_TABLES = (
    "conversations", "memories", "entities", "entity_aliases", "relationships",
    "knowledge_conflicts", "reminders", "executions", "workflows", "imported_archives",
)
SCOPE_COLUMNS = ("organisation_id", "scope_id", "scope_visibility")


# ============================================================================
# The fail-closed guard (runs without PostgreSQL)
# ============================================================================


def guarded_url(raw: str) -> str:
    """`raw` if it names a dedicated PostgreSQL test database; otherwise an
    AssertionError. Never a fallback."""
    assert raw, "TEST_POSTGRES_URL is empty; there is no fallback"
    parsed = make_url(raw)
    assert parsed.get_backend_name() == "postgresql", "this module needs PostgreSQL"
    name = parsed.database or ""
    assert name != "mai", "never the real database"
    assert "test" in name, f"{name!r} is not a dedicated test database (no 'test' in its name)"
    assert re.fullmatch(r"[a-z0-9_]{1,40}", name), "a plain lower-case test database name only"
    return raw


def sibling_url(raw: str, suffix: str) -> str:
    """The URL of a throwaway sibling of the configured test database."""
    parsed = make_url(guarded_url(raw))
    name = f"{parsed.database}_s0021_{suffix}"
    assert re.fullmatch(r"[a-z0-9_]{1,63}", name) and "test" in name
    return parsed.set(database=name).render_as_string(hide_password=False)


@pytest.mark.parametrize("raw", [
    "",
    "postgresql+asyncpg://u:p@127.0.0.1:5432/mai",
    "postgresql+asyncpg://u:p@127.0.0.1:5432/postgres",
    "postgresql+asyncpg://u:p@127.0.0.1:5432/production",
    "postgresql+asyncpg://u:p@127.0.0.1:5432/Mai-Test",
    "postgresql+asyncpg://u:p@127.0.0.1:5432",
    "sqlite+aiosqlite:///mai_test.db",
])
def test_the_guard_refuses_anything_but_a_dedicated_test_database(raw) -> None:
    with pytest.raises(AssertionError):
        guarded_url(raw)


def test_the_guard_accepts_a_test_database_and_derives_test_siblings() -> None:
    raw = "postgresql+asyncpg://u:p@127.0.0.1:5432/mai_test"
    assert guarded_url(raw) == raw
    sibling = make_url(sibling_url(raw, "ab12cd34"))
    assert sibling.database == "mai_test_s0021_ab12cd34"
    assert (sibling.host, sibling.port, sibling.username) == ("127.0.0.1", 5432, "u")


# ============================================================================
# Helpers
# ============================================================================


def engine(url: str, **options):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(url, **options)


@contextlib.asynccontextmanager
async def scratch_database() -> AsyncIterator[str]:
    """A fresh, empty sibling database, dropped afterwards."""
    url = sibling_url(POSTGRES_URL, uuid.uuid4().hex[:8])
    name = make_url(url).database
    admin = engine(guarded_url(POSTGRES_URL), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield url
        finally:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def fetch(url: str, sql: str, params=None) -> List:
    eng = engine(url)
    try:
        async with eng.connect() as connection:
            return list((await connection.execute(text(sql), params or {})).all())
    finally:
        await eng.dispose()


async def execute(url: str, sql: str, params=None) -> None:
    eng = engine(url)
    try:
        async with eng.begin() as connection:
            await connection.execute(text(sql), params or {})
    finally:
        await eng.dispose()


async def version(url: str) -> str:
    [(found,)] = await fetch(url, "SELECT version_num FROM alembic_version")
    return found


async def schema(url: str) -> Dict[str, List]:
    """Every column, constraint and index in `public`."""
    return {
        "columns": [tuple(r) for r in await fetch(url,
            "SELECT table_name, column_name, data_type, character_maximum_length,"
            " is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = 'public' ORDER BY 1, 2")],
        "constraints": [tuple(r) for r in await fetch(url,
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)"
            " FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2")],
        "indexes": [tuple(r) for r in await fetch(url,
            "SELECT tablename, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = 'public' ORDER BY 1, 2")],
    }


async def rows(url: str, drop_scope: bool = False) -> Dict[str, List[dict]]:
    """Every row of every scoped table, whole; optionally without the scope columns."""
    found = {}
    for table in SCOPED_TABLES:
        records = [json.loads(r[0]) for r in await fetch(
            url, f"SELECT row_to_json(x)::text FROM {table} x ORDER BY id")]
        if drop_scope:
            for record in records:
                for column in SCOPE_COLUMNS:
                    record.pop(column, None)
        found[table] = records
    return found


# --- synthetic legacy rows at 0020 -------------------------------------------

ENUM = object()
#: Enum labels the seed avoids: they require further columns by CHECK.
STATEFUL_LABELS = {"cancelled", "approved", "succeeded", "failed"}


async def _insert(connection, table: str, values: dict) -> None:
    columns, placeholders, binds = [], [], {}
    for i, (column, value) in enumerate(values.items()):
        key = f"p{i}"
        columns.append(column)
        if value is ENUM:
            udt = (await connection.execute(text(
                "SELECT udt_name FROM information_schema.columns"
                " WHERE table_name = :t AND column_name = :c"), {"t": table, "c": column})).scalar_one()
            labels = [r[0] for r in (await connection.execute(text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
                " WHERE t.typname = :u ORDER BY e.enumsortorder"), {"u": udt})).all()]
            placeholders.append(f'CAST(CAST(:{key} AS text) AS "{udt}")')
            binds[key] = [label for label in labels if label not in STATEFUL_LABELS][0]
        elif isinstance(value, dict):
            placeholders.append(f"CAST(CAST(:{key} AS text) AS jsonb)")
            binds[key] = json.dumps(value)
        else:
            placeholders.append(f":{key}")
            binds[key] = value
    await connection.execute(text(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"), binds)


async def seed_legacy_rows(url: str) -> None:
    """At least one row in every scoped table, written as the old releases did."""
    conversation, m1, m2, e1, e2, workflow = (uuid.uuid4() for _ in range(6))
    eng = engine(url)
    try:
        async with eng.begin() as c:
            await _insert(c, "conversations", {"id": conversation, "title": "legacy"})
            for memory, content in ((m1, "legacy fact one"), (m2, "legacy fact two")):
                await _insert(c, "memories", {
                    "id": memory, "content": content, "normalized_content": content,
                    "memory_type": ENUM, "status": ENUM, "importance_score": 5,
                    "confidence_score": 0.5, "source_conversation_id": conversation})
            for entity, name in ((e1, "Legacy One"), (e2, "Legacy Two")):
                await _insert(c, "entities", {
                    "id": entity, "canonical_name": name, "normalized_name": name.lower(),
                    "entity_type": ENUM, "status": ENUM})
            await _insert(c, "entity_aliases", {
                "id": uuid.uuid4(), "entity_id": e1, "alias": "L1", "normalized_alias": "l1"})
            await _insert(c, "relationships", {
                "id": uuid.uuid4(), "source_entity_id": e1, "target_entity_id": e2,
                "relationship_type": ENUM, "confidence_score": 0.5, "status": ENUM})
            await _insert(c, "knowledge_conflicts", {
                "id": uuid.uuid4(), "older_memory_id": m1, "newer_memory_id": m2,
                "resolution": ENUM, "reason": ENUM})
            await _insert(c, "reminders", {
                "id": uuid.uuid4(), "text": "legacy reminder", "state": ENUM, "recurrence": ENUM,
                "next_run_at": datetime(2030, 1, 1, tzinfo=timezone.utc), "timezone_name": "UTC",
                "fire_count": 0, "failure_count": 0, "conversation_id": conversation})
            await _insert(c, "workflows", {
                "id": workflow, "kind": "legacy", "state": ENUM, "plan": {},
                "conversation_id": conversation})
            await _insert(c, "executions", {
                "id": uuid.uuid4(), "tool_name": "legacy", "arguments": {},
                "authorization_status": ENUM, "state": ENUM, "idempotency_key": "legacy-1",
                "conversation_id": conversation, "workflow_id": workflow})
            await _insert(c, "imported_archives", {
                "id": uuid.uuid4(), "source_filename": "a.zip", "source_sha256": "a" * 64,
                "source_bytes": 1, "import_format": ENUM, "status": ENUM,
                "conversations_imported": 0, "messages_imported": 0, "conversations_skipped": 0,
                "messages_skipped": 0, "redactions": 0, "memories_derived": 0})
    finally:
        await eng.dispose()


@contextlib.asynccontextmanager
async def legacy_at_0020() -> AsyncIterator[str]:
    async with scratch_database() as url:
        alembic(url, "0020")
        await seed_legacy_rows(url)
        found = await rows(url)
        assert all(found[t] for t in SCOPED_TABLES), {t: len(found[t]) for t in SCOPED_TABLES}
        yield url


async def _delete_with_dependents(connection, table: str, column: str, value) -> None:
    """Delete the rows of `table` whose `column` is `value`, children first."""
    children = (await connection.execute(text(
        "SELECT k.conrelid::regclass::text, a.attname FROM pg_constraint k"
        " JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]"
        " WHERE k.contype = 'f' AND k.confrelid = CAST(:t AS regclass)"), {"t": table})).all()
    ids = [r[0] for r in (await connection.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :v"), {"v": value})).all()]
    for child_table, child_column in children:
        for row_id in ids:
            await _delete_with_dependents(connection, child_table, child_column, row_id)
    await connection.execute(text(f"DELETE FROM {table} WHERE {column} = :v"), {"v": value})


# ============================================================================
# Upgrade
# ============================================================================


@needs_postgres
@pytest.mark.anyio
async def test_the_upgrade_adds_exactly_the_scope_schema() -> None:
    async with legacy_at_0020() as url:
        before = await schema(url)
        alembic(url, "head")
        assert await version(url) == "0021"
        after = await schema(url)

    added = {k: sorted(set(after[k]) - set(before[k])) for k in before}
    removed = {k: sorted(set(before[k]) - set(after[k])) for k in before}
    assert removed == {"columns": [], "constraints": [], "indexes": []}
    assert added["columns"] == sorted(
        (t, c, kind, length, "YES", None) for t in SCOPED_TABLES for c, kind, length in (
            ("organisation_id", "uuid", None), ("scope_id", "uuid", None),
            ("scope_visibility", "character varying", 16)))
    assert [(t, n) for t, n, _ in added["constraints"]] == sorted(
        (t, f"ck_{t}_knowledge_scope_valid") for t in SCOPED_TABLES)
    assert all(d.startswith("CHECK ") for _, _, d in added["constraints"])
    assert [(t, n) for t, n, _ in added["indexes"]] == sorted(
        (t, f"ix_{t}_scope_id") for t in SCOPED_TABLES)
    for _, _, definition in added["indexes"]:
        assert "UNIQUE" not in definition and definition.endswith("USING btree (scope_id)")


@needs_postgres
@pytest.mark.anyio
async def test_legacy_rows_survive_and_only_the_organisation_is_backfilled() -> None:
    async with legacy_at_0020() as url:
        before = await rows(url)
        alembic(url, "head")
        assert await rows(url, drop_scope=True) == before
        for table in SCOPED_TABLES:
            [(wrong,)] = await fetch(url,
                f"SELECT COUNT(*) FROM {table} WHERE organisation_id IS DISTINCT FROM :org"
                " OR scope_id IS NOT NULL OR scope_visibility IS NOT NULL", {"org": DEFAULT_ORG})
            assert wrong == 0, table


CHECK_CASES = [  # (organisation, scope, visibility, accepted)
    (None, None, None, True),
    (DEFAULT_ORG, None, None, True),
    (DEFAULT_ORG, ADMIN_MEMBER, "personal", True),
    (DEFAULT_ORG, DEFAULT_ORG, "organisation", True),
    (DEFAULT_ORG, ADMIN_MEMBER, None, False),
    (None, None, "personal", False),
    (None, ADMIN_MEMBER, "personal", False),
    (DEFAULT_ORG, DEFAULT_ORG, "personal", False),
    (DEFAULT_ORG, ADMIN_MEMBER, "organisation", False),
    (DEFAULT_ORG, ADMIN_MEMBER, "shared", False),
]


@needs_postgres
@pytest.mark.anyio
async def test_every_check_accepts_and_refuses_as_declared() -> None:
    """On all ten tables; each attempt in a transaction that is rolled back."""
    wrong = []
    async with legacy_at_0020() as url:
        alembic(url, "head")
        eng = engine(url)
        try:
            async with eng.connect() as connection:
                for table in SCOPED_TABLES:
                    for organisation, scope, visibility, accepted in CHECK_CASES:
                        transaction = await connection.begin()
                        try:
                            await connection.execute(text(
                                f"UPDATE {table} SET organisation_id = :o, scope_id = :s,"
                                " scope_visibility = :v"), {"o": organisation, "s": scope, "v": visibility})
                            outcome = True
                        except IntegrityError as exc:
                            outcome = (False if f"ck_{table}_knowledge_scope_valid" in str(exc.orig)
                                       else str(exc.orig))
                        finally:
                            await transaction.rollback()
                        if outcome is not accepted:
                            wrong.append((table, organisation, scope, visibility, outcome))
        finally:
            await eng.dispose()
    assert wrong == []


@needs_postgres
@pytest.mark.anyio
async def test_the_upgrade_refuses_without_the_default_organisation_and_changes_nothing() -> None:
    async with legacy_at_0020() as url:
        eng = engine(url)
        try:
            async with eng.begin() as connection:
                await _delete_with_dependents(connection, "organisations", "id", DEFAULT_ORG)
        finally:
            await eng.dispose()
        before_schema, before_rows = await schema(url), await rows(url)

        result = alembic_attempt(url, "upgrade", "head")
        assert result.returncode != 0
        assert "0021 refused" in result.stdout + result.stderr
        assert await version(url) == "0020"
        assert await schema(url) == before_schema
        assert await rows(url) == before_rows


@needs_postgres
@pytest.mark.anyio
async def test_a_failure_part_way_through_rolls_back_every_table() -> None:
    """An object named like 0021's ninth index (workflows) makes the upgrade
    fail after eight tables have been altered. PostgreSQL DDL is
    transactional and `env.py` runs the upgrade in one transaction, so none
    of the eight may remain."""
    async with legacy_at_0020() as url:
        await execute(url, "CREATE INDEX ix_workflows_scope_id ON conversations (title)")
        before_schema, before_rows = await schema(url), await rows(url)

        result = alembic_attempt(url, "upgrade", "head")
        assert result.returncode != 0
        assert "ix_workflows_scope_id" in result.stdout + result.stderr
        assert await version(url) == "0020"
        assert await schema(url) == before_schema
        assert await rows(url) == before_rows
        [(left,)] = await fetch(url,
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = 'public'"
            " AND column_name IN ('scope_id', 'scope_visibility')")
        assert left == 0


# ============================================================================
# Downgrade
# ============================================================================


async def _assert_each_downgrade_refused(url: str, changes) -> None:
    """Apply each change to one row, check the downgrade refuses naming that
    table and changes nothing, then undo it. Finally the downgrade succeeds,
    so the refusals were caused by the rows."""
    failures = []
    for table, sql, params in changes:
        [(row_id,)] = await fetch(url, f"SELECT id FROM {table} ORDER BY id LIMIT 1")
        await execute(url, sql.format(table=table), {"id": row_id, **params})
        before_schema, before_rows = await schema(url), await rows(url)

        result = alembic_attempt(url, "downgrade", "0020")
        output = result.stdout + result.stderr
        if not (result.returncode != 0 and "0021 downgrade refused" in output
                and f"{table}: 1" in output and await version(url) == "0021"
                and await schema(url) == before_schema and await rows(url) == before_rows):
            failures.append((table, sql, result.returncode))
        await execute(url, f"UPDATE {table} SET organisation_id = :org, scope_id = NULL,"
                           " scope_visibility = NULL WHERE id = :id", {"org": DEFAULT_ORG, "id": row_id})
    assert failures == []
    result = alembic_attempt(url, "downgrade", "0020")
    assert result.returncode == 0, result.stderr


@needs_postgres
@pytest.mark.anyio
async def test_the_downgrade_refuses_a_scoped_row_in_any_table() -> None:
    personal = [(t, "UPDATE {table} SET scope_id = :m, scope_visibility = 'personal' WHERE id = :id",
                 {"m": ADMIN_MEMBER}) for t in SCOPED_TABLES]
    shared = [("memories", "UPDATE {table} SET scope_id = :o, scope_visibility = 'organisation'"
                           " WHERE id = :id", {"o": DEFAULT_ORG})]
    async with legacy_at_0020() as url:
        alembic(url, "head")
        await _assert_each_downgrade_refused(url, personal + shared)


@needs_postgres
@pytest.mark.anyio
async def test_the_downgrade_refuses_a_row_of_another_organisation_in_any_table() -> None:
    other = [(t, "UPDATE {table} SET organisation_id = :other WHERE id = :id",
              {"other": uuid.uuid4()}) for t in SCOPED_TABLES]
    async with legacy_at_0020() as url:
        alembic(url, "head")
        await _assert_each_downgrade_refused(url, other)


@needs_postgres
@pytest.mark.anyio
async def test_a_clean_downgrade_restores_0020_and_the_upgrade_repeats_exactly() -> None:
    async with legacy_at_0020() as url:
        at_0020_schema, at_0020_rows = await schema(url), await rows(url)
        alembic(url, "head")
        at_0021_schema, at_0021_rows = await schema(url), await rows(url)

        result = alembic_attempt(url, "downgrade", "0020")
        assert result.returncode == 0, result.stderr
        assert await version(url) == "0020"
        assert await schema(url) == at_0020_schema
        assert await rows(url) == at_0020_rows

        alembic(url, "head")
        assert await version(url) == "0021"
        assert await schema(url) == at_0021_schema
        assert await rows(url) == at_0021_rows        # the backfill, recreated exactly


# ============================================================================
# The ORM
# ============================================================================


@needs_postgres
@pytest.mark.anyio
async def test_the_migrated_scope_schema_is_the_models_scope_schema() -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import CheckConstraint
    from sqlalchemy.dialects import postgresql

    from app.database.metadata import Base
    from app.database.models.scope import KNOWLEDGE_SCOPE_CHECK_SQL

    async with scratch_database() as url:
        alembic(url, "head")
        for table_name in SCOPED_TABLES:
            table = Base.metadata.tables[table_name]
            columns = {r[0]: r for r in await fetch(url,
                "SELECT column_name, data_type, character_maximum_length, is_nullable,"
                " column_default FROM information_schema.columns WHERE table_name = :t",
                {"t": table_name})}
            for name in SCOPE_COLUMNS:
                model = table.c[name]
                _, kind, length, nullable, default = columns[name]
                declared = model.type.compile(dialect=postgresql.dialect()).lower()
                assert declared == ("uuid" if kind == "uuid" else f"varchar({length})"), (table_name, name)
                assert (nullable == "YES") is bool(model.nullable), (table_name, name)
                assert default is None and model.server_default is None and model.default is None
                assert not model.foreign_keys
            checks = [c for c in table.constraints if isinstance(c, CheckConstraint)
                      and str(c.name).endswith("knowledge_scope_valid")]
            assert [str(c.sqltext) for c in checks] == [KNOWLEDGE_SCOPE_CHECK_SQL]
            assert [r[0] for r in await fetch(url,
                "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"
                " AND contype = 'c' AND conname LIKE '%knowledge_scope%'", {"t": table_name})
            ] == [checks[0].name]
            declared_indexes = {(i.name, tuple(c.name for c in i.columns), bool(i.unique))
                                for i in table.indexes if "scope" in i.name}
            migrated_indexes = {(r[0], tuple(r[2]), r[1]) for r in await fetch(url,
                "SELECT i.relname, ix.indisunique, array_agg(a.attname ORDER BY a.attnum)"
                " FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid"
                " JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = ANY(ix.indkey)"
                " WHERE ix.indrelid = CAST(:t AS regclass) AND i.relname LIKE '%scope%'"
                " GROUP BY i.relname, ix.indisunique", {"t": table_name})}
            assert migrated_indexes == declared_indexes == {
                (f"ix_{table_name}_scope_id", ("scope_id",), False)}

        # Alembic's own comparison, ORM against the migrated database: nothing
        # about the scope may differ. (Unrelated, pre-existing differences are
        # recorded as known defects in docs/CHANGELOG.md and are not judged here.)
        eng = engine(url)
        try:
            async with eng.connect() as connection:
                differences = await connection.run_sync(lambda sync: compare_metadata(
                    MigrationContext.configure(sync, opts={
                        "compare_type": True, "compare_server_default": True}),
                    Base.metadata))
        finally:
            await eng.dispose()
        about_scope = [d for d in differences if any(
            word in repr(d) for word in (*SCOPE_COLUMNS, "knowledge_scope"))]
        assert about_scope == []
