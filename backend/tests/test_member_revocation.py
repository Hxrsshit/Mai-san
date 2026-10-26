"""Stage 7A: `MemberRevocation` through a real (SQLite) database.

The cancel service is built exactly as `deps.get_member_revocation` builds
it: a `MonitorRequestService` bound to the member being revoked. Failures are
injected by wrapping that factory, never by mocking the code under test.
Subjects, names and sentinel texts are synthetic.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.authorization.grants import GrantService
from app.authorization.models import ApprovalGrant
from app.core.config import Settings
from app.identity.constants import (
    DEFAULT_ORGANISATION_ID,
    LEGACY_ADMIN_MEMBER_ID,
    TELEGRAM_PROVIDER,
    MemberStatus,
    Role,
)
from app.identity.link_codes import code_sha256
from app.identity.models import (
    LinkCode,
    Organisation,
    OrganisationMember,
    User,
    UserIdentity,
)
from app.identity.schemas import RedeemResult, RevokeOutcome
from app.identity.service import MembershipService
from app.organisation import revocation as revocation_module
from app.organisation.revocation import (
    BATCH_SIZE,
    MAX_ROUNDS,
    MemberRevocation,
    RevocationIncomplete,
)
from app.tasks.models import Task, TaskEvent, TaskEventType
from app.tasks.requests import MonitorRequestService
from app.tasks.schemas import CancelReason, TaskOutcome, TaskResult
from app.tasks.states import TaskState
from app.tools.schemas import RiskLevel

SUBJECT = "918273645512"
SENTINEL = "sentinel-sqlalchemy-text-7a"
REVOCATION_LOGGER = "app.organisation.revocation"

NON_TERMINAL = [
    TaskState.PROPOSED,
    TaskState.PLANNED,
    TaskState.AWAITING_APPROVAL,
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.PAUSED,
    TaskState.BLOCKED,
]


# --- Helpers -----------------------------------------------------------------


@pytest.fixture
def svc_settings(settings: Settings) -> Settings:
    """No admin chat configured, so any synthetic subject may link."""
    return settings.model_copy(update={"TELEGRAM_ALLOWED_CHAT_ID": ""})


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Member:
    def __init__(self, member_id: uuid.UUID, user_id: uuid.UUID, open_code: str) -> None:
        self.id = member_id
        self.user_id = user_id
        self.open_code = open_code


async def _member(session_factory, settings: Settings, linked: bool, name: str = "Member") -> Member:
    """An invited member with an open code, or a linked (active) member that
    also holds a fresh open code."""
    async with session_factory() as session:
        service = MembershipService(session, settings)
        view, issued = await service.invite(DEFAULT_ORGANISATION_ID, name)
        open_code = issued.code
        if linked:
            result = await service.redeem_link_code(issued.code, TELEGRAM_PROVIDER, SUBJECT)
            assert result is RedeemResult.LINKED
            open_code = (await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)).code
        user_id = (
            await session.execute(
                select(OrganisationMember.user_id).where(OrganisationMember.id == view.id)
            )
        ).scalar_one()
        await session.commit()
    return Member(view.id, user_id, open_code)


async def _tasks(session_factory, owner_id: uuid.UUID, states: List[TaskState]) -> Dict[uuid.UUID, TaskState]:
    """Fixture task rows in the given states, oldest first."""
    base = datetime.now(timezone.utc) - timedelta(minutes=len(states) + 1)
    created = {}
    async with session_factory() as session:
        for index, state in enumerate(states):
            moment = base + timedelta(minutes=index)
            task = Task(
                owner_id=owner_id,
                objective="Fixture task",
                state=state,
                completed_at=moment if state is TaskState.COMPLETED else None,
                cancelled_at=moment if state is TaskState.CANCELLED else None,
                created_at=moment,
                updated_at=moment,
            )
            session.add(task)
            await session.flush()
            created[task.id] = state
        await session.commit()
    return created


async def _grant(session_factory, owner_id: uuid.UUID, live: bool = True) -> uuid.UUID:
    now = datetime.now(timezone.utc)
    if live:
        created, expires = now - timedelta(hours=1), now + timedelta(days=1)
    else:
        created, expires = now - timedelta(days=2), now - timedelta(days=1)
    async with session_factory() as session:
        grant = ApprovalGrant(
            owner_id=owner_id,
            capability="gmail_list_messages",
            risk_level=RiskLevel.HIGH,
            created_at=created,
            expires_at=expires,
        )
        session.add(grant)
        await session.flush()
        grant_id = grant.id
        await session.commit()
    return grant_id


def _factory(session, settings: Settings, calls: List[uuid.UUID]):
    """`deps.get_member_revocation`'s factory, recording who it was built for."""

    def cancel_service_for(member_id: uuid.UUID) -> MonitorRequestService:
        calls.append(member_id)
        return MonitorRequestService(session, settings=settings, owner_id=member_id)

    return cancel_service_for


async def _revoke(
    session_factory, settings: Settings, member_id: uuid.UUID,
    organisation_id: uuid.UUID = DEFAULT_ORGANISATION_ID,
    calls: Optional[List[uuid.UUID]] = None,
) -> RevokeOutcome:
    calls = calls if calls is not None else []
    async with session_factory() as session:
        revocation = MemberRevocation(session, cancel_service_for=_factory(session, settings, calls))
        outcome = await revocation.revoke(organisation_id, member_id)
        # Committed on success, rolled back on a refusal: never left open.
        assert not session.in_transaction()
        return outcome


async def _member_row(session_factory, member_id: uuid.UUID) -> OrganisationMember:
    async with session_factory() as session:
        return (
            await session.execute(select(OrganisationMember).where(OrganisationMember.id == member_id))
        ).scalar_one()


async def _code_row(session_factory, code: str) -> LinkCode:
    async with session_factory() as session:
        return (
            await session.execute(select(LinkCode).where(LinkCode.code_sha256 == code_sha256(code)))
        ).scalar_one()


async def _identities(session_factory, user_id: uuid.UUID) -> List[UserIdentity]:
    async with session_factory() as session:
        return list(
            (await session.execute(select(UserIdentity).where(UserIdentity.user_id == user_id)))
            .scalars().all()
        )


async def _states(session_factory, task_ids) -> Dict[uuid.UUID, TaskState]:
    async with session_factory() as session:
        rows = (await session.execute(select(Task.id, Task.state).where(Task.id.in_(list(task_ids))))).all()
    return {row[0]: row[1] for row in rows}


async def _grants(session_factory, owner_id: uuid.UUID) -> Dict[uuid.UUID, ApprovalGrant]:
    async with session_factory() as session:
        rows = (
            await session.execute(select(ApprovalGrant).where(ApprovalGrant.owner_id == owner_id))
        ).scalars().all()
    return {row.id: row for row in rows}


async def _cancel_reasons(session_factory, task_id: uuid.UUID) -> List[Optional[str]]:
    async with session_factory() as session:
        events = (
            await session.execute(
                select(TaskEvent).where(
                    TaskEvent.task_id == task_id,
                    TaskEvent.event_type == TaskEventType.TASK_CANCELLED,
                )
            )
        ).scalars().all()
    return [(event.event_metadata or {}).get("reason") for event in events]


async def _other_organisation_member(session_factory) -> Tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        organisation = Organisation(name="Other organisation", status="active")
        user = User(display_name="Other person")
        session.add_all([organisation, user])
        await session.flush()
        member = OrganisationMember(
            organisation_id=organisation.id, user_id=user.id, role=Role.MEMBER.value,
            status=MemberStatus.ACTIVE.value, activated_at=now,
        )
        session.add(member)
        await session.flush()
        ids = (organisation.id, member.id)
        await session.commit()
    return ids


def _record_text(record: logging.LogRecord) -> str:
    return record.getMessage() + " " + repr(sorted((k, repr(v)) for k, v in record.__dict__.items()))


# --- The membership --------------------------------------------------------------


@pytest.mark.parametrize("linked", [False, True], ids=["invited", "active"])
async def test_a_member_is_revoked_with_codes_closed_and_identity_detached(
    session_factory, svc_settings, linked
) -> None:
    member = await _member(session_factory, svc_settings, linked=linked)
    before = await _member_row(session_factory, member.id)
    assert before.status == (MemberStatus.ACTIVE.value if linked else MemberStatus.INVITED.value)

    calls: List[uuid.UUID] = []
    outcome = await _revoke(session_factory, svc_settings, member.id, calls=calls)

    assert outcome is RevokeOutcome.REVOKED
    row = await _member_row(session_factory, member.id)
    assert row.status == MemberStatus.REVOKED.value
    assert row.revoked_at is not None
    assert row.revoked_reason == "admin_revoked"
    assert (await _code_row(session_factory, member.open_code)).invalidated_at is not None
    identities = await _identities(session_factory, member.user_id)
    assert len(identities) == (1 if linked else 0)
    assert all(identity.detached_at is not None for identity in identities)
    assert calls == []                      # no task, so no cancel service was built


# --- Tasks -----------------------------------------------------------------------------


async def test_every_non_terminal_task_is_cancelled_and_completed_is_untouched(
    session_factory, svc_settings
) -> None:
    member = await _member(session_factory, svc_settings, linked=True)
    tasks = await _tasks(session_factory, member.id, NON_TERMINAL + [TaskState.COMPLETED])
    admin_tasks = await _tasks(session_factory, LEGACY_ADMIN_MEMBER_ID, [TaskState.PLANNED])

    calls: List[uuid.UUID] = []
    assert await _revoke(session_factory, svc_settings, member.id, calls=calls) is RevokeOutcome.REVOKED

    states = await _states(session_factory, tasks)
    for task_id, original in tasks.items():
        if original is TaskState.COMPLETED:
            assert states[task_id] is TaskState.COMPLETED
            assert await _cancel_reasons(session_factory, task_id) == []
        else:
            assert states[task_id] is TaskState.CANCELLED, original
            assert await _cancel_reasons(session_factory, task_id) == [CancelReason.MEMBER_REVOKED.value]
    # The cancel service was built once per task, and only for this member.
    assert calls == [member.id] * len(NON_TERMINAL)
    # Nobody else's work moved.
    assert await _states(session_factory, admin_tasks) == admin_tasks


async def test_tasks_are_cancelled_newest_first(session_factory, svc_settings) -> None:
    member = await _member(session_factory, svc_settings, linked=False)
    tasks = await _tasks(session_factory, member.id, [TaskState.PLANNED] * 3)
    order: List[uuid.UUID] = []

    class Recording:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def cancel(self, task_id, *, reason=CancelReason.OWNER):
            order.append(task_id)
            return await self._inner.cancel(task_id, reason=reason)

    async with session_factory() as session:
        def factory(member_id):
            return Recording(MonitorRequestService(session, settings=svc_settings, owner_id=member_id))

        await MemberRevocation(session, cancel_service_for=factory).revoke(DEFAULT_ORGANISATION_ID, member.id)
    assert order == list(reversed(list(tasks)))


async def test_already_revoked_still_cancels_a_task_created_afterwards(
    session_factory, svc_settings
) -> None:
    member = await _member(session_factory, svc_settings, linked=True)
    assert await _revoke(session_factory, svc_settings, member.id) is RevokeOutcome.REVOKED
    first = await _member_row(session_factory, member.id)

    late = await _tasks(session_factory, member.id, [TaskState.QUEUED])
    late_grant = await _grant(session_factory, member.id)
    calls: List[uuid.UUID] = []
    assert await _revoke(session_factory, svc_settings, member.id, calls=calls) is RevokeOutcome.ALREADY_REVOKED

    assert set((await _states(session_factory, late)).values()) == {TaskState.CANCELLED}
    assert (await _grants(session_factory, member.id))[late_grant].revoked_reason == "member_revoked"
    assert calls == [member.id]
    again = await _member_row(session_factory, member.id)
    assert again.status == MemberStatus.REVOKED.value
    assert _aware(again.revoked_at) == _aware(first.revoked_at)   # the first revocation stands


# --- Grants ----------------------------------------------------------------------------


async def test_live_grants_are_revoked_and_an_expired_one_is_left(session_factory, svc_settings) -> None:
    member = await _member(session_factory, svc_settings, linked=True)
    live = [await _grant(session_factory, member.id), await _grant(session_factory, member.id)]
    expired = await _grant(session_factory, member.id, live=False)
    admin_grant = await _grant(session_factory, LEGACY_ADMIN_MEMBER_ID)

    assert await _revoke(session_factory, svc_settings, member.id) is RevokeOutcome.REVOKED

    grants = await _grants(session_factory, member.id)
    for grant_id in live:
        assert grants[grant_id].revoked_at is not None
        assert grants[grant_id].revoked_reason == "member_revoked"
    assert grants[expired].revoked_at is None              # already dead; not rewritten
    assert (await _grants(session_factory, LEGACY_ADMIN_MEMBER_ID))[admin_grant].revoked_at is None


# --- Atomicity ---------------------------------------------------------------------------


class _Failing:
    """A cancel service that fails, as the second task's cancel."""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    async def cancel(self, task_id, *, reason=CancelReason.OWNER) -> TaskResult:
        if self._mode == "raises":
            raise OperationalError(
                f"UPDATE tasks SET state = 'cancelled' -- {SENTINEL}",
                {"subject": SUBJECT, "note": SENTINEL},
                Exception(SENTINEL),
            )
        return TaskResult(outcome=TaskOutcome.FAILED, reason="persistence_failed")


@pytest.mark.parametrize("mode", ["raises", "failed"])
async def test_a_failure_on_the_second_task_rolls_everything_back(
    session_factory, svc_settings, caplog, mode
) -> None:
    caplog.set_level(logging.INFO)
    member = await _member(session_factory, svc_settings, linked=True)
    tasks = await _tasks(session_factory, member.id, [TaskState.PLANNED, TaskState.QUEUED, TaskState.RUNNING])
    live = [await _grant(session_factory, member.id), await _grant(session_factory, member.id)]

    calls: List[uuid.UUID] = []
    async with session_factory() as session:
        def factory(member_id):
            calls.append(member_id)
            if len(calls) == 2:
                return _Failing(mode)
            return MonitorRequestService(session, settings=svc_settings, owner_id=member_id)

        with pytest.raises(RevocationIncomplete) as raised:
            await MemberRevocation(session, cancel_service_for=factory).revoke(
                DEFAULT_ORGANISATION_ID, member.id
            )
        # Rolled back by the revocation itself, not merely by closing the
        # session: the same session no longer sees any of its writes.
        assert not session.in_transaction()
        assert (
            await session.execute(
                select(OrganisationMember.status).where(OrganisationMember.id == member.id)
            )
        ).scalar_one() == MemberStatus.ACTIVE.value
    assert raised.value.reason == ("unexpected_error" if mode == "raises" else "task_not_cancelled")
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert len(calls) == 2

    # The member is exactly as it was: active, linked, its open code open.
    row = await _member_row(session_factory, member.id)
    assert row.status == MemberStatus.ACTIVE.value
    assert row.revoked_at is None and row.revoked_reason is None
    [identity] = await _identities(session_factory, member.user_id)
    assert identity.detached_at is None
    code = await _code_row(session_factory, member.open_code)
    assert code.invalidated_at is None and code.consumed_at is None
    # No task was cancelled -- not even the first, whose cancel succeeded.
    assert await _states(session_factory, tasks) == tasks
    for task_id in tasks:
        assert await _cancel_reasons(session_factory, task_id) == []
    # No grant was revoked.
    grants = await _grants(session_factory, member.id)
    assert all(grants[grant_id].revoked_at is None for grant_id in live)

    # One error line, a reason code only.
    failures = [r for r in caplog.records if r.name == REVOCATION_LOGGER and r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in failures] == ["Member revocation failed"]
    assert failures[0].reason == raised.value.reason
    assert failures[0].exc_info is None and failures[0].exc_text is None
    for record in caplog.records:
        text = _record_text(record)
        assert SENTINEL not in text, record.name
        assert SUBJECT not in text, record.name
        assert member.open_code not in text, record.name


async def test_a_cancel_that_reports_terminal_without_finishing_fails_the_postcondition(
    session_factory, svc_settings
) -> None:
    """A dishonest TERMINAL is caught by the direct count, and the loop is bounded."""
    member = await _member(session_factory, svc_settings, linked=False)
    tasks = await _tasks(session_factory, member.id, [TaskState.PLANNED, TaskState.BLOCKED])
    calls: List[uuid.UUID] = []

    class Liar:
        async def cancel(self, task_id, *, reason=CancelReason.OWNER) -> TaskResult:
            return TaskResult(outcome=TaskOutcome.TERMINAL, task_id=task_id)

    async with session_factory() as session:
        def factory(member_id):
            calls.append(member_id)
            return Liar()

        with pytest.raises(RevocationIncomplete) as raised:
            await MemberRevocation(session, cancel_service_for=factory).revoke(
                DEFAULT_ORGANISATION_ID, member.id
            )
    assert raised.value.reason == "tasks_remain"
    assert len(calls) == MAX_ROUNDS * len(tasks)
    assert (await _member_row(session_factory, member.id)).status == MemberStatus.INVITED.value
    assert await _states(session_factory, tasks) == tasks


@pytest.mark.parametrize("returned", ["none", "unrevoked", "lies"])
async def test_a_grant_that_is_not_revoked_rolls_everything_back(
    session_factory, svc_settings, monkeypatch, returned
) -> None:
    member = await _member(session_factory, svc_settings, linked=True)
    tasks = await _tasks(session_factory, member.id, [TaskState.PLANNED])
    live = await _grant(session_factory, member.id)

    async def no_revoke(self, grant_id, reason=None, now=None):
        if returned == "none":
            return None
        if returned == "lies":
            # Says it is revoked; the row is not. Only the direct count sees it.
            return SimpleNamespace(id=grant_id, revoked_at=datetime.now(timezone.utc))
        return await self.get(grant_id)          # found, but still unrevoked

    monkeypatch.setattr(GrantService, "revoke", no_revoke)
    with pytest.raises(RevocationIncomplete) as raised:
        await _revoke(session_factory, svc_settings, member.id)
    assert raised.value.reason == ("grants_remain" if returned == "lies" else "grant_not_revoked")

    assert (await _member_row(session_factory, member.id)).status == MemberStatus.ACTIVE.value
    assert await _states(session_factory, tasks) == tasks
    assert (await _grants(session_factory, member.id))[live].revoked_at is None
    assert (await _code_row(session_factory, member.open_code)).invalidated_at is None


async def test_a_failing_membership_write_is_reported_and_rolled_back(
    session_factory, svc_settings, monkeypatch
) -> None:
    member = await _member(session_factory, svc_settings, linked=True)
    calls: List[uuid.UUID] = []

    async def broken(self, organisation_id, member_id, reason=None, now=None):
        raise OperationalError(f"UPDATE organisation_members -- {SENTINEL}", {}, Exception(SENTINEL))

    monkeypatch.setattr(MembershipService, "mark_revoked", broken)
    with pytest.raises(RevocationIncomplete) as raised:
        await _revoke(session_factory, svc_settings, member.id, calls=calls)
    assert raised.value.reason == "membership_failed"
    assert SENTINEL not in str(raised.value)
    assert calls == []
    assert (await _member_row(session_factory, member.id)).status == MemberStatus.ACTIVE.value


async def _unchanged(session_factory, member: Member, tasks, grants: List[uuid.UUID]) -> None:
    """The linked member, its open code, its tasks and its grants are exactly
    as they were before the revocation was attempted."""
    row = await _member_row(session_factory, member.id)
    assert row.status == MemberStatus.ACTIVE.value
    assert row.revoked_at is None and row.revoked_reason is None
    [identity] = await _identities(session_factory, member.user_id)
    assert identity.detached_at is None
    code = await _code_row(session_factory, member.open_code)
    assert code.invalidated_at is None and code.consumed_at is None
    assert await _states(session_factory, tasks) == tasks
    for task_id in tasks:
        assert await _cancel_reasons(session_factory, task_id) == []
    stored = await _grants(session_factory, member.id)
    for grant_id in grants:
        assert stored[grant_id].revoked_at is None, grant_id
        assert stored[grant_id].revoked_reason is None, grant_id


async def test_a_real_grant_revocation_is_rolled_back_when_the_next_fails(
    session_factory, svc_settings, monkeypatch
) -> None:
    """The first grant really is revoked (the real UPDATE runs); the second
    is not. The first revocation must not survive."""
    member = await _member(session_factory, svc_settings, linked=True)
    tasks = await _tasks(session_factory, member.id, [TaskState.QUEUED])
    grants = [await _grant(session_factory, member.id), await _grant(session_factory, member.id)]
    real_revoke = GrantService.revoke
    seen: List[Tuple[uuid.UUID, Optional[datetime]]] = []

    async def second_fails(self, grant_id, reason=None, now=None):
        if seen:
            seen.append((grant_id, None))
            return None
        grant = await real_revoke(self, grant_id, reason=reason, now=now)
        seen.append((grant_id, grant.revoked_at))
        return grant

    monkeypatch.setattr(GrantService, "revoke", second_fails)
    with pytest.raises(RevocationIncomplete) as raised:
        await _revoke(session_factory, svc_settings, member.id)

    assert raised.value.reason == "grant_not_revoked"
    assert len(seen) == 2
    first_id, first_revoked_at = seen[0]
    assert first_revoked_at is not None             # it was really written...
    assert (await _grants(session_factory, member.id))[first_id].revoked_at is None   # ...and undone
    await _unchanged(session_factory, member, tasks, grants)


async def test_a_failing_commit_leaves_everything_unchanged(
    session_factory, svc_settings, caplog, monkeypatch
) -> None:
    caplog.set_level(logging.INFO)
    member = await _member(session_factory, svc_settings, linked=True)
    tasks = await _tasks(session_factory, member.id, [TaskState.PLANNED, TaskState.RUNNING])
    grants = [await _grant(session_factory, member.id), await _grant(session_factory, member.id)]
    commits: List[str] = []

    async with session_factory() as session:
        async def failing_commit():
            commits.append("commit")
            raise OperationalError(f"COMMIT -- {SENTINEL}", {"subject": SUBJECT}, Exception(SENTINEL))

        monkeypatch.setattr(session, "commit", failing_commit)
        revocation = MemberRevocation(session, cancel_service_for=_factory(session, svc_settings, []))
        with pytest.raises(RevocationIncomplete) as raised:
            await revocation.revoke(DEFAULT_ORGANISATION_ID, member.id)
        assert not session.in_transaction()

    assert raised.value.reason == "unexpected_error"
    assert commits == ["commit"]
    await _unchanged(session_factory, member, tasks, grants)
    for record in caplog.records:
        assert SENTINEL not in _record_text(record), record.name


# --- Refusals ------------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["admin", "unknown"])
async def test_a_failing_rollback_still_answers_the_refusal(
    session_factory, svc_settings, caplog, monkeypatch, target
) -> None:
    caplog.set_level(logging.INFO)
    member_id = LEGACY_ADMIN_MEMBER_ID if target == "admin" else uuid.uuid4()
    expected = RevokeOutcome.CANNOT_REVOKE_ADMIN if target == "admin" else RevokeOutcome.NOT_FOUND
    rollbacks: List[str] = []

    async with session_factory() as session:
        async def failing_rollback():
            rollbacks.append("rollback")
            raise OperationalError(f"ROLLBACK -- {SENTINEL}", {}, Exception(SENTINEL))

        monkeypatch.setattr(session, "rollback", failing_rollback)
        calls: List[uuid.UUID] = []
        revocation = MemberRevocation(session, cancel_service_for=_factory(session, svc_settings, calls))
        outcome = await revocation.revoke(DEFAULT_ORGANISATION_ID, member_id)

    assert outcome is expected
    assert rollbacks == ["rollback"]
    assert calls == []
    warnings = [r for r in caplog.records if r.name == REVOCATION_LOGGER and r.levelno == logging.WARNING]
    assert [r.reason for r in warnings] == ["refusal_rollback_failed"]
    for record in caplog.records:
        assert SENTINEL not in _record_text(record), record.name
        assert record.exc_info is None, record.name
    admin = await _member_row(session_factory, LEGACY_ADMIN_MEMBER_ID)
    assert admin.status == MemberStatus.ACTIVE.value and admin.revoked_at is None


async def test_another_organisations_member_is_not_found_and_untouched(
    session_factory, svc_settings
) -> None:
    _, other_member = await _other_organisation_member(session_factory)
    tasks = await _tasks(session_factory, other_member, [TaskState.PLANNED])
    grant = await _grant(session_factory, other_member)
    calls: List[uuid.UUID] = []

    outcome = await _revoke(session_factory, svc_settings, other_member, calls=calls)

    assert outcome is RevokeOutcome.NOT_FOUND
    assert calls == []
    assert (await _member_row(session_factory, other_member)).status == MemberStatus.ACTIVE.value
    assert await _states(session_factory, tasks) == tasks
    assert (await _grants(session_factory, other_member))[grant].revoked_at is None


async def test_an_unknown_member_is_not_found(session_factory, svc_settings) -> None:
    calls: List[uuid.UUID] = []
    assert await _revoke(session_factory, svc_settings, uuid.uuid4(), calls=calls) is RevokeOutcome.NOT_FOUND
    assert calls == []


async def test_the_seeded_admin_cannot_be_revoked_and_nothing_changes(
    session_factory, svc_settings
) -> None:
    tasks = await _tasks(session_factory, LEGACY_ADMIN_MEMBER_ID, [TaskState.PLANNED, TaskState.QUEUED])
    grant = await _grant(session_factory, LEGACY_ADMIN_MEMBER_ID)
    calls: List[uuid.UUID] = []

    outcome = await _revoke(session_factory, svc_settings, LEGACY_ADMIN_MEMBER_ID, calls=calls)

    assert outcome is RevokeOutcome.CANNOT_REVOKE_ADMIN
    assert calls == []
    admin = await _member_row(session_factory, LEGACY_ADMIN_MEMBER_ID)
    assert admin.status == MemberStatus.ACTIVE.value and admin.revoked_at is None
    assert await _states(session_factory, tasks) == tasks
    assert (await _grants(session_factory, LEGACY_ADMIN_MEMBER_ID))[grant].revoked_at is None


# --- Logs ----------------------------------------------------------------------------------


async def test_a_revocation_logs_no_code_subject_or_hash(session_factory, svc_settings, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    member = await _member(session_factory, svc_settings, linked=True)
    await _tasks(session_factory, member.id, [TaskState.QUEUED])
    await _grant(session_factory, member.id)
    caplog.clear()

    assert await _revoke(session_factory, svc_settings, member.id) is RevokeOutcome.REVOKED

    assert caplog.records, "the revocation logged nothing at all"
    done = [r for r in caplog.records if r.name == REVOCATION_LOGGER]
    assert [r.getMessage() for r in done] == ["Member revocation committed"]
    assert done[0].tasks_cancelled == 1 and done[0].grants_revoked == 1
    for record in caplog.records:
        text = _record_text(record)
        assert member.open_code not in text, record.name
        assert code_sha256(member.open_code) not in text, record.name
        assert SUBJECT not in text, record.name


# --- Bounds ----------------------------------------------------------------------------------


def test_the_loops_are_bounded() -> None:
    assert (MAX_ROUNDS, BATCH_SIZE) == (10, 50)
    assert revocation_module.REVOKED_REASON == "admin_revoked"
    assert revocation_module.GRANT_REVOKED_REASON == CancelReason.MEMBER_REVOKED.value
