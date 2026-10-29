"""Stage 7B units 5-6: migration 0022 (uniqueness per scope) on real PostgreSQL.

SQLite hides PostgreSQL-specific behaviour (`AGENTS.md` 4.6), so the partial
unique indexes are exercised here on the dialect they ship to:

- the upgrade from 0021 replaces each global unique index with a per-scope
  and a legacy partial index, changes no row, and keeps every other index;
- two scopes may hold the same key, one scope may not hold it twice, and
  unattributed rows keep their old guarantee among themselves;
- the downgrade refuses, changing nothing, when a key is shared across
  scopes (or by a scoped and a legacy row); otherwise it restores 0021's
  schema exactly and the upgrade repeats exactly;
- the migrated indexes are the models' indexes, predicates included.

**Gated and fail-closed**, with the same guard and sibling databases as
`tests/test_knowledge_scope_migration_pg.py` (whose helpers it reuses):
runs only when `TEST_POSTGRES_URL` is set, never reads `.env` or
`DATABASE_URL`, and fails rather than skips on an unsafe URL. All rows are
synthetic.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from tests.test_knowledge_scope_migration_pg import (
    DEFAULT_ORG,
    execute,
    fetch,
    legacy_at_0020,
    needs_postgres,
    rows,
    schema,
    version,
)
from tests.test_migration_compatibility import alembic, alembic_attempt

ADMIN_MEMBER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
COLLEAGUE_MEMBER = uuid.UUID("00000000-0000-0000-0000-0000000c0111")
INDEXES = {  # table -> (old global index, new name stem, key columns)
    "memories": ("uq_memories_type_normalized_content", "type_normalized_content",
                 ("memory_type", "normalized_content")),
    "entities": ("uq_entities_normalized_name", "normalized_name", ("normalized_name",)),
    "entity_aliases": ("uq_entity_aliases_normalized_alias", "normalized_alias",
                       ("normalized_alias",)),
}
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


async def _entity(url: str, name: str, member=None) -> None:
    await execute(url,
        "INSERT INTO entities (id, canonical_name, normalized_name, entity_type, status,"
        " created_at, updated_at, organisation_id, scope_id, scope_visibility)"
        " VALUES (:id, :name, :name, 'technology', 'active', :now, :now, :org, :member,"
        " CASE WHEN CAST(:member AS uuid) IS NULL THEN NULL ELSE 'personal' END)",
        {"id": uuid.uuid4(), "name": name, "now": NOW, "org": DEFAULT_ORG, "member": member})


async def _index_definitions(url: str, table: str) -> dict:
    return {name: definition for name, definition in await fetch(url,
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t", {"t": table})}


@needs_postgres
@pytest.mark.anyio
async def test_the_upgrade_swaps_each_global_index_for_two_partial_ones() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0021")
        before_schema, before_rows = await schema(url), await rows(url)
        alembic(url, "0022")
        assert await version(url) == "0022"
        assert await rows(url) == before_rows                 # no data change
        for table, (old, stem, columns) in INDEXES.items():
            found = await _index_definitions(url, table)
            assert old not in found, table
            scoped, legacy = found[f"uq_{table}_scoped_{stem}"], found[f"uq_{table}_legacy_{stem}"]
            assert scoped.startswith("CREATE UNIQUE INDEX")
            assert f"(scope_id, {', '.join(columns)})" in scoped
            assert scoped.endswith("WHERE (scope_id IS NOT NULL)")
            assert f"({', '.join(columns)})" in legacy and legacy.endswith("WHERE (scope_id IS NULL)")
        # Nothing else in the schema moved.
        after = await schema(url)
        changed = {name for _t, name, _d in set(after["indexes"]) ^ set(before_schema["indexes"])}
        assert changed == {old for old, _s, _c in INDEXES.values()} | {
            f"uq_{t}_{kind}_{stem}" for t, (_o, stem, _c) in INDEXES.items()
            for kind in ("scoped", "legacy")}
        assert after["columns"] == before_schema["columns"]
        assert after["constraints"] == before_schema["constraints"]


@needs_postgres
@pytest.mark.anyio
async def test_uniqueness_holds_per_scope_and_among_legacy_rows() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0022")
        await _entity(url, "postgresql", ADMIN_MEMBER)
        await _entity(url, "postgresql", COLLEAGUE_MEMBER)     # another scope: allowed
        with pytest.raises(IntegrityError):
            await _entity(url, "postgresql", ADMIN_MEMBER)     # the same scope: refused
        await _entity(url, "legacy-only")                      # unattributed
        with pytest.raises(IntegrityError):
            await _entity(url, "legacy-only")                  # the old guarantee, kept
        await _entity(url, "legacy-only", ADMIN_MEMBER)        # attributed copy: allowed


@needs_postgres
@pytest.mark.anyio
@pytest.mark.parametrize("second", [COLLEAGUE_MEMBER, None], ids=["two-scopes", "scoped-and-legacy"])
async def test_the_downgrade_refuses_a_key_shared_across_scopes(second) -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0022")
        await _entity(url, "shared", ADMIN_MEMBER)
        await _entity(url, "shared", second)
        before_schema, before_rows = await schema(url), await rows(url)
        result = alembic_attempt(url, "downgrade", "0021")
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "0022 downgrade refused" in output and "entities: 1" in output
        assert await version(url) == "0022"
        assert await schema(url) == before_schema and await rows(url) == before_rows


@needs_postgres
@pytest.mark.anyio
async def test_a_clean_downgrade_restores_0021_and_the_upgrade_repeats_exactly() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0021")
        at_0021_schema, at_0021_rows = await schema(url), await rows(url)
        alembic(url, "0022")
        at_0022_schema = await schema(url)
        await _entity(url, "only-admin", ADMIN_MEMBER)
        await execute(url, "DELETE FROM entities WHERE normalized_name = 'only-admin'")

        result = alembic_attempt(url, "downgrade", "0021")
        assert result.returncode == 0, result.stderr
        assert await version(url) == "0021"
        assert await schema(url) == at_0021_schema
        assert await rows(url) == at_0021_rows

        alembic(url, "0022")
        assert await schema(url) == at_0022_schema


@needs_postgres
@pytest.mark.anyio
async def test_the_migrated_unique_indexes_are_the_models_indexes() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    from app.database.metadata import Base

    async with legacy_at_0020() as url:
        alembic(url, "0022")
        for table, (_old, stem, _columns) in INDEXES.items():
            found = await _index_definitions(url, table)
            for kind in ("scoped", "legacy"):
                name = f"uq_{table}_{kind}_{stem}"
                [model] = [i for i in Base.metadata.tables[table].indexes if i.name == name]
                declared = str(CreateIndex(model).compile(dialect=postgresql.dialect()))
                # Same columns, uniqueness and predicate as the migration made.
                migrated = found[name]
                assert ("UNIQUE" in declared) and migrated.startswith("CREATE UNIQUE INDEX")
                predicate = "scope_id IS NOT NULL" if kind == "scoped" else "scope_id IS NULL"
                assert f"WHERE {predicate}" in declared and migrated.endswith(f"WHERE ({predicate})")
                columns = declared[declared.index("(") + 1:declared.index(")")]
                assert f"({columns})" in migrated, (name, declared, migrated)


# --- Migration 0023: execution idempotency per scope (unit 8b) -----------------


async def _execution(url: str, key: str, member=None) -> None:
    await execute(url,
        "INSERT INTO executions (id, tool_name, arguments, state, authorization_status,"
        " idempotency_key, created_at, updated_at, organisation_id, scope_id, scope_visibility)"
        " VALUES (:id, 'echo', '{}', 'proposed', 'allowed', :key, :now, :now, :org, :member,"
        " CASE WHEN CAST(:member AS uuid) IS NULL THEN NULL ELSE 'personal' END)",
        {"id": uuid.uuid4(), "key": key, "now": NOW, "org": DEFAULT_ORG, "member": member})


@needs_postgres
@pytest.mark.anyio
async def test_0023_makes_idempotency_per_scope_and_downgrades_safely() -> None:
    async with legacy_at_0020() as url:
        alembic(url, "0022")
        before_rows = await rows(url)
        alembic(url, "0023")
        assert await rows(url) == before_rows
        found = await _index_definitions(url, "executions")
        assert "uq_executions_idempotency_key" not in found
        assert found["uq_executions_scoped_idempotency_key"].endswith("WHERE (scope_id IS NOT NULL)")
        assert found["uq_executions_legacy_idempotency_key"].endswith("WHERE (scope_id IS NULL)")

        await _execution(url, "same-payload", ADMIN_MEMBER)
        await _execution(url, "same-payload", COLLEAGUE_MEMBER)
        with pytest.raises(IntegrityError):
            await _execution(url, "same-payload", ADMIN_MEMBER)

        before_schema, before = await schema(url), await rows(url)
        refused = alembic_attempt(url, "downgrade", "0022")
        assert refused.returncode != 0
        assert "0023 downgrade refused" in refused.stdout + refused.stderr
        assert await version(url) == "0023"
        assert await schema(url) == before_schema and await rows(url) == before

        await execute(url, "DELETE FROM executions WHERE idempotency_key = 'same-payload'")
        clean = alembic_attempt(url, "downgrade", "0022")
        assert clean.returncode == 0, clean.stderr
        assert "uq_executions_idempotency_key" in await _index_definitions(url, "executions")
