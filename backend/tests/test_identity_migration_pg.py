"""Stage 7A: migration 0020 on real PostgreSQL.

SQLite hides PostgreSQL-specific defects (`AGENTS.md` 4.6), so the
organisation-identity migration is exercised here on the dialect it ships
to: upgrade, downgrade to 0019 and upgrade again; the seed exactly once;
every CHECK refusing an invalid row; the two partial unique indexes and their
`postgresql_where` predicate; and the constraint names the naming convention
declares.

Runs against `TEST_POSTGRES_URL` with the same `alembic` subprocess helpers
as `tests/test_migration_compatibility.py`, and is skipped without it. The
database named `mai` is refused. Every row a test inserts is inserted in a
transaction that is rolled back, and the database is left at head.

    TEST_POSTGRES_URL=postgresql+asyncpg://USER:PASS@127.0.0.1:5432/mai_test
"""

import os
import uuid
from typing import List, Optional, Sequence, Tuple

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.identity.constants import (
    DEFAULT_ORGANISATION_ID,
    DEFAULT_ORGANISATION_NAME,
    LEGACY_ADMIN_DISPLAY_NAME,
    LEGACY_ADMIN_MEMBER_ID,
    LEGACY_ADMIN_USER_ID,
)
from tests.test_migration_compatibility import IDENTITY_TABLES, alembic, alembic_downgrade

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set"),
]

PARTIAL_INDEXES = (
    "uq_user_identities_provider_subject_attached",
    "uq_user_identities_user_id_provider_attached",
)

Statement = Tuple[str, dict]


def url() -> str:
    parsed = make_url(POSTGRES_URL)
    assert parsed.get_backend_name() == "postgresql", "this module needs PostgreSQL"
    assert parsed.database != "mai", "never the real database"
    return POSTGRES_URL


def engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(url())


async def scalar_rows(sql: str, params: Optional[dict] = None) -> List:
    eng = engine()
    try:
        async with eng.connect() as connection:
            return list((await connection.execute(text(sql), params or {})).all())
    finally:
        await eng.dispose()


async def attempt(setup: Sequence[Statement], statement: Statement) -> Optional[str]:
    """Run `setup` then `statement` in one transaction that is always rolled
    back. None if the statement was accepted, else the error's text."""
    eng = engine()
    try:
        async with eng.connect() as connection:
            transaction = await connection.begin()
            try:
                for sql, params in setup:
                    await connection.execute(text(sql), params)
                try:
                    await connection.execute(text(statement[0]), statement[1])
                except IntegrityError as exc:
                    return str(exc.orig)
                return None
            finally:
                await transaction.rollback()
    finally:
        await eng.dispose()


async def seed_counts() -> dict:
    found = {}
    for table in ("organisations", "users", "organisation_members"):
        [(count,)] = await scalar_rows(f"SELECT COUNT(*) FROM {table}")
        found[table] = count
    return found


# --- fixtures for the CHECK attempts -------------------------------------------

def a_user(user_id: uuid.UUID) -> Statement:
    return ("INSERT INTO users (id, display_name) VALUES (:id, 'Sentinel')", {"id": user_id})


def a_member(member_id, user_id, role="member", status="invited",
             activated=False, revoked=False) -> Statement:
    return (
        "INSERT INTO organisation_members (id, organisation_id, user_id, role, status,"
        " activated_at, revoked_at) VALUES (:id, :org, :user, :role, :status,"
        + (" now()," if activated else " NULL,")
        + (" now())" if revoked else " NULL)"),
        {"id": member_id, "org": DEFAULT_ORGANISATION_ID, "user": user_id,
         "role": role, "status": status},
    )


def an_identity(user_id, subject="7000000001", provider="telegram", detached=False) -> Statement:
    return (
        "INSERT INTO user_identities (id, user_id, provider, subject, detached_at)"
        " VALUES (:id, :user, :provider, :subject," + (" now())" if detached else " NULL)"),
        {"id": uuid.uuid4(), "user": user_id, "provider": provider, "subject": subject},
    )


def a_code(digest="a" * 64, expires="now() + interval '10 minutes'",
           consumed=False, invalidated=False) -> Statement:
    return (
        "INSERT INTO identity_link_codes (id, member_id, code_sha256, created_at, expires_at,"
        f" consumed_at, invalidated_at) VALUES (:id, :member, :digest, now(), {expires},"
        + (" now()," if consumed else " NULL,")
        + (" now())" if invalidated else " NULL)"),
        {"id": uuid.uuid4(), "member": LEGACY_ADMIN_MEMBER_ID, "digest": digest},
    )


# ============================================================================
# Tests
# ============================================================================


async def test_upgrade_downgrade_to_0019_and_upgrade_again_keeps_one_seed() -> None:
    alembic(url(), "head")
    alembic_downgrade(url(), "0019")
    remaining = {row[0] for row in await scalar_rows(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
    assert not remaining & set(IDENTITY_TABLES), remaining & set(IDENTITY_TABLES)
    assert "tasks" in remaining                 # the downgrade removed only 0020's tables
    alembic(url(), "head")
    assert await seed_counts() == {"organisations": 1, "users": 1, "organisation_members": 1}

    [(org_id, org_name, org_status)] = await scalar_rows(
        "SELECT id, name, status FROM organisations")
    assert (org_id, org_name, org_status) == (DEFAULT_ORGANISATION_ID, DEFAULT_ORGANISATION_NAME, "active")
    [(user_id, display_name)] = await scalar_rows("SELECT id, display_name FROM users")
    assert (user_id, display_name) == (LEGACY_ADMIN_USER_ID, LEGACY_ADMIN_DISPLAY_NAME)
    [(member_id, member_org, member_user, role, status, activated_at, revoked_at)] = await scalar_rows(
        "SELECT id, organisation_id, user_id, role, status, activated_at, revoked_at"
        " FROM organisation_members")
    assert (member_id, member_org, member_user, role, status) == (
        LEGACY_ADMIN_MEMBER_ID, DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_USER_ID, "admin", "active")
    assert activated_at is not None and revoked_at is None


async def test_every_check_refuses_an_invalid_row_and_accepts_a_valid_one() -> None:
    alembic(url(), "head")
    user = uuid.uuid4()
    member = uuid.uuid4()
    cases = [
        # (setup, valid control, invalid row, the constraint that must refuse it)
        ([], ("INSERT INTO organisations (id, name, status) VALUES (:id, 'S', 'active')",
              {"id": uuid.uuid4()}),
         ("INSERT INTO organisations (id, name, status) VALUES (:id, 'S', 'closed')",
          {"id": uuid.uuid4()}), "ck_organisations_status_valid"),
        ([a_user(user)], a_member(member, user), a_member(member, user, role="owner"),
         "ck_organisation_members_role_valid"),
        ([a_user(user)], a_member(member, user), a_member(member, user, status="paused"),
         "ck_organisation_members_status_valid"),
        ([a_user(user)], a_member(member, user, status="revoked", revoked=True),
         a_member(member, user, status="revoked"), "ck_organisation_members_revoked_iff_revoked_at"),
        ([a_user(user)], a_member(member, user, status="active", activated=True),
         a_member(member, user, status="active", activated=True, revoked=True),
         "ck_organisation_members_revoked_iff_revoked_at"),
        ([a_user(user)], a_member(member, user, status="active", activated=True),
         a_member(member, user, status="active"), "ck_organisation_members_active_has_activated_at"),
        ([a_user(user)], an_identity(user), an_identity(user, provider="email"),
         "ck_user_identities_provider_valid"),
        ([a_user(user)], an_identity(user), an_identity(user, subject=""),
         "ck_user_identities_subject_not_empty"),
        ([], a_code(), a_code(digest="a" * 63), "ck_identity_link_codes_hash_length"),
        ([], a_code(consumed=True), a_code(consumed=True, invalidated=True),
         "ck_identity_link_codes_not_consumed_and_invalidated"),
        ([], a_code(), a_code(expires="now()"), "ck_identity_link_codes_expires_after_created"),
    ]
    for setup, valid, invalid, constraint in cases:
        assert await attempt(setup, valid) is None, constraint
        refused = await attempt(setup, invalid)
        assert refused is not None and constraint in refused, (constraint, refused)
    assert await seed_counts() == {"organisations": 1, "users": 1, "organisation_members": 1}


async def test_attached_identities_are_unique_and_a_detached_one_is_not_counted() -> None:
    alembic(url(), "head")
    first, second = uuid.uuid4(), uuid.uuid4()
    users = [a_user(first), a_user(second)]

    refused = await attempt(users + [an_identity(first, "7000000010")], an_identity(second, "7000000010"))
    assert refused is not None and "uq_user_identities_provider_subject_attached" in refused, refused

    refused = await attempt(users + [an_identity(first, "7000000011")], an_identity(first, "7000000012"))
    assert refused is not None and "uq_user_identities_user_id_provider_attached" in refused, refused

    assert await attempt(
        users + [an_identity(first, "7000000013", detached=True)], an_identity(second, "7000000013")
    ) is None
    assert await attempt(
        users + [an_identity(first, "7000000014", detached=True)], an_identity(first, "7000000015")
    ) is None
    [(count,)] = await scalar_rows("SELECT COUNT(*) FROM user_identities")
    assert count == 0                           # every attempt was rolled back


async def test_the_partial_indexes_carry_the_postgresql_predicate() -> None:
    alembic(url(), "head")
    rows = dict(await scalar_rows(
        "SELECT indexname, indexdef FROM pg_indexes"
        " WHERE schemaname = current_schema() AND tablename = 'user_identities'"))
    for name in PARTIAL_INDEXES:
        assert name in rows, sorted(rows)
        assert rows[name].startswith("CREATE UNIQUE INDEX"), rows[name]
        assert rows[name].endswith("WHERE (detached_at IS NULL)"), rows[name]


async def test_constraint_names_equal_the_declared_names() -> None:
    import app.main  # noqa: F401  (registers every model)
    from app.database.metadata import Base

    alembic(url(), "head")
    declared = {
        table: {str(c.name) for c in Base.metadata.tables[table].constraints}
        for table in IDENTITY_TABLES
    }
    assert all(declared.values())
    assert not [n for names in declared.values() for n in names if n in ("None", "")]
    for table in IDENTITY_TABLES:
        found = {row[0] for row in await scalar_rows(
            "SELECT c.conname FROM pg_constraint c"
            " JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = current_schema() AND t.relname = :table"
            " AND c.contype IN ('p', 'f', 'u', 'c')",
            {"table": table})}
        assert found == declared[table], (table, found ^ declared[table])
