"""Stage 7A: link-code and revocation races, on real PostgreSQL.

SQLite has one writer and no row locks, so it cannot show these races. Each
test gets its own throwaway `mai_pgtest_*` database, created from
`TEST_POSTGRES_ADMIN_URL` (a maintenance database on a server where the user
may CREATE DATABASE), and drops it afterwards. The real `mai` database is
refused. Without the variable the module is skipped, never silently passed.

Every race is forced, not hoped for: a third connection holds the contended
row `FOR UPDATE` while both contenders start, so both have read past their
unlocked steps before either can take the lock. Every assertion is about
committed database state. Every code and Telegram id is a synthetic sentinel.

    TEST_POSTGRES_ADMIN_URL=postgresql+asyncpg://USER:PASS@127.0.0.1:5432/postgres
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update

from app.identity.constants import DEFAULT_ORGANISATION_ID, MemberStatus
from app.identity.models import LinkCode, OrganisationMember, UserIdentity
from app.identity.schemas import RedeemResult, RevokeOutcome
from app.identity.service import MembershipService
from app.organisation.revocation import MemberRevocation
from app.tasks.models import Task, TaskEvent
from app.tasks.requests import MonitorRequestService
from app.tasks.service import TaskService
from app.tasks.states import TaskState

ADMIN_URL = os.environ.get("TEST_POSTGRES_ADMIN_URL", "")

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not ADMIN_URL, reason="TEST_POSTGRES_ADMIN_URL is not set"),
]

#: How long the blocking connection holds its lock.
HOLD = 0.6
#: How many fresh members each interleaving race is repeated for.
ROUNDS = 4


@pytest_asyncio.fixture
async def pg():
    """A fresh, uniquely named database on the server; dropped afterwards."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.main  # noqa: F401  (registers every model)
    from app.database.metadata import Base

    url = make_url(ADMIN_URL)
    assert url.get_backend_name() == "postgresql", "these races need PostgreSQL"
    assert url.database != "mai", "never the real database"
    name = f"mai_pgtest_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(url.set(database=name), pool_size=10)
    try:
        async with engine.begin() as connection:
            # `tests/conftest.py` seeds the default organisation on create.
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


# --- helpers ------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc)


async def invited(F, settings):
    """A new invited member and its open code."""
    async with F() as session:
        view, issued = await MembershipService(session, settings=settings).invite(
            DEFAULT_ORGANISATION_ID, "Sentinel Member")
        await session.commit()
        return view.id, issued.code


async def give_task(F, settings, member_id) -> uuid.UUID:
    async with F() as session:
        created = await TaskService(session, settings=settings, owner_id=member_id).create_for_user(
            "Sentinel objective")
        await session.commit()
        assert created.ok, created
        return created.task_id


async def redeem(F, settings, code, subject):
    """One redemption as the webhook makes it: commit only on LINKED."""
    async with F() as session:
        try:
            result = await MembershipService(session, settings=settings).redeem_link_code(
                code, "telegram", subject)
        except Exception as exc:  # reported, never committed
            await session.rollback()
            return f"error:{type(exc).__name__}"
        if result is RedeemResult.LINKED:
            await session.commit()
        else:
            await session.rollback()
        return result


async def revoke(F, settings, member_id):
    async with F() as session:
        revocation = MemberRevocation(
            session,
            cancel_service_for=lambda mid: MonitorRequestService(session, settings=settings, owner_id=mid),
        )
        return await revocation.revoke(DEFAULT_ORGANISATION_ID, member_id)


async def while_member_is_held(F, member_id, *contenders):
    """Start every contender while another connection holds the member row,
    check that none could finish, then release it and gather them."""
    async with F() as blocker:
        await blocker.execute(
            select(OrganisationMember.id).where(OrganisationMember.id == member_id).with_for_update())
        started = [asyncio.ensure_future(c) for c in contenders]
        await asyncio.sleep(HOLD)
        assert not any(t.done() for t in started), "a contender did not wait for the member lock"
        await blocker.rollback()
    return await asyncio.gather(*started)


async def member_state(F, member_id) -> dict:
    async with F() as session:
        member = (await session.execute(
            select(OrganisationMember).where(OrganisationMember.id == member_id))).scalar_one()
        attached = (await session.execute(
            select(func.count()).select_from(UserIdentity).where(
                UserIdentity.user_id == member.user_id, UserIdentity.detached_at.is_(None)))).scalar_one()
        identities = (await session.execute(
            select(func.count()).select_from(UserIdentity).where(
                UserIdentity.user_id == member.user_id))).scalar_one()
        codes = (await session.execute(
            select(LinkCode).where(LinkCode.member_id == member_id))).scalars().all()
        return {
            "status": member.status,
            "revoked_at": member.revoked_at,
            "attached": attached,
            "identities": identities,
            "consumed": sum(1 for c in codes if c.consumed_at is not None),
            "open": sum(1 for c in codes if c.consumed_at is None and c.invalidated_at is None),
        }


async def member_revoked_events(F, task_id) -> int:
    async with F() as session:
        events = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id))).scalars().all()
        return sum(1 for e in events if (e.event_metadata or {}).get("reason") == "member_revoked")


# ============================================================================
# 1. One code, two redemptions
# ============================================================================


async def test_one_code_redeemed_twice_concurrently_links_exactly_once(pg, settings) -> None:
    F = pg
    member_id, code = await invited(F, settings)
    results = await while_member_is_held(
        F, member_id, redeem(F, settings, code, "7000000001"), redeem(F, settings, code, "7000000002"))

    assert [r for r in results if r is RedeemResult.LINKED] == [RedeemResult.LINKED], results
    state = await member_state(F, member_id)
    assert state["attached"] == 1 and state["identities"] == 1
    assert state["consumed"] == 1 and state["open"] == 0
    assert state["status"] == MemberStatus.ACTIVE.value


async def test_the_same_account_redeeming_one_code_twice_links_once(pg, settings) -> None:
    F = pg
    member_id, code = await invited(F, settings)
    results = await while_member_is_held(
        F, member_id, redeem(F, settings, code, "7000000003"), redeem(F, settings, code, "7000000003"))
    assert [r for r in results if r is RedeemResult.LINKED] == [RedeemResult.LINKED], results
    state = await member_state(F, member_id)
    assert state["attached"] == 1 and state["identities"] == 1 and state["consumed"] == 1


# ============================================================================
# 2. Redemption against revocation
# ============================================================================


async def test_redemption_racing_revocation_never_leaves_a_half_state(pg, settings) -> None:
    F = pg
    for round_number in range(ROUNDS):
        member_id, code = await invited(F, settings)
        subject = f"71000000{round_number:02d}"
        contenders = [redeem(F, settings, code, subject), revoke(F, settings, member_id)]
        if round_number % 2:
            contenders.reverse()
        results = await while_member_is_held(F, member_id, *contenders)
        redeemed = next(r for r in results if not isinstance(r, RevokeOutcome))
        revoked = next(r for r in results if isinstance(r, RevokeOutcome))

        assert revoked is RevokeOutcome.REVOKED, results
        assert redeemed in (RedeemResult.LINKED, RedeemResult.INVALID), results
        state = await member_state(F, member_id)
        # Never revoked with an attached identity.
        assert not (state["status"] == MemberStatus.REVOKED.value and state["attached"]), state
        # Never active with the code consumed by nobody.
        assert not (state["status"] == MemberStatus.ACTIVE.value and state["consumed"] == 0), state
        # Revocation always wins in the end, and no code stays open.
        assert state["status"] == MemberStatus.REVOKED.value and state["open"] == 0, state
        if redeemed is RedeemResult.LINKED:
            assert state["consumed"] == 1 and state["identities"] == 1, state
        else:
            assert state["consumed"] == 0 and state["identities"] == 0, state


# ============================================================================
# 3. Two revocations
# ============================================================================


async def test_two_concurrent_revocations_settle_on_one(pg, settings) -> None:
    F = pg
    member_id, _ = await invited(F, settings)
    task_id = await give_task(F, settings, member_id)
    results = await while_member_is_held(
        F, member_id, revoke(F, settings, member_id), revoke(F, settings, member_id))

    assert sorted(r.value for r in results) == [
        RevokeOutcome.ALREADY_REVOKED.value, RevokeOutcome.REVOKED.value]
    state = await member_state(F, member_id)
    assert state["status"] == MemberStatus.REVOKED.value and state["revoked_at"] is not None
    async with F() as session:
        rows = (await session.execute(
            select(OrganisationMember.revoked_at).where(OrganisationMember.id == member_id))).all()
    assert len(rows) == 1
    # The task was cancelled once, by whichever revocation ran first.
    assert await member_revoked_events(F, task_id) == 1


# ============================================================================
# 4. Revocation against a task completing under its lock
# ============================================================================


async def test_revocation_waits_for_a_held_task_and_accepts_its_completion(pg, settings) -> None:
    F = pg
    member_id, _ = await invited(F, settings)
    task_id = await give_task(F, settings, member_id)

    async with F() as holder:
        await holder.execute(select(Task.id).where(Task.id == task_id).with_for_update())
        revoking = asyncio.ensure_future(revoke(F, settings, member_id))
        await asyncio.sleep(HOLD)
        assert not revoking.done(), "the revocation did not wait for the task lock"
        await holder.execute(
            update(Task).where(Task.id == task_id)
            .values(state=TaskState.COMPLETED, completed_at=now()))
        await holder.commit()

    assert await revoking is RevokeOutcome.REVOKED
    async with F() as session:
        task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.state is TaskState.COMPLETED
    assert task.cancelled_at is None and task.completed_at is not None
    assert await member_revoked_events(F, task_id) == 0
    state = await member_state(F, member_id)
    assert state["status"] == MemberStatus.REVOKED.value
