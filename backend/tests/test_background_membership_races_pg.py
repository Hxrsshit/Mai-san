"""Stage 7B unit 7: background knowledge steps racing a revocation, on PostgreSQL.

The background gate (`lock_active_principal`) reads the member FOR SHARE;
revocation (`MembershipService.mark_revoked`) takes the same row FOR UPDATE.
SQLite has no row locks, so only PostgreSQL can show that the two serialize:

- a revocation started while a step holds the share lock waits until the
  step commits (the step's writes are work the member was entitled to);
- a step started while a revocation holds the row waits, then sees the
  revocation and does nothing.

Every race is forced, never hoped for, and every assertion is about committed
state. Same gate and throwaway databases as `tests/test_identity_races_pg.py`
(skipped without `TEST_POSTGRES_ADMIN_URL`; the real `mai` database is
refused). Rows are synthetic.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.identity.constants import MemberStatus
from app.identity.models import OrganisationMember
from app.identity.principal import lock_active_principal
from app.identity.schemas import RevokeOutcome
from app.identity.scope import KnowledgeScope
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.services.conversation_service import ConversationService
from tests.test_identity_races_pg import (  # noqa: F401  (the `pg` fixture)
    HOLD,
    invited,
    pg,
    pytestmark,
    redeem,
    revoke,
)

SUBJECT = "700000000001"


async def active_member(F, settings):
    member_id, code = await invited(F, settings)
    assert str(await redeem(F, settings, code, SUBJECT)).endswith("LINKED")
    async with F() as session:
        principal = await lock_active_principal(session, member_id)
        await session.rollback()
    assert principal is not None
    return member_id, KnowledgeScope.personal(principal)


async def memories(F, scope) -> int:
    async with F() as session:
        return (await session.execute(select(func.count()).select_from(Memory)
                                      .where(scope.clause(Memory)))).scalar_one()


async def status(F, member_id) -> str:
    async with F() as session:
        return (await session.execute(select(OrganisationMember.status)
                                      .where(OrganisationMember.id == member_id))).scalar_one()


async def test_a_revocation_waits_for_an_in_flight_step_and_then_wins(pg, settings) -> None:
    member_id, scope = await active_member(pg, settings)
    async with pg() as setup:
        conversation = await ConversationService(setup, scope).create_conversation()
        await setup.commit()

    async with pg() as step:
        assert await lock_active_principal(step, member_id) is not None    # the gate
        revocation = asyncio.ensure_future(revoke(pg, settings, member_id))
        await asyncio.sleep(HOLD)
        assert not revocation.done(), "the revocation did not wait for the step"
        step.add(Memory(content="User prefers jasmine tea.", normalized_content="user prefers jasmine tea",
                        memory_type=MemoryType.PREFERENCE, status=MemoryStatus.ACTIVE,
                        importance_score=8, confidence_score=0.9,
                        source_conversation_id=conversation.id, **scope.write_columns()))
        await step.commit()

    outcome = await revocation
    assert outcome is RevokeOutcome.REVOKED, outcome
    assert await status(pg, member_id) == MemberStatus.REVOKED.value
    assert await memories(pg, scope) == 1      # written while still entitled
    async with pg() as later:                  # and nothing after
        assert await lock_active_principal(later, member_id) is None


async def test_a_step_that_starts_during_a_revocation_waits_and_does_nothing(pg, settings) -> None:
    member_id, _scope = await active_member(pg, settings)

    async def step():
        async with pg() as session:
            principal = await lock_active_principal(session, member_id)
            await session.rollback()
            return principal

    async with pg() as revoking:               # a revocation holding the row
        await revoking.execute(select(OrganisationMember.id)
                               .where(OrganisationMember.id == member_id).with_for_update())
        contender = asyncio.ensure_future(step())
        await asyncio.sleep(HOLD)
        assert not contender.done(), "the step did not wait for the revocation"
        await revoking.execute(update(OrganisationMember).where(OrganisationMember.id == member_id)
                               .values(status=MemberStatus.REVOKED.value,
                                       revoked_at=datetime.now(timezone.utc)))
        await revoking.commit()

    assert await contender is None


async def test_the_runner_takes_the_member_before_the_task_so_revocation_cannot_deadlock(
    pg, settings
) -> None:
    """Review M1 (unit 8b). Revocation locks the member row, then the
    member's tasks. The runner now binds the owner first: while a revocation
    holds the member row, the runner waits *without* having locked the task
    row (a third connection can still lock it at once), and once the
    revocation commits it writes nothing."""
    from sqlalchemy import text as sql_text

    from app.tasks.models import Task
    from app.tasks.runner import TaskRunner
    from app.tasks.service import TaskService

    member_id, _scope = await active_member(pg, settings)
    async with pg() as setup:
        created = await TaskService(setup, settings=settings, owner_id=member_id).create_for_user(
            "Sentinel objective")
        await setup.commit()
    task_id = created.task_id

    async def advance():
        async with pg() as session:
            result = await TaskRunner(TaskService(session, settings=settings, owner_id=member_id)
                                      ).advance(task_id)
            await session.commit()
            return result

    async with pg() as revoking:
        await revoking.execute(select(OrganisationMember.id)
                               .where(OrganisationMember.id == member_id).with_for_update())
        runner = asyncio.ensure_future(advance())
        await asyncio.sleep(HOLD)
        assert not runner.done(), "the runner did not wait for the member lock"
        async with pg() as probe:                      # the task row is free
            await probe.execute(sql_text("SET LOCAL lock_timeout = '200ms'"))
            await probe.execute(select(Task.id).where(Task.id == task_id).with_for_update())
            await probe.rollback()
        await revoking.execute(update(OrganisationMember).where(OrganisationMember.id == member_id)
                               .values(status=MemberStatus.REVOKED.value,
                                       revoked_at=datetime.now(timezone.utc)))
        await revoking.commit()

    result = await runner
    assert result.reason == "owner_not_active", result
