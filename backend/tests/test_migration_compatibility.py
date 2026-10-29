"""Migration portability, exercised by running the real migration chain.

Migration 0003 removes duplicate memories so a unique index can be built. Its
original formulation used `MIN(id)` over a UUID primary key, which SQLite
accepts and PostgreSQL rejects:

    asyncpg.exceptions.UndefinedFunctionError: function min(uuid) does not exist

The suite ran on SQLite and never saw it, for two compounding reasons: SQLite
is dynamically typed and happily orders whatever a UUID is stored as, and no
test ever ran a migration at all -- the fixtures build the schema straight from
ORM metadata, which is the *post*-migration shape.

These tests close both gaps. They run `alembic upgrade` against a real
database, insert the duplicates the old race produced, and assert what the
migration does to them. Running against PostgreSQL as well as SQLite needs only
a URL: see `test_the_chain_runs_on_postgresql`.
"""

import importlib.util
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS = BACKEND / "alembic" / "versions"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def deduplication_sql() -> str:
    """The statement the migration actually runs, loaded from the migration."""
    path = MIGRATIONS / "0003_memory_unique_constraint.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._DEDUPLICATE_MEMORIES


def alembic(url: str, target: str) -> None:
    """Run `alembic upgrade` in a subprocess against `url`.

    A subprocess rather than the API because Alembic configures logging and
    engine state globally, and doing that inside the test process disturbs the
    fixtures every other test depends on.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND,
        env={
            "PATH": "/usr/bin:/bin",
            "DATABASE_URL": url,
            "GROQ_API_KEY": "test-key",
            "LOG_LEVEL": "WARNING",
        },
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"alembic upgrade {target} failed:\n{result.stdout}\n{result.stderr}"
        )


def insert_duplicate(connection, conversation_id, content, normalized, day, kind):
    """Insert a memory with raw SQL, as the old race did.

    Deliberately not the ORM: the ORM's schema carries the unique index the
    migration is clearing the way for, so it cannot produce the rows under
    test.
    """
    connection.execute(
        text(
            """
            INSERT INTO memories (
                id, content, normalized_content, memory_type, status,
                importance_score, confidence_score, source_conversation_id,
                created_at, updated_at
            ) VALUES (
                :id, :content, :normalized, :kind, 'active',
                8, 0.9, :conversation, :created, :created
            )
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "content": content,
            "normalized": normalized,
            "kind": kind,
            "conversation": str(conversation_id),
            "created": BASE + timedelta(days=day),
        },
    )


@pytest.fixture
def migrated_to_0002(tmp_path):
    """A real database migrated to 0002 -- before the unique index exists."""
    database = tmp_path / "migration.db"
    url = f"sqlite+aiosqlite:///{database}"
    alembic(url, "0002")

    engine = create_engine(f"sqlite:///{database}")
    conversation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES (:id, 'seed', :now, :now)"
            ),
            {"id": str(conversation_id), "now": BASE},
        )
    engine.dispose()
    return url, database, conversation_id


def rows(database) -> list:
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        found = connection.execute(
            text(
                "SELECT content, normalized_content, memory_type "
                "FROM memories ORDER BY created_at"
            )
        ).all()
    engine.dispose()
    return found


# --- The migration chain runs, and cleans up correctly ----------------------


def test_the_chain_runs_from_empty_to_head(tmp_path) -> None:
    """The plain case: no data, every migration in order."""
    alembic(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}", "head")


def test_one_row_survives_each_duplicate_group(migrated_to_0002) -> None:
    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for label, day in (("A", 0), ("B", 1), ("C", 2)):
            insert_duplicate(connection, conversation, label, "same text", day, "semantic")
    engine.dispose()

    alembic(url, "0003")

    assert len(rows(database)) == 1


def test_the_oldest_row_is_the_one_kept(migrated_to_0002) -> None:
    """What the migration's comment always claimed, and `MIN(id)` never did.

    Memory ids are random UUIDv4, so a minimum over them picks an arbitrary
    member of the group. Ordering by `created_at` picks the earliest.
    """
    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        # Inserted newest-first, so insertion order cannot be what decides it.
        insert_duplicate(connection, conversation, "newest", "same text", 10, "semantic")
        insert_duplicate(connection, conversation, "middle", "same text", 5, "semantic")
        insert_duplicate(connection, conversation, "oldest", "same text", 0, "semantic")
    engine.dispose()

    alembic(url, "0003")

    survivors = rows(database)
    assert len(survivors) == 1
    assert survivors[0].content == "oldest"


def test_groups_are_keyed_by_type_and_content_together(migrated_to_0002) -> None:
    """Same text under a different type is a different memory, not a duplicate."""
    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        insert_duplicate(connection, conversation, "A", "same text", 0, "semantic")
        insert_duplicate(connection, conversation, "B", "same text", 1, "preference")
        insert_duplicate(connection, conversation, "C", "other text", 2, "semantic")
    engine.dispose()

    alembic(url, "0003")

    assert sorted(row.content for row in rows(database)) == ["A", "B", "C"]


def test_distinct_memories_are_untouched(migrated_to_0002) -> None:
    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for index in range(5):
            insert_duplicate(
                connection, conversation, f"m{index}", f"text {index}", index, "semantic"
            )
    engine.dispose()

    alembic(url, "0003")

    assert len(rows(database)) == 5


def test_ties_on_timestamp_still_leave_exactly_one(migrated_to_0002) -> None:
    """The `id` tiebreak: same instant, three rows, one deterministic survivor."""
    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for label in ("A", "B", "C"):
            insert_duplicate(connection, conversation, label, "identical", 0, "semantic")
    engine.dispose()

    alembic(url, "0003")

    assert len(rows(database)) == 1


def test_the_unique_index_holds_afterwards(migrated_to_0002) -> None:
    """The point of the step: the index must be creatable, and must then bite."""
    from sqlalchemy.exc import IntegrityError

    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        insert_duplicate(connection, conversation, "A", "same", 0, "semantic")
        insert_duplicate(connection, conversation, "B", "same", 1, "semantic")
    engine.dispose()

    alembic(url, "0003")

    engine = create_engine(f"sqlite:///{database}")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            insert_duplicate(connection, conversation, "dupe", "same", 9, "semantic")
    engine.dispose()


def test_the_whole_chain_runs_over_duplicated_data(migrated_to_0002) -> None:
    """0003 is mid-chain; the migrations after it must survive the cleanup too."""
    url, database, conversation = migrated_to_0002
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for label, day in (("A", 0), ("B", 1)):
            insert_duplicate(connection, conversation, label, "same", day, "semantic")
    engine.dispose()

    alembic(url, "head")

    assert len(rows(database)) == 1


# --- The statement itself is UUID-safe --------------------------------------


def test_the_deduplication_step_aggregates_no_uuid() -> None:
    """The specific fault. PostgreSQL can *order* UUIDs but not aggregate them."""
    sql = deduplication_sql().lower()

    for aggregate in ("min(id)", "max(id)", "min(m.id)", "max(m.id)"):
        assert aggregate not in sql, f"the statement aggregates a UUID: {aggregate}"
    assert "row_number()" in sql, "expected a window function"


def test_the_deduplication_step_does_not_cast_uuids_to_text() -> None:
    """A cast would work, and would also be a silent index-defeating trap."""
    sql = deduplication_sql().lower()
    for cast in ("::text", "cast(id as", "id::varchar"):
        assert cast not in sql, f"the statement casts a UUID: {cast}"


def test_the_deduplication_step_orders_deterministically() -> None:
    sql = re.sub(r"\s+", " ", deduplication_sql().lower())
    assert "order by created_at asc, id asc" in sql


# --- The fault class, swept across every migration --------------------------


def test_no_migration_aggregates_a_uuid_column() -> None:
    """`min()`/`max()` over an id fails on PostgreSQL and passes on SQLite.

    Worth banning across the board rather than fixing one occurrence.
    """
    offenders = []
    pattern = re.compile(r"\b(?:min|max)\s*\(\s*[a-z_]*\.?id\s*\)", re.IGNORECASE)

    for path in sorted(MIGRATIONS.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}")

    assert offenders == [], f"UUID aggregation in migrations: {offenders}"


def test_no_migration_uses_a_sqlite_only_function() -> None:
    """Functions PostgreSQL does not provide, in executable migration SQL.

    Matched on a word boundary so `sa.DateTime(` is not mistaken for SQLite's
    `datetime(`.
    """
    sqlite_only = (
        "julianday", "strftime", "ifnull", "group_concat",
        "sqlite_version", "last_insert_rowid", "randomblob",
    )
    offenders = []

    for path in sorted(MIGRATIONS.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            for function in sqlite_only:
                if re.search(rf"(?<![\w.]){function}\s*\(", line, re.IGNORECASE):
                    offenders.append(f"{path.name}:{number}:{function}")

    assert offenders == [], f"SQLite-only functions in migrations: {offenders}"


def test_the_test_database_supports_window_functions() -> None:
    """If it did not, the behavioural tests above would pass for a wrong reason."""
    import sqlite3

    major, minor = (int(part) for part in sqlite3.sqlite_version.split(".")[:2])
    assert (major, minor) >= (3, 25), (
        f"SQLite {sqlite3.sqlite_version} predates window functions"
    )


# --- PostgreSQL ------------------------------------------------------------


def postgres_url() -> str:
    """A PostgreSQL URL to test against, if one is reachable.

    Set `TEST_POSTGRES_URL` to run the chain against a real server. Without it
    this is skipped rather than silently passing -- the whole point of this
    module is that a green SQLite suite proved nothing about PostgreSQL.
    """
    import os

    return os.environ.get("TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not postgres_url(), reason="TEST_POSTGRES_URL is not set")
def test_the_chain_runs_on_postgresql() -> None:
    """The test that would have caught this, on the dialect that showed it."""
    alembic(postgres_url(), "head")


# --- Stage 7A: the organisation-identity migration --------------------------
# The suite's databases are built by `create_all` plus a test-only seed
# (`tests/conftest.py`). These tests keep that honest: the migration's seed is
# the same, it is readable through the application's own query, and the
# migrated schema -- including the two partial unique indexes and their
# predicates -- is the schema the models declare.

IDENTITY_TABLES = (
    "organisations", "users", "organisation_members", "user_identities", "identity_link_codes",
)


def alembic_downgrade(url: str, target: str) -> None:
    """`alembic downgrade`, in a subprocess, like `alembic` above."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", target],
        cwd=BACKEND,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": url, "GROQ_API_KEY": "test-key",
             "LOG_LEVEL": "WARNING"},
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"alembic downgrade {target} failed:\n{result.stdout}\n{result.stderr}")


def _migration_0020():
    path = MIGRATIONS / "0020_organisation_identity.py"
    spec = importlib.util.spec_from_file_location("migration_0020", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_0020_seed_literals_equal_the_application_constants() -> None:
    from app.core.owner import CONFIGURED_OWNER_ID
    from app.identity import constants

    migration = _migration_0020()
    assert migration.DEFAULT_ORGANISATION_ID == constants.DEFAULT_ORGANISATION_ID
    assert migration.DEFAULT_ORGANISATION_NAME == constants.DEFAULT_ORGANISATION_NAME
    assert migration.LEGACY_ADMIN_USER_ID == constants.LEGACY_ADMIN_USER_ID
    assert migration.LEGACY_ADMIN_DISPLAY_NAME == constants.LEGACY_ADMIN_DISPLAY_NAME
    assert migration.LEGACY_ADMIN_MEMBER_ID == constants.LEGACY_ADMIN_MEMBER_ID == CONFIGURED_OWNER_ID


def _seed_rows(database) -> dict:
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        found = {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in ("organisations", "users", "organisation_members")
        }
    engine.dispose()
    return found


async def test_the_migrated_seed_is_the_active_admin_read_by_the_application(tmp_path) -> None:
    """Through the application's own query and the real SQLite `Uuid` storage
    path: a seed written in another form would not match, and every request
    on a migrated database would be refused."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.owner import CONFIGURED_OWNER_ID
    from app.identity.constants import DEFAULT_ORGANISATION_ID, Role
    from app.identity.principal import load_active_principal

    database = tmp_path / "seed.db"
    alembic(f"sqlite+aiosqlite:///{database}", "head")
    assert _seed_rows(database) == {"organisations": 1, "users": 1, "organisation_members": 1}

    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with engine.connect() as connection:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(bind=connection) as session:
                principal = await load_active_principal(session, CONFIGURED_OWNER_ID)
    finally:
        await engine.dispose()
    assert principal is not None
    assert principal.member_id == CONFIGURED_OWNER_ID
    assert principal.organisation_id == DEFAULT_ORGANISATION_ID
    assert principal.role is Role.ADMIN


def test_0020_downgrades_and_upgrades_again_with_one_seed(tmp_path) -> None:
    database = tmp_path / "cycle.db"
    url = f"sqlite+aiosqlite:///{database}"
    alembic(url, "head")
    alembic_downgrade(url, "0019")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        remaining = {
            row[0] for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }
    engine.dispose()
    assert not remaining & set(IDENTITY_TABLES)
    alembic(url, "head")
    assert _seed_rows(database) == {"organisations": 1, "users": 1, "organisation_members": 1}


def _schema(database) -> dict:
    """Every schema object of the identity tables, as SQLite records it."""
    from sqlalchemy import inspect

    engine = create_engine(f"sqlite:///{database}")
    found = {}
    with engine.connect() as connection:
        inspector = inspect(connection)
        for table in IDENTITY_TABLES:
            found[table] = {
                "columns": sorted(
                    (c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)
                ),
                "pk": inspector.get_pk_constraint(table)["name"],
                "checks": sorted(
                    (c["name"], " ".join(c["sqltext"].split()))
                    for c in inspector.get_check_constraints(table)
                ),
                "uniques": sorted(
                    (u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
                ),
                "fks": sorted(
                    (f["name"], tuple(f["constrained_columns"]), f["referred_table"],
                     f.get("options", {}).get("ondelete"))
                    for f in inspector.get_foreign_keys(table)
                ),
            }
        # Indexes as SQLite stores their DDL: this is where a partial index's
        # WHERE predicate lives, and what an inspector of constraints misses.
        rows = connection.execute(text(
            "SELECT tbl_name, name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL"
        )).all()
    engine.dispose()
    for table, name, sql in rows:
        if table in found:
            found[table].setdefault("indexes", []).append((name, " ".join(sql.split())))
    for table in found:
        found[table]["indexes"] = sorted(found[table].get("indexes", []))
    return found


def test_the_migrated_identity_schema_is_the_models_schema(tmp_path) -> None:
    from app.database.metadata import Base

    migrated = tmp_path / "migrated.db"
    alembic(f"sqlite+aiosqlite:///{migrated}", "head")

    declared = tmp_path / "declared.db"
    engine = create_engine(f"sqlite:///{declared}")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in IDENTITY_TABLES])
    engine.dispose()

    expected, actual = _schema(declared), _schema(migrated)
    assert actual == expected
    # The comparison is not vacuous: both partial unique indexes are there,
    # with their predicate.
    indexes = dict(actual["user_identities"]["indexes"])
    for name in ("uq_user_identities_provider_subject_attached",
                 "uq_user_identities_user_id_provider_attached"):
        assert indexes[name].startswith("CREATE UNIQUE INDEX")
        assert indexes[name].endswith("WHERE detached_at IS NULL")


# --- Stage 7B L0: knowledge-scope columns (0021) ----------------------------
# The upgrade backfills only `organisation_id` (to the one default
# organisation), never a scope; the downgrade refuses whenever dropping the
# columns would lose ownership. Rows are inserted at 0020 with raw SQL, as
# the old releases wrote them.

SCOPED_TABLES = (
    "conversations", "memories", "entities", "entity_aliases", "relationships",
    "knowledge_conflicts", "reminders", "executions", "workflows", "imported_archives",
)
DEFAULT_ORG_HEX = uuid.UUID("00000000-0000-0000-0000-00000000b1b1").hex
ADMIN_MEMBER_HEX = uuid.UUID("00000000-0000-0000-0000-00000000a1a1").hex


def alembic_attempt(url: str, *command: str) -> subprocess.CompletedProcess:
    """`alembic <command>`, returning the result instead of raising."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command],
        cwd=BACKEND,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": url, "GROQ_API_KEY": "test-key",
             "LOG_LEVEL": "WARNING"},
        capture_output=True, text=True,
    )


def _columns(database, table: str) -> set:
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        found = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
    engine.dispose()
    return found


def _scalar(database, sql: str, params=None):
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        value = connection.execute(text(sql), params or {}).scalar()
    engine.dispose()
    return value


def _execute(database, sql: str, params=None) -> None:
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})
    engine.dispose()


@pytest.fixture
def legacy_at_0020(tmp_path):
    """A database at 0020 holding legacy rows: a conversation, a memory and an entity."""
    database = tmp_path / "legacy.db"
    url = f"sqlite+aiosqlite:///{database}"
    alembic(url, "0020")
    conversation = uuid.uuid4().hex
    _execute(database, "INSERT INTO conversations (id, title, created_at, updated_at) "
                       "VALUES (:id, 'legacy', :now, :now)", {"id": conversation, "now": BASE})
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        insert_duplicate(connection, uuid.UUID(conversation), "legacy fact", "legacy fact", 0, "semantic")
    engine.dispose()
    _execute(database, "INSERT INTO entities (id, canonical_name, normalized_name, entity_type, "
                       "status, created_at, updated_at) VALUES (:id, 'Legacy', 'legacy', "
                       "'person', 'active', :now, :now)", {"id": uuid.uuid4().hex, "now": BASE})
    return url, database


def _row_counts(database) -> dict:
    return {t: _scalar(database, f"SELECT COUNT(*) FROM {t}") for t in SCOPED_TABLES}


def test_0021_backfills_only_the_organisation_and_preserves_every_row(legacy_at_0020) -> None:
    url, database = legacy_at_0020
    before = _row_counts(database)
    # Not vacuous: legacy rows exist (the chain itself also seeds an entity).
    assert before["conversations"] >= 1 and before["memories"] >= 1 and before["entities"] >= 1
    alembic(url, "head")
    assert _row_counts(database) == before
    for table in SCOPED_TABLES:
        assert {"organisation_id", "scope_id", "scope_visibility"} <= _columns(database, table)
        assert _scalar(database, f"SELECT COUNT(*) FROM {table} WHERE organisation_id IS NULL "
                                 f"OR organisation_id <> :org", {"org": DEFAULT_ORG_HEX}) == 0, table
        # Never a guessed scope: legacy rows stay unattributed.
        assert _scalar(database, f"SELECT COUNT(*) FROM {table} WHERE scope_id IS NOT NULL "
                                 f"OR scope_visibility IS NOT NULL") == 0, table


def test_0021_downgrades_cleanly_when_only_the_backfill_is_present(legacy_at_0020) -> None:
    url, database = legacy_at_0020
    before = _row_counts(database)
    alembic(url, "head")
    result = alembic_attempt(url, "downgrade", "0020")
    assert result.returncode == 0, result.stderr
    assert _row_counts(database) == before
    for table in SCOPED_TABLES:
        assert not {"organisation_id", "scope_id", "scope_visibility"} & _columns(database, table)
    alembic(url, "head")     # and the upgrade recreates the backfill exactly
    assert _scalar(database, "SELECT COUNT(*) FROM conversations WHERE organisation_id = :org",
                   {"org": DEFAULT_ORG_HEX}) == 1


@pytest.mark.parametrize("ownership_sql", [
    # a row with a scope (personal, the Admin's)
    "UPDATE conversations SET scope_id = :member, scope_visibility = 'personal'",
    # a row attributed to an organisation other than the default
    "UPDATE memories SET organisation_id = :other",
])
def test_0021_downgrade_refuses_when_it_would_lose_ownership(legacy_at_0020, ownership_sql) -> None:
    url, database = legacy_at_0020
    alembic(url, "0021")
    _execute(database, ownership_sql, {"member": ADMIN_MEMBER_HEX, "other": uuid.uuid4().hex})
    before = _row_counts(database)
    result = alembic_attempt(url, "downgrade", "0020")
    assert result.returncode != 0
    assert "0021 downgrade refused" in result.stdout + result.stderr
    assert _scalar(database, "SELECT version_num FROM alembic_version") == "0021"
    assert _row_counts(database) == before
    for table in SCOPED_TABLES:      # nothing was dropped
        assert {"organisation_id", "scope_id", "scope_visibility"} <= _columns(database, table)


#: Offline (`--sql`) generation never connects, so any well-formed URL will do.
OFFLINE_URL = "postgresql+asyncpg://offline:offline@127.0.0.1:1/mai_offline_test"


@pytest.mark.parametrize("command", [
    ("upgrade", "0020:0021", "--sql"),
    ("downgrade", "0021:0020", "--sql"),
])
def test_0021_refuses_offline_sql_generation(command) -> None:
    """Review L2. Offline mode cannot read the database, so 0021 cannot check
    its own preconditions (the default organisation exists; a downgrade loses
    no ownership). It must refuse with its own message -- not crash on
    `bind.execute()` returning None, and not emit a script without the checks."""
    result = alembic_attempt(OFFLINE_URL, *command)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "0021 refused: offline" in output
    assert "AttributeError" not in output
    assert "ALTER TABLE" not in result.stdout


def test_offline_sql_generation_still_works_before_0021() -> None:
    """The refusal is 0021's alone: earlier migrations still render offline."""
    result = alembic_attempt(OFFLINE_URL, "upgrade", "0019:0020", "--sql")
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE organisations" in result.stdout


def test_0021_refuses_without_the_default_organisation(legacy_at_0020) -> None:
    url, database = legacy_at_0020
    for sql in ("DELETE FROM organisation_members", "DELETE FROM users", "DELETE FROM organisations"):
        _execute(database, sql)
    result = alembic_attempt(url, "upgrade", "head")
    assert result.returncode != 0
    assert "0021 refused" in result.stdout + result.stderr
    assert _scalar(database, "SELECT version_num FROM alembic_version") == "0020"
    assert "scope_id" not in _columns(database, "conversations")


def _scope_schema(database) -> dict:
    """The scope columns, CHECK and index of each scoped table, as SQLite records them."""
    from sqlalchemy import inspect

    engine = create_engine(f"sqlite:///{database}")
    found = {}
    with engine.connect() as connection:
        inspector = inspect(connection)
        for table in SCOPED_TABLES:
            found[table] = {
                "columns": sorted(
                    (c["name"], str(c["type"]), c["nullable"], c.get("default"))
                    for c in inspector.get_columns(table)
                    if c["name"] in ("organisation_id", "scope_id", "scope_visibility")
                ),
                "checks": sorted(
                    (c["name"], " ".join(c["sqltext"].split()))
                    for c in inspector.get_check_constraints(table)
                    if c["name"] and c["name"].endswith("knowledge_scope_valid")
                ),
                "index": " ".join(connection.execute(text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"),
                    {"n": f"ix_{table}_scope_id"}).scalar_one().split()),
            }
    engine.dispose()
    return found


def test_the_migrated_scope_schema_is_the_models_scope_schema(tmp_path) -> None:
    from app.database.metadata import Base

    migrated = tmp_path / "migrated.db"
    alembic(f"sqlite+aiosqlite:///{migrated}", "head")
    declared = tmp_path / "declared.db"
    engine = create_engine(f"sqlite:///{declared}")
    Base.metadata.create_all(engine)
    engine.dispose()

    expected, actual = _scope_schema(declared), _scope_schema(migrated)
    assert actual == expected
    for table in SCOPED_TABLES:      # not vacuous
        assert len(actual[table]["columns"]) == 3 and len(actual[table]["checks"]) == 1, table


# --- Stage 7B units 5-6: uniqueness per scope (0022) ---------------------------

SCOPED_INDEXES_0022 = {
    "memories": "type_normalized_content",
    "entities": "normalized_name",
    "entity_aliases": "normalized_alias",
}
OLD_INDEXES_0022 = {
    "memories": "uq_memories_type_normalized_content",
    "entities": "uq_entities_normalized_name",
    "entity_aliases": "uq_entity_aliases_normalized_alias",
}
COLLEAGUE_MEMBER_HEX = uuid.UUID("00000000-0000-0000-0000-0000000c0111").hex


def _index_names(database, table: str) -> set:
    return {row[1] for row in _rows(database, f"PRAGMA index_list({table})")}


def _rows(database, sql: str, params=None) -> list:
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        found = list(connection.execute(text(sql), params or {}))
    engine.dispose()
    return found


def _insert_entity(database, normalized: str, member_hex=None) -> None:
    scope = (", :org, :member, 'personal'" if member_hex else ", :org, NULL, NULL")
    _execute(database, "INSERT INTO entities (id, canonical_name, normalized_name, entity_type, "
                       "status, created_at, updated_at, organisation_id, scope_id, scope_visibility) "
                       "VALUES (:id, :name, :name, 'technology', 'active', :now, :now" + scope + ")",
             {"id": uuid.uuid4().hex, "name": normalized, "now": BASE,
              "org": DEFAULT_ORG_HEX, "member": member_hex})


def test_0022_replaces_each_global_index_with_two_partial_ones(legacy_at_0020) -> None:
    url, database = legacy_at_0020
    before = _row_counts(database)
    alembic(url, "head")
    assert _row_counts(database) == before                 # no data change
    for table, stem in SCOPED_INDEXES_0022.items():
        names = _index_names(database, table)
        assert {f"uq_{table}_scoped_{stem}", f"uq_{table}_legacy_{stem}"} <= names, table
        assert OLD_INDEXES_0022[table] not in names, table


def test_0022_lets_two_scopes_hold_the_same_name_but_not_one_scope_twice(legacy_at_0020) -> None:
    from sqlalchemy.exc import IntegrityError

    url, database = legacy_at_0020
    alembic(url, "head")
    _insert_entity(database, "postgresql", ADMIN_MEMBER_HEX)
    _insert_entity(database, "postgresql", COLLEAGUE_MEMBER_HEX)   # another member: allowed
    with pytest.raises(IntegrityError):
        _insert_entity(database, "postgresql", ADMIN_MEMBER_HEX)   # same scope: refused
    # The legacy guarantee is kept among unattributed rows: 'legacy' exists.
    with pytest.raises(IntegrityError):
        _insert_entity(database, "legacy")
    _insert_entity(database, "legacy", ADMIN_MEMBER_HEX)           # attributed copy: allowed


def test_0022_downgrades_cleanly_when_no_key_is_shared(legacy_at_0020) -> None:
    url, database = legacy_at_0020
    alembic(url, "head")
    _insert_entity(database, "only-admin", ADMIN_MEMBER_HEX)
    result = alembic_attempt(url, "downgrade", "0021")
    assert result.returncode == 0, result.stderr
    for table, stem in SCOPED_INDEXES_0022.items():
        names = _index_names(database, table)
        assert OLD_INDEXES_0022[table] in names
        assert not {f"uq_{table}_scoped_{stem}", f"uq_{table}_legacy_{stem}"} & names


@pytest.mark.parametrize("second_member", [COLLEAGUE_MEMBER_HEX, None])
def test_0022_downgrade_refuses_when_a_key_is_shared_across_scopes(legacy_at_0020, second_member) -> None:
    """Two members (or a member and a legacy row) holding the same name cannot
    go back under one global index; the downgrade refuses and changes nothing."""
    url, database = legacy_at_0020
    alembic(url, "head")
    _insert_entity(database, "shared", ADMIN_MEMBER_HEX)
    _insert_entity(database, "shared", second_member)
    before = _row_counts(database)
    result = alembic_attempt(url, "downgrade", "0021")
    assert result.returncode != 0
    assert "0022 downgrade refused" in result.stdout + result.stderr
    assert "entities: 1" in result.stdout + result.stderr
    assert _scalar(database, "SELECT version_num FROM alembic_version") == "0022"
    assert _row_counts(database) == before
    assert "uq_entities_scoped_normalized_name" in _index_names(database, "entities")


def test_0022_refuses_an_offline_downgrade() -> None:
    result = alembic_attempt(OFFLINE_URL, "downgrade", "0022:0021", "--sql")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "0022 refused: offline" in output
    assert "CREATE UNIQUE INDEX" not in result.stdout


def test_0022_renders_its_upgrade_offline() -> None:
    result = alembic_attempt(OFFLINE_URL, "upgrade", "0021:0022", "--sql")
    assert result.returncode == 0, result.stderr
    assert "uq_entities_scoped_normalized_name" in result.stdout
    assert "WHERE scope_id IS NOT NULL" in result.stdout
