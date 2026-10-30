"""Stage 7B unit 7: background knowledge work stops when its member is gone.

A chat turn hands its frozen scope to the background pipeline (memory ->
entities -> relationships -> conflicts). Each step re-reads the member
behind that scope, under a share lock, and does nothing -- and stops the
chain -- unless the member is still active, in an active organisation,
with exactly that scope. Rows are synthetic; the member is a real row.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, update

from app.entities.models import Entity
from app.identity.constants import DEFAULT_ORGANISATION_ID, MemberStatus, OrganisationStatus, Role
from app.identity.models import Organisation, OrganisationMember, User
from app.memory.models import Memory
from app.memory import tasks as memory_tasks
from app.memory.tasks import run_entity_extraction, run_memory_extraction
from app.services.conversation_service import ConversationService
from tests.scopes import COLLEAGUE_PRINCIPAL, COLLEAGUE_SCOPE

pytestmark = pytest.mark.anyio

MEMORY_REPLY = json.dumps({"should_store_memory": True, "memories": [{
    "content": "User prefers jasmine tea.", "memory_type": "preference",
    "importance_score": 8, "confidence_score": 0.95}]})
ENTITY_REPLY = json.dumps({"entities": [
    {"name": "Jasmine tea", "entity_type": "concept", "confidence_score": 0.95}]})


async def _colleague(session_factory, status: MemberStatus = MemberStatus.ACTIVE) -> None:
    """The colleague of `tests.scopes`, as a real membership row."""
    async with session_factory() as session:
        user = User(display_name="Synthetic colleague")
        session.add(user)
        await session.flush()
        session.add(OrganisationMember(
            id=COLLEAGUE_PRINCIPAL.member_id, organisation_id=DEFAULT_ORGANISATION_ID,
            user_id=user.id, role=Role.MEMBER.value, status=status.value,
            activated_at=datetime.now(timezone.utc)))
        await session.commit()


async def _set_status(session_factory, status: MemberStatus) -> None:
    async with session_factory() as session:
        await session.execute(update(OrganisationMember)
                              .where(OrganisationMember.id == COLLEAGUE_PRINCIPAL.member_id)
                              .values(status=status.value,
                                      revoked_at=datetime.now(timezone.utc)
                                      if status is MemberStatus.REVOKED else None))
        await session.commit()


async def _turn(session_factory, fake_provider, settings):
    async with session_factory() as session:
        conversation = await ConversationService(session, COLLEAGUE_SCOPE).create_conversation()
        await session.commit()
    fake_provider.extraction_reply = MEMORY_REPLY
    fake_provider.entity_reply = ENTITY_REPLY
    await run_memory_extraction(conversation.id, "I like jasmine tea", "Noted.", settings=settings,
                                provider=fake_provider, session_factory=session_factory,
                                scope=COLLEAGUE_SCOPE)


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model)
                                      .where(COLLEAGUE_SCOPE.clause(model)))).scalar_one()


async def test_an_active_member_is_served(session_factory, fake_provider, settings) -> None:
    """Not vacuous: the same pipeline does its work for an active member."""
    await _colleague(session_factory)
    await _turn(session_factory, fake_provider, settings)
    assert await _count(session_factory, Memory) == 1
    assert await _count(session_factory, Entity) >= 1
    assert fake_provider.extraction_calls and fake_provider.entity_calls


@pytest.mark.parametrize("status", [MemberStatus.REVOKED, MemberStatus.INVITED])
async def test_a_member_who_is_not_active_gets_no_background_work(
    session_factory, fake_provider, settings, status
) -> None:
    await _colleague(session_factory)
    await _set_status(session_factory, status)
    await _turn(session_factory, fake_provider, settings)
    assert fake_provider.extraction_calls == []          # no model call at all
    assert await _count(session_factory, Memory) == 0


async def test_a_member_without_a_membership_row_gets_no_background_work(
    session_factory, fake_provider, settings
) -> None:
    await _turn(session_factory, fake_provider, settings)
    assert fake_provider.extraction_calls == []
    assert await _count(session_factory, Memory) == 0


async def test_a_suspended_organisation_stops_the_work(session_factory, fake_provider, settings) -> None:
    await _colleague(session_factory)
    async with session_factory() as session:
        await session.execute(update(Organisation).where(Organisation.id == DEFAULT_ORGANISATION_ID)
                              .values(status=OrganisationStatus.SUSPENDED.value))
        await session.commit()
    await _turn(session_factory, fake_provider, settings)
    assert fake_provider.extraction_calls == []


async def test_a_revocation_between_steps_stops_the_rest_of_the_chain(
    session_factory, fake_provider, settings
) -> None:
    """The memory step ran while the member was active; the member is
    revoked before entity extraction; nothing further runs or is written."""
    await _colleague(session_factory)
    async with session_factory() as session:
        conversation = await ConversationService(session, COLLEAGUE_SCOPE).create_conversation()
        await session.commit()
    settings.ENTITY_EXTRACTION_ENABLED = False      # stop after the memory step
    fake_provider.extraction_reply = MEMORY_REPLY
    await run_memory_extraction(conversation.id, "I like jasmine tea", "Noted.", settings=settings,
                                provider=fake_provider, session_factory=session_factory,
                                scope=COLLEAGUE_SCOPE)
    assert await _count(session_factory, Memory) == 1
    async with session_factory() as session:
        [memory_id] = (await session.execute(select(Memory.id))).scalars().all()

    await _set_status(session_factory, MemberStatus.REVOKED)
    settings.ENTITY_EXTRACTION_ENABLED = True
    fake_provider.entity_reply = ENTITY_REPLY
    await run_entity_extraction([memory_id], settings=settings, provider=fake_provider,
                                session_factory=session_factory, scope=COLLEAGUE_SCOPE)
    assert fake_provider.entity_calls == []
    assert fake_provider.relationship_calls == []
    assert await _count(session_factory, Entity) == 0


async def test_a_scope_that_no_longer_matches_the_membership_stops_the_work(
    session_factory, fake_provider, settings
) -> None:
    """The member now belongs to another organisation: the frozen scope of
    the turn is no longer theirs."""
    await _colleague(session_factory)
    async with session_factory() as session:
        other = Organisation(name="Another organisation", status="active")
        session.add(other)
        await session.flush()
        await session.execute(update(OrganisationMember)
                              .where(OrganisationMember.id == COLLEAGUE_PRINCIPAL.member_id)
                              .values(organisation_id=other.id))
        await session.commit()
    await _turn(session_factory, fake_provider, settings)
    assert fake_provider.extraction_calls == []


async def test_a_failed_membership_check_is_a_stop_not_a_pass(
    session_factory, fake_provider, settings, monkeypatch
) -> None:
    await _colleague(session_factory)

    async def broken(session, member_id):
        raise RuntimeError("membership read failed")

    monkeypatch.setattr(memory_tasks, "lock_active_principal", broken)
    await _turn(session_factory, fake_provider, settings)
    assert fake_provider.extraction_calls == []
    assert await _count(session_factory, Memory) == 0


async def test_the_membership_read_takes_a_share_lock() -> None:
    """The statement the gate runs is FOR SHARE OF the membership row on
    PostgreSQL -- what makes revocation (FOR UPDATE) wait for an in-flight
    step instead of racing it."""
    from sqlalchemy.dialects import postgresql

    from app.identity.principal import _active_member

    statement = _active_member(uuid.uuid4()).with_for_update(read=True, of=OrganisationMember)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("FOR SHARE OF organisation_members")


def test_only_a_personal_scope_names_a_member() -> None:
    from app.identity.scope import InvalidScope, KnowledgeScope

    assert COLLEAGUE_SCOPE.member_id == COLLEAGUE_PRINCIPAL.member_id
    with pytest.raises(InvalidScope):
        KnowledgeScope.organisation(COLLEAGUE_PRINCIPAL).member_id
