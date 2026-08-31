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
