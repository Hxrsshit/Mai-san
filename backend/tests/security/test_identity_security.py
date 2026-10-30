"""Stage 7A security: the membership seam, the organisation routes and
revocation's all-or-nothing rule, through the real app and real SQLite.

Written from the Stage 7A contract, not from the implementation:

- the API token is accepted only while its member is active in an active
  organisation, checked in the database on every request (403
  `membership_inactive`); a failed check is 503 `membership_unavailable` and
  never access; pending work before the seam is 500 `seam_invariant`;
- `get_principal` re-reads membership itself, so a replaced seam still
  cannot reach `/api/organisation`; `require_admin` refuses a non-Admin;
- an invitation's response carries no identifier, hash or token, and the
  link code's plaintext is stored nowhere;
- a revocation whose postconditions do not hold commits nothing.

Every credential, code and parameter here is a synthetic sentinel.
"""

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.authorization.models import ApprovalGrant
from app.core.config import get_settings
from app.core.errors import (
    AdminRequired,
    MembershipInactive,
    MembershipUnavailable,
    SeamInvariantError,
)
from app.core.owner import CONFIGURED_OWNER_ID
from app.database.metadata import Base
from app.database.session import get_db_session, get_session_factory
from app.identity.constants import (
    DEFAULT_ORGANISATION_ID,
    LINK_CODE_ALPHABET,
    MemberStatus,
    OrganisationStatus,
    Role,
)
from app.identity.models import (
    LinkCode,
    Organisation,
    OrganisationMember,
    User,
    UserIdentity,
)
from app.identity.principal import Principal
from app.identity.schemas import RedeemResult
from app.identity.service import MembershipService
from app.llm.factory import get_llm_provider
from app.main import create_app
from app.organisation.revocation import MemberRevocation, RevocationIncomplete
from app.tasks.models import Task, TaskEvent
from app.tasks.requests import MonitorRequestService
from app.tasks.schemas import TaskOutcome, TaskResult
from app.tasks.service import TaskService
from app.tasks.states import TERMINAL_STATES, TaskState
from app.tools.schemas import RiskLevel
from tests.conftest import AUTH_HEADERS

pytestmark = pytest.mark.anyio

#: Text a database error would carry: a statement and a bound parameter.
SENTINEL_STATEMENT = "SELECT sentinel FROM organisation_members WHERE id = :p"
SENTINEL_PARAM = "SENTINEL-PARAM-7a51"
SENTINEL_ORIG = "SENTINEL-DRIVER-TEXT-7a52"
HEX64 = re.compile(r"[0-9a-fA-F]{64}")
SUBJECT = "4242424242"


def database_error() -> OperationalError:
    return OperationalError(SENTINEL_STATEMENT, {"p": SENTINEL_PARAM}, Exception(SENTINEL_ORIG))


def now() -> datetime:
    return datetime.now(timezone.utc)


async def make_client(session_factory, fake_provider, settings, overrides=None) -> AsyncClient:
    """The `client` fixture's app, with extra dependency overrides."""
    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides.update(overrides or {})
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS)


async def set_admin_status(session_factory, status: MemberStatus) -> None:
    async with session_factory() as session:
        await session.execute(
            update(OrganisationMember)
            .where(OrganisationMember.id == CONFIGURED_OWNER_ID)
            .values(status=status.value,
                    revoked_at=now() if status is MemberStatus.REVOKED else None)
        )
        await session.commit()


async def set_organisation_status(session_factory, status: OrganisationStatus) -> None:
    async with session_factory() as session:
        await session.execute(
            update(Organisation)
            .where(Organisation.id == DEFAULT_ORGANISATION_ID)
            .values(status=status.value)
        )
        await session.commit()


def error_of(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message", "request_id"}, body
    return body["error"]


# ============================================================================
# A. Seam refusals
# ============================================================================


async def test_a_control_the_active_admin_reaches_a_data_route(client) -> None:
    response = await client.get("/api/conversations")
    assert response.status_code == 200, response.text


async def test_an_inactive_admin_is_refused_on_a_data_route(client, session_factory) -> None:
    await set_admin_status(session_factory, MemberStatus.REVOKED)
    response = await client.get("/api/conversations")
    assert response.status_code == 403
    assert error_of(response)["code"] == "membership_inactive"


async def test_a_suspended_organisation_is_refused_on_a_data_route(client, session_factory) -> None:
    await set_organisation_status(session_factory, OrganisationStatus.SUSPENDED)
    response = await client.get("/api/conversations")
    assert response.status_code == 403
    assert error_of(response)["code"] == "membership_inactive"


async def test_the_two_refusals_are_indistinguishable(client, session_factory) -> None:
    await set_admin_status(session_factory, MemberStatus.REVOKED)
    member = await client.get("/api/conversations")
    await set_admin_status(session_factory, MemberStatus.ACTIVE)
    async with session_factory() as session:
        await session.execute(update(OrganisationMember)
                              .where(OrganisationMember.id == CONFIGURED_OWNER_ID)
                              .values(activated_at=now()))
        await session.commit()
    assert (await client.get("/api/conversations")).status_code == 200
    await set_organisation_status(session_factory, OrganisationStatus.SUSPENDED)
    organisation = await client.get("/api/conversations")
    a, b = error_of(member), error_of(organisation)
    assert (member.status_code, a["code"], a["message"]) == (
        organisation.status_code, b["code"], b["message"])


async def test_a_database_error_in_the_check_is_503_with_no_data(
    client, monkeypatch
) -> None:
    async def failing(session, member_id):
        raise database_error()

    monkeypatch.setattr(deps, "load_active_principal", failing)
    response = await client.get("/api/conversations")
    assert response.status_code == 503
    error = error_of(response)
    assert error["code"] == "membership_unavailable"
    assert error["message"] == MembershipUnavailable.message
    assert SENTINEL_PARAM not in response.text and SENTINEL_ORIG not in response.text


async def test_a_connection_error_in_the_check_is_503_not_500(client, monkeypatch) -> None:
    async def refused(session, member_id):
        raise OSError("connection refused")

    monkeypatch.setattr(deps, "load_active_principal", refused)
    response = await client.get("/api/conversations")
    assert response.status_code == 503
    assert error_of(response)["code"] == "membership_unavailable"
    assert "connection refused" not in response.text


async def test_the_503_log_record_carries_only_the_exception_class(
    client, monkeypatch, caplog
) -> None:
    async def failing(session, member_id):
        raise database_error()

    monkeypatch.setattr(deps, "load_active_principal", failing)
    caplog.set_level(logging.INFO)
    response = await client.get("/api/conversations")
    assert response.status_code == 503

    standard = set(logging.LogRecord("n", 0, "p", 0, "m", (), None).__dict__) | {"message", "asctime"}
    [record] = [r for r in caplog.records if r.getMessage() == "Membership check failed"]
    assert record.category == "OperationalError"
    assert set(record.__dict__) - standard == {"category"}
    assert record.args in ((), None) and record.exc_info is None and record.exc_text is None
    for r in caplog.records:
        rendered = " ".join(str(v) for v in r.__dict__.values())
        for secret in (SENTINEL_PARAM, SENTINEL_ORIG, "sentinel FROM"):
            assert secret not in rendered, (r.name, r.getMessage())


# ============================================================================
# B. get_principal re-reads membership; require_admin
# ============================================================================


ORGANISATION_CALLS = (
    ("GET", "/api/organisation", None),
    ("GET", "/api/organisation/members", None),
    ("POST", "/api/organisation/members", {"display_name": "Sentinel Person"}),
    ("POST", f"/api/organisation/members/{uuid.uuid4()}/link-codes", {}),
    ("POST", f"/api/organisation/members/{uuid.uuid4()}/revoke", {}),
)


async def member_count(session_factory) -> int:
    async with session_factory() as session:
        return len((await session.execute(select(OrganisationMember.id))).all())


async def test_a_replaced_seam_with_a_random_owner_cannot_reach_the_organisation(
    session_factory, fake_provider, settings
) -> None:
    before = await member_count(session_factory)
    async with await make_client(session_factory, fake_provider, settings,
                                 {deps.get_owner_id: lambda: uuid.uuid4()}) as http:
        for method, path, body in ORGANISATION_CALLS:
            response = await http.request(method, path, json=body)
            assert response.status_code == 403, (path, response.text)
            assert error_of(response)["code"] == "membership_inactive"
    assert await member_count(session_factory) == before


async def test_a_replaced_seam_naming_a_revoked_admin_cannot_reach_the_organisation(
    session_factory, fake_provider, settings
) -> None:
    await set_admin_status(session_factory, MemberStatus.REVOKED)
    async with await make_client(session_factory, fake_provider, settings,
                                 {deps.get_owner_id: lambda: CONFIGURED_OWNER_ID}) as http:
        response = await http.get("/api/organisation/members")
    assert response.status_code == 403
    assert error_of(response)["code"] == "membership_inactive"


async def test_a_control_the_admin_reaches_the_organisation(client) -> None:
    response = await client.get("/api/organisation/members")
    assert response.status_code == 200, response.text


def test_require_admin_refuses_a_member_and_accepts_an_admin() -> None:
    member = Principal(member_id=uuid.uuid4(), organisation_id=DEFAULT_ORGANISATION_ID,
                       role=Role.MEMBER)
    with pytest.raises(AdminRequired):
        deps.require_admin(member)
    admin = Principal(member_id=CONFIGURED_OWNER_ID, organisation_id=DEFAULT_ORGANISATION_ID,
                      role=Role.ADMIN)
    assert deps.require_admin(admin) is admin


async def test_a_member_principal_is_refused_by_every_organisation_route(
    session_factory, fake_provider, settings
) -> None:
    async def a_member() -> Principal:
        return Principal(member_id=CONFIGURED_OWNER_ID, organisation_id=DEFAULT_ORGANISATION_ID,
                         role=Role.MEMBER)

    async with await make_client(session_factory, fake_provider, settings,
                                 {deps.get_principal: a_member}) as http:
        for method, path, body in ORGANISATION_CALLS:
            response = await http.request(method, path, json=body)
            assert response.status_code == 403, (path, response.text)
            assert error_of(response)["code"] == "admin_required"


# ============================================================================
# C. The invitation response and the stored code
# ============================================================================


def keys_of(value) -> set:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


async def test_an_invitation_carries_no_identifier_hash_or_token(client, session_factory, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="app")
    before = now()
    response = await client.post("/api/organisation/members", json={"display_name": "Sentinel Person"})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert not keys_of(body) & {"user_id", "subject", "code_sha256", "token"}
    assert not HEX64.search(response.text)

    code = body["link_code"]["code"]
    assert len(code) == 16 and set(code) <= set(LINK_CODE_ALPHABET)
    expires = datetime.fromisoformat(body["link_code"]["expires_at"].replace("Z", "+00:00"))
    assert timedelta(seconds=590) <= expires - before <= timedelta(seconds=610)

    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    async with session_factory() as session:
        stored = (await session.execute(select(LinkCode.code_sha256))).scalars().all()
        assert stored == [digest]                 # stored, and only as its hash
        scanned = 0
        for table in Base.metadata.sorted_tables:
            for row in (await session.execute(select(table))).all():
                for value in row:
                    scanned += 1
                    assert code not in str(value), table.name
    assert scanned > 10
    # The application's own log records (a driver's DEBUG statement log is
    # the driver's, and is off in production).
    app_records = [r for r in caplog.records if r.name == "app" or r.name.startswith("app.")]
    assert app_records
    for record in app_records:
        rendered = " ".join(str(v) for v in record.__dict__.values())
        assert code not in rendered and digest not in rendered, record.getMessage()


async def test_a_new_link_code_response_is_no_store_and_carries_no_hash(client) -> None:
    invited = (await client.post("/api/organisation/members",
                                 json={"display_name": "Sentinel Person"})).json()
    member_id = invited["member"]["id"]
    response = await client.post(f"/api/organisation/members/{member_id}/link-codes", json={})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    assert not keys_of(response.json()) & {"user_id", "subject", "code_sha256", "token"}
    assert not HEX64.search(response.text)


# ============================================================================
# D. The seam's own invariants, called directly
# ============================================================================


async def test_pending_writes_before_the_seam_are_a_programming_error(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(display_name="Sentinel Pending"))
        with pytest.raises(SeamInvariantError):
            await deps._active_principal(session, CONFIGURED_OWNER_ID)
        assert len(session.new) == 1            # nothing was rolled back over it
        await session.rollback()


async def test_an_open_transaction_before_the_seam_is_a_programming_error(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
        assert session.in_transaction()
        with pytest.raises(SeamInvariantError):
            await deps._active_principal(session, CONFIGURED_OWNER_ID)
        assert session.in_transaction()         # not ended by the seam
        await session.rollback()


async def test_a_control_a_clean_session_passes_the_seam(session_factory) -> None:
    async with session_factory() as session:
        principal = await deps._active_principal(session, CONFIGURED_OWNER_ID)
        assert principal is not None and principal.role is Role.ADMIN
        assert not session.in_transaction()     # the read was ended at once


async def test_a_failing_rollback_never_masks_the_original_error(session_factory, monkeypatch) -> None:
    async def not_a_database_error(session, member_id):
        raise ValueError("the original error")

    monkeypatch.setattr(deps, "load_active_principal", not_a_database_error)
    async with session_factory() as session:
        rollbacks = []

        async def failing_rollback():
            rollbacks.append(1)
            raise database_error()

        monkeypatch.setattr(session, "rollback", failing_rollback)
        with pytest.raises(ValueError, match="the original error"):
            await deps._active_principal(session, CONFIGURED_OWNER_ID)
        assert rollbacks, "the failing rollback was never reached"


def test_the_membership_errors_have_their_status_and_codes_and_say_nothing_specific() -> None:
    expected = {
        MembershipInactive: (403, "membership_inactive"),
        MembershipUnavailable: (503, "membership_unavailable"),
        AdminRequired: (403, "admin_required"),
        SeamInvariantError: (500, "seam_invariant"),
    }
    for error, (status, code) in expected.items():
        assert (error.status_code, error.code) == (status, code), error.__name__
        message = error().message.lower()
        for tell in ("revoked", "suspended", "invited", "disabled", "deleted",
                     "your organisation is", "your membership is", "member was", "organisation was"):
            assert tell not in message, (error.__name__, message)


# ============================================================================
# E. Revocation commits nothing unless its postconditions hold
# ============================================================================


async def linked_member_with_work(session_factory, settings) -> uuid.UUID:
    """An active member with an identity, an open code, a task and a grant."""
    async with session_factory() as session:
        service = MembershipService(session, settings=settings)
        view, issued = await service.invite(DEFAULT_ORGANISATION_ID, "Sentinel Member")
        await session.commit()
        assert await service.redeem_link_code(issued.code, "telegram", SUBJECT) is RedeemResult.LINKED
        await session.commit()
        await service.issue_link_code(DEFAULT_ORGANISATION_ID, view.id)   # left open
        await session.commit()
        created = await TaskService(session, settings=settings, owner_id=view.id).create_for_user(
            "Sentinel objective")
        assert created.ok, created
        moment = now()
        session.add(ApprovalGrant(owner_id=view.id, capability="gmail_list_messages",
                                  risk_level=RiskLevel.HIGH, created_at=moment,
                                  expires_at=moment + timedelta(days=1)))
        await session.commit()
        return view.id


async def state_of(session_factory, member_id) -> dict:
    async with session_factory() as session:
        member = (await session.execute(
            select(OrganisationMember).where(OrganisationMember.id == member_id))).scalar_one()
        attached = (await session.execute(
            select(UserIdentity.id).where(UserIdentity.user_id == member.user_id,
                                          UserIdentity.detached_at.is_(None)))).all()
        open_codes = (await session.execute(
            select(LinkCode.id).where(LinkCode.member_id == member_id,
                                      LinkCode.consumed_at.is_(None),
                                      LinkCode.invalidated_at.is_(None)))).all()
        tasks = (await session.execute(select(Task).where(Task.owner_id == member_id))).scalars().all()
        live = (await session.execute(
            select(ApprovalGrant.id).where(ApprovalGrant.owner_id == member_id,
                                           ApprovalGrant.revoked_at.is_(None)))).all()
        reasons = [(e.event_metadata or {}).get("reason") for e in (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id.in_([t.id for t in tasks])))).scalars().all()]
        return {
            "status": member.status, "revoked_at": member.revoked_at,
            "attached": len(attached), "open_codes": len(open_codes),
            "open_tasks": len([t for t in tasks if t.state not in TERMINAL_STATES]),
            "cancelled_tasks": len([t for t in tasks if t.state is TaskState.CANCELLED]),
            "live_grants": len(live), "member_revoked_events": reasons.count("member_revoked"),
        }


UNTOUCHED = {
    "status": "active", "revoked_at": None, "attached": 1, "open_codes": 1,
    "open_tasks": 1, "cancelled_tasks": 0, "live_grants": 1, "member_revoked_events": 0,
}


def revocation(session, settings, cancel_service_for=None) -> MemberRevocation:
    return MemberRevocation(
        session,
        cancel_service_for=cancel_service_for or (
            lambda member_id: MonitorRequestService(session, settings=settings, owner_id=member_id)),
    )


async def test_a_control_a_full_revocation_closes_everything(session_factory, settings) -> None:
    member_id = await linked_member_with_work(session_factory, settings)
    assert await state_of(session_factory, member_id) == UNTOUCHED
    async with session_factory() as session:
        outcome = await revocation(session, settings).revoke(DEFAULT_ORGANISATION_ID, member_id)
    assert outcome.value == "revoked"
    after = await state_of(session_factory, member_id)
    assert after["revoked_at"] is not None
    after["revoked_at"] = None
    assert after == {
        "status": "revoked", "revoked_at": None, "attached": 0, "open_codes": 0,
        "open_tasks": 0, "cancelled_tasks": 1, "live_grants": 0, "member_revoked_events": 1,
    }


async def test_a_task_left_open_fails_the_postcondition_and_nothing_is_committed(
    session_factory, settings
) -> None:
    """A cancel service that answers TERMINAL without finishing the task: only
    the postcondition count can notice, and the whole revocation must roll
    back -- membership, codes and identity included."""

    class Lying:
        async def cancel(self, task_id, reason=None):
            return TaskResult(outcome=TaskOutcome.TERMINAL, task_id=task_id, reason="terminal")

    member_id = await linked_member_with_work(session_factory, settings)
    async with session_factory() as session:
        with pytest.raises(RevocationIncomplete):
            await revocation(session, settings, lambda _: Lying()).revoke(
                DEFAULT_ORGANISATION_ID, member_id)
    assert await state_of(session_factory, member_id) == UNTOUCHED


async def test_a_grant_left_live_fails_the_postcondition_and_nothing_is_committed(
    session_factory, settings, monkeypatch
) -> None:
    """A grant revocation that reports success without writing: the
    postcondition must catch it, and the task cancellation already made in
    the same transaction must roll back with everything else."""
    from app.authorization import grants

    async def reports_revoked(self, grant_id, reason=None, now=None):
        return SimpleNamespace(id=grant_id, revoked_at=datetime.now(timezone.utc))

    monkeypatch.setattr(grants.GrantService, "revoke", reports_revoked)
    member_id = await linked_member_with_work(session_factory, settings)
    async with session_factory() as session:
        with pytest.raises(RevocationIncomplete):
            await revocation(session, settings).revoke(DEFAULT_ORGANISATION_ID, member_id)
    assert await state_of(session_factory, member_id) == UNTOUCHED
