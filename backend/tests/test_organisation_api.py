"""Stage 7A over HTTP: the organisation routes, through the real app.

`client` sends the suite's API token, which authenticates the seeded Admin of
the default organisation; `anon_client` sends nothing. Names and subjects are
synthetic.
"""

import ast
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.authorization.models import ApprovalGrant
from app.database.metadata import Base
from app.identity.constants import (
    DEFAULT_ORGANISATION_ID,
    DEFAULT_ORGANISATION_NAME,
    LEGACY_ADMIN_MEMBER_ID,
    TELEGRAM_PROVIDER,
    OrganisationStatus,
    Role,
    MemberStatus,
)
from app.identity import service as identity_service_module
from app.identity.link_codes import code_sha256
from app.identity.principal import Principal
from app.identity.models import LinkCode, Organisation, OrganisationMember, User
from app.identity.schemas import RedeemResult
from app.identity.service import MembershipService
from app.organisation import revocation as revocation_module
from app.organisation.revocation import RevocationIncomplete
from app.tasks.models import Task
from app.tasks.states import TaskState
from app.tools.schemas import RiskLevel
from tests.conftest import AUTH_HEADERS

BACKEND = Path(__file__).resolve().parents[1]
ROUTES = BACKEND / "app" / "api" / "routes" / "organisation.py"

SUBJECT = "918273645512"
MEMBER_KEYS = {"id", "display_name", "role", "status", "linked_at", "created_at", "revoked_at"}

#: (method, path, body) for each of the five routes; {member} is filled in.
ROUTES_UNDER_TEST = [
    ("GET", "/api/organisation", None),
    ("GET", "/api/organisation/members", None),
    ("POST", "/api/organisation/members", {"display_name": "Someone"}),
    ("POST", "/api/organisation/members/{member}/link-codes", {}),
    ("POST", "/api/organisation/members/{member}/revoke", {}),
]


def _message(response) -> str:
    return response.json()["error"]["message"]


def _code(response) -> str:
    return response.json()["error"]["code"]


async def _invite(client, name: str = "Asha") -> dict:
    response = await client.post("/api/organisation/members", json={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def _request(client, method: str, path: str, body, member: uuid.UUID):
    url = path.format(member=member)
    if body is None:
        return await client.request(method, url)
    return await client.request(method, url, json=body)


# --- The five routes, as the Admin -------------------------------------------


async def test_get_the_organisation(client) -> None:
    response = await client.get("/api/organisation")
    assert response.status_code == 200
    assert response.json() == {"id": str(DEFAULT_ORGANISATION_ID), "name": DEFAULT_ORGANISATION_NAME}


async def test_list_members_starts_with_the_admin(client) -> None:
    response = await client.get("/api/organisation/members")
    assert response.status_code == 200
    [admin] = response.json()["members"]
    assert set(admin) == MEMBER_KEYS
    assert admin["id"] == str(LEGACY_ADMIN_MEMBER_ID)
    assert admin["role"] == Role.ADMIN.value
    assert admin["status"] == MemberStatus.ACTIVE.value


async def test_invite_returns_the_member_and_its_code_once(client, session_factory) -> None:
    response = await client.post("/api/organisation/members", json={"display_name": "  Asha  "})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"member", "link_code"}
    assert set(body["member"]) == MEMBER_KEYS
    assert set(body["link_code"]) == {"code", "expires_at"}
    member = body["member"]
    assert member["display_name"] == "Asha"
    assert member["role"] == "member" and member["status"] == "invited"
    assert member["linked_at"] is None and member["revoked_at"] is None
    code = body["link_code"]["code"]
    assert len(code) == 16

    # Committed by the request session, and listed.
    listed = (await client.get("/api/organisation/members")).json()["members"]
    assert [m["id"] for m in listed] == [str(LEGACY_ADMIN_MEMBER_ID), member["id"]]

    # No user id and no hash anywhere in the response.
    async with session_factory() as session:
        user_id = (
            await session.execute(
                select(OrganisationMember.user_id).where(OrganisationMember.id == uuid.UUID(member["id"]))
            )
        ).scalar_one()
    text = response.text
    assert "user" not in text
    assert str(user_id) not in text and user_id.hex not in text
    assert code_sha256(code) not in text


async def test_the_code_is_never_stored(client, session_factory) -> None:
    code = (await _invite(client))["link_code"]["code"]
    found = []
    async with session_factory() as session:
        for table in Base.metadata.sorted_tables:
            for row in (await session.execute(select(table))).all():
                for value in row:
                    if value == code or (isinstance(value, str) and code in value):
                        found.append(table.name)
    assert found == []
    async with session_factory() as session:
        stored = (
            await session.execute(select(LinkCode).where(LinkCode.code_sha256 == code_sha256(code)))
        ).scalar_one()
    assert stored.consumed_at is None


@pytest.mark.parametrize(
    "body, message",
    [
        ({"display_name": "Asha", "role": "admin"}, None),
        ({"display_name": "Asha", "organisation_id": str(uuid.uuid4())}, None),
        ({}, None),
        ({"display_name": 123}, None),
        ({"display_name": ""}, "invalid_display_name"),
        ({"display_name": "   "}, "invalid_display_name"),
        ({"display_name": "x" * 101}, "invalid_display_name"),
        ({"display_name": "x" * 1001}, None),
        ({"display_name": "‮"}, "invalid_display_name"),
        ({"display_name": "Asha‮"}, "invalid_display_name"),
    ],
    ids=["extra-role", "extra-organisation", "missing", "not-a-string", "empty",
         "blank", "101-chars", "1001-chars", "rtl-override", "rtl-override-inside"],
)
async def test_invite_refuses_bad_bodies(client, session_factory, body, message) -> None:
    response = await client.post("/api/organisation/members", json=body)
    assert response.status_code == 422, response.text
    if message is not None:
        assert _message(response) == message
    async with session_factory() as session:
        count = len((await session.execute(select(OrganisationMember))).scalars().all())
    assert count == 1                                   # only the Admin; nothing committed


async def test_a_100_character_name_is_accepted(client) -> None:
    assert (await _invite(client, "x" * 100))["member"]["display_name"] == "x" * 100


async def test_a_huge_name_is_refused_before_the_service(client, monkeypatch) -> None:
    """Over the body bound, Pydantic refuses it; the service is never asked."""
    asked = []

    async def recording(self, organisation_id, display_name, now=None):
        asked.append(len(display_name))
        raise AssertionError("the service must not be reached")

    monkeypatch.setattr(MembershipService, "invite", recording)
    response = await client.post("/api/organisation/members", json={"display_name": "x" * 1001})
    assert response.status_code == 422
    assert _code(response) == "validation_error"
    assert asked == []


async def test_a_padded_name_within_the_body_bound_reaches_the_service(client) -> None:
    """The body bound is not the name rule: 1000 characters of which the
    service keeps 100 after stripping is a valid name."""
    name = " " * 450 + "y" * 100 + " " * 450
    assert (await _invite(client, name))["member"]["display_name"] == "y" * 100


async def test_issue_a_link_code(client, session_factory) -> None:
    invited = await _invite(client)
    member_id = invited["member"]["id"]
    first = invited["link_code"]["code"]

    response = await client.post(f"/api/organisation/members/{member_id}/link-codes", json={})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"code", "expires_at"}
    assert body["code"] != first
    async with session_factory() as session:
        old = (
            await session.execute(select(LinkCode).where(LinkCode.code_sha256 == code_sha256(first)))
        ).scalar_one()
    assert old.invalidated_at is not None                 # the earlier code stopped working


async def test_link_code_refusals(client) -> None:
    admin = await client.post(f"/api/organisation/members/{LEGACY_ADMIN_MEMBER_ID}/link-codes", json={})
    assert admin.status_code == 409 and _message(admin) == "admin_link_not_supported"

    unknown = await client.post(f"/api/organisation/members/{uuid.uuid4()}/link-codes", json={})
    assert unknown.status_code == 404 and _message(unknown) == "member_not_found"

    member_id = (await _invite(client))["member"]["id"]
    extra = await client.post(f"/api/organisation/members/{member_id}/link-codes", json={"ttl": 9999})
    assert extra.status_code == 422
    no_body = await client.post(f"/api/organisation/members/{member_id}/link-codes")
    assert no_body.status_code == 422

    assert (await client.post(f"/api/organisation/members/{member_id}/revoke", json={})).status_code == 200
    revoked = await client.post(f"/api/organisation/members/{member_id}/link-codes", json={})
    assert revoked.status_code == 409 and _message(revoked) == "member_revoked"


async def test_revoke_is_idempotent(client) -> None:
    member_id = (await _invite(client))["member"]["id"]

    first = await client.post(f"/api/organisation/members/{member_id}/revoke", json={})
    assert first.status_code == 200
    body = first.json()
    assert set(body) == {"member"} and set(body["member"]) == MEMBER_KEYS
    assert body["member"]["id"] == member_id
    assert body["member"]["status"] == "revoked"
    assert body["member"]["revoked_at"] is not None

    second = await client.post(f"/api/organisation/members/{member_id}/revoke", json={})
    assert second.status_code == 200
    assert second.json() == body


async def test_revoke_refusals(client) -> None:
    admin = await client.post(f"/api/organisation/members/{LEGACY_ADMIN_MEMBER_ID}/revoke", json={})
    assert admin.status_code == 409 and _message(admin) == "cannot_revoke_admin"

    unknown = await client.post(f"/api/organisation/members/{uuid.uuid4()}/revoke", json={})
    assert unknown.status_code == 404 and _message(unknown) == "member_not_found"

    member_id = (await _invite(client))["member"]["id"]
    extra = await client.post(f"/api/organisation/members/{member_id}/revoke", json={"force": True})
    assert extra.status_code == 422
    listed = (await client.get("/api/organisation/members")).json()["members"]
    assert [m["status"] for m in listed] == ["active", "invited"]


async def test_revoke_cancels_the_members_work_over_http(client, session_factory) -> None:
    member_id = uuid.UUID((await _invite(client))["member"]["id"])
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        task = Task(owner_id=member_id, objective="Fixture task", state=TaskState.QUEUED)
        grant = ApprovalGrant(
            owner_id=member_id, capability="gmail_list_messages", risk_level=RiskLevel.HIGH,
            created_at=now - timedelta(hours=1), expires_at=now + timedelta(days=1),
        )
        session.add_all([task, grant])
        await session.commit()
        task_id, grant_id = task.id, grant.id

    response = await client.post(f"/api/organisation/members/{member_id}/revoke", json={})
    assert response.status_code == 200

    async with session_factory() as session:
        assert (await session.execute(select(Task.state).where(Task.id == task_id))).scalar_one() is TaskState.CANCELLED
        revoked = (await session.execute(select(ApprovalGrant).where(ApprovalGrant.id == grant_id))).scalar_one()
    assert revoked.revoked_at is not None and revoked.revoked_reason == "member_revoked"


async def test_a_failed_revocation_is_a_500_with_nothing_changed(client, monkeypatch) -> None:
    member_id = (await _invite(client))["member"]["id"]

    async def incomplete(self, organisation_id, member_id):
        raise RevocationIncomplete("task_not_cancelled")

    monkeypatch.setattr(revocation_module.MemberRevocation, "revoke", incomplete)
    response = await client.post(f"/api/organisation/members/{member_id}/revoke", json={})
    assert response.status_code == 500
    assert _message(response) == "revocation_failed"
    listed = (await client.get("/api/organisation/members")).json()["members"]
    assert listed[1]["status"] == "invited"


async def test_revoke_answers_for_a_member_beyond_the_first_listing(client) -> None:
    """The re-read is a lookup, not a scan of one bounded listing."""
    invited = [await _invite(client, f"Member {index}") for index in range(201)]
    last = invited[-1]["member"]["id"]
    listed = (await client.get("/api/organisation/members", params={"limit": 200})).json()["members"]
    assert last not in {m["id"] for m in listed}          # not among the first 200

    response = await client.post(f"/api/organisation/members/{last}/revoke", json={})

    assert response.status_code == 200, response.text
    member = response.json()["member"]
    assert member["id"] == last
    assert member["display_name"] == "Member 200"
    assert member["status"] == "revoked" and member["revoked_at"] is not None


async def test_a_revoked_member_that_cannot_be_read_back_is_a_500(client, monkeypatch) -> None:
    """Unreachable in practice; never answered as a success it cannot show."""
    member_id = (await _invite(client))["member"]["id"]

    async def missing(self, organisation_id, member_id):
        return None

    monkeypatch.setattr(MembershipService, "get_member", missing)
    response = await client.post(f"/api/organisation/members/{member_id}/revoke", json={})
    assert response.status_code == 500
    assert _message(response) == "revocation_failed"


async def test_another_organisations_member_is_not_found(client, session_factory) -> None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        organisation = Organisation(name="Other organisation", status="active")
        user = User(display_name="Other person")
        session.add_all([organisation, user])
        await session.flush()
        member = OrganisationMember(
            organisation_id=organisation.id, user_id=user.id, role="member",
            status="active", activated_at=now,
        )
        session.add(member)
        await session.commit()
        other = member.id

    for path in ("link-codes", "revoke"):
        response = await client.post(f"/api/organisation/members/{other}/{path}", json={})
        assert response.status_code == 404 and _message(response) == "member_not_found", path
    listed = (await client.get("/api/organisation/members")).json()["members"]
    assert str(other) not in {m["id"] for m in listed}
    async with session_factory() as session:
        assert (
            await session.execute(select(OrganisationMember.status).where(OrganisationMember.id == other))
        ).scalar_one() == "active"


# --- Codes are durable before they are shown --------------------------------------------

SENTINEL = "sentinel-commit-failure-7a"


@pytest.fixture
def issued_codes(monkeypatch):
    """Every link code the service generates during the test."""
    real = identity_service_module.generate_code
    codes = []

    def recording() -> str:
        code = real()
        codes.append(code)
        return code

    monkeypatch.setattr(identity_service_module, "generate_code", recording)
    return codes


def _fail_next_commit(monkeypatch) -> list:
    """Make the next `AsyncSession.commit` anywhere raise, once."""
    real = AsyncSession.commit
    failed = []

    async def commit(self):
        if not failed:
            failed.append("commit")
            raise OperationalError(f"COMMIT -- {SENTINEL}", {"p": SENTINEL}, Exception(SENTINEL))
        return await real(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    return failed


async def test_invite_with_a_failing_commit_is_a_503_and_shows_no_code(
    client, session_factory, issued_codes, monkeypatch, caplog, handling
) -> None:
    caplog.set_level(logging.INFO)
    failed = _fail_next_commit(monkeypatch)

    response = await client.post("/api/organisation/members", json={"display_name": "Asha"})

    assert failed == ["commit"]
    assert response.status_code == 503
    assert _code(response) == "database_error"
    assert len(issued_codes) == 1
    assert issued_codes[0] not in response.text and "link_code" not in response.text
    assert SENTINEL not in response.text
    async with session_factory() as session:
        members = (await session.execute(select(OrganisationMember.id))).scalars().all()
        codes = (await session.execute(select(LinkCode.id))).scalars().all()
    assert members == [LEGACY_ADMIN_MEMBER_ID]
    assert codes == []
    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert SENTINEL not in text and issued_codes[0] not in text, record.name
    logged = [r for r in caplog.records if r.getMessage() == "Link code commit failed"]
    assert [r.category for r in logged] == ["OperationalError"]
    assert logged[0].exc_info is None
    assert handling == [("Link code commit failed", None)]


async def test_link_code_with_a_failing_commit_is_a_503_and_keeps_the_old_code(
    client, session_factory, issued_codes, monkeypatch
) -> None:
    invited = await _invite(client)
    member_id = invited["member"]["id"]
    previous = invited["link_code"]["code"]
    failed = _fail_next_commit(monkeypatch)

    response = await client.post(f"/api/organisation/members/{member_id}/link-codes", json={})

    assert failed == ["commit"]
    assert response.status_code == 503
    assert _code(response) == "database_error"
    assert len(issued_codes) == 2 and issued_codes[0] == previous
    assert issued_codes[1] not in response.text and SENTINEL not in response.text
    async with session_factory() as session:
        codes = (await session.execute(select(LinkCode))).scalars().all()
    assert [c.code_sha256 for c in codes] == [code_sha256(previous)]
    assert codes[0].invalidated_at is None and codes[0].consumed_at is None


LEAKY_NAME = "Sentinel Person"
LEAKS = (LEAKY_NAME, "boom", "parameters")


def _leaky_error() -> OperationalError:
    """A driver error whose text quotes a bound parameter, as a real one does."""
    return OperationalError("INSERT", {"display_name": LEAKY_NAME}, Exception("boom"))


class _HandlingRecorder(logging.Handler):
    """Records, for each route log line, which exception (if any) was being
    handled at the moment it was emitted."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seen = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append((record.getMessage(), sys.exc_info()[0]))


@pytest.fixture
def handling(monkeypatch):
    route_logger = logging.getLogger("app.api.routes.organisation")
    recorder = _HandlingRecorder()
    route_logger.addHandler(recorder)
    monkeypatch.setattr(route_logger, "level", logging.DEBUG)
    yield recorder.seen
    route_logger.removeHandler(recorder)


def _assert_no_leak(caplog, codes) -> None:
    for record in caplog.records:
        text = " ".join((record.getMessage(), repr(record.__dict__), str(record.exc_text or "")))
        for leak in LEAKS + tuple(codes):
            assert leak not in text, (record.name, record.getMessage(), leak)
        assert record.exc_info is None, (record.name, record.getMessage())


async def test_invite_with_a_database_error_is_a_503_that_leaks_nothing(
    client, session_factory, issued_codes, monkeypatch, caplog, handling
) -> None:
    """The service did its writes (user, member, code) and then the driver
    failed: all of it is rolled back, and nothing of the error is shown."""
    # INFO, the suite's level: at DEBUG the SQLite driver itself logs every
    # statement with its parameters, which is not what is under test.
    caplog.set_level(logging.INFO)
    real_invite = MembershipService.invite

    async def failing_invite(self, organisation_id, display_name, now=None):
        await real_invite(self, organisation_id, display_name, now=now)
        raise _leaky_error()

    monkeypatch.setattr(MembershipService, "invite", failing_invite)
    response = await client.post("/api/organisation/members", json={"display_name": LEAKY_NAME})

    assert response.status_code == 503
    assert _code(response) == "database_error"
    assert len(issued_codes) == 1
    for leak in LEAKS + (issued_codes[0], "link_code"):
        assert leak not in response.text, leak
    async with session_factory() as session:
        assert (await session.execute(select(OrganisationMember.id))).scalars().all() == [LEGACY_ADMIN_MEMBER_ID]
        assert (await session.execute(select(User.display_name))).scalars().all() == ["Admin"]
        assert (await session.execute(select(LinkCode.id))).scalars().all() == []
    _assert_no_leak(caplog, issued_codes)
    logged = [r for r in caplog.records if r.getMessage() == "Invitation failed"]
    assert [r.category for r in logged] == ["OperationalError"]
    # Logged (and rolled back) with no exception being handled.
    assert handling == [("Invitation failed", None)]


async def test_link_code_with_a_database_error_is_a_503_that_leaks_nothing(
    client, session_factory, issued_codes, monkeypatch, caplog, handling
) -> None:
    """The service invalidated the old code and flushed a new one, then the
    driver failed: the old code is still the open one."""
    invited = await _invite(client)
    member_id = invited["member"]["id"]
    previous = invited["link_code"]["code"]
    # INFO, the suite's level: at DEBUG the SQLite driver itself logs every
    # statement with its parameters, which is not what is under test.
    caplog.set_level(logging.INFO)
    caplog.clear()
    real_issue = MembershipService.issue_link_code

    async def failing_issue(self, organisation_id, member_id, now=None):
        await real_issue(self, organisation_id, member_id, now=now)
        raise _leaky_error()

    monkeypatch.setattr(MembershipService, "issue_link_code", failing_issue)
    response = await client.post(f"/api/organisation/members/{member_id}/link-codes", json={})

    assert response.status_code == 503
    assert _code(response) == "database_error"
    assert len(issued_codes) == 2 and issued_codes[0] == previous
    for leak in LEAKS + (issued_codes[1],):
        assert leak not in response.text, leak
    async with session_factory() as session:
        codes = (await session.execute(select(LinkCode))).scalars().all()
    assert [c.code_sha256 for c in codes] == [code_sha256(previous)]
    assert codes[0].invalidated_at is None and codes[0].consumed_at is None
    _assert_no_leak(caplog, issued_codes)
    logged = [r for r in caplog.records if r.getMessage() == "Link code issue failed"]
    assert [r.category for r in logged] == ["OperationalError"]
    assert handling == [("Link code issue failed", None)]


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_a_failed_code_commit_rolls_back_itself(session_factory, rollback_fails) -> None:
    """The route's own rollback, not only the session dependency's: nothing
    of the failed transaction is left open, and a failing rollback does not
    replace the 503."""
    from app.api.routes.organisation import _commit_before_answering
    from app.core.errors import DatabaseError

    async with session_factory() as session:
        session.add(User(display_name="Pending person"))
        await session.flush()
        assert session.in_transaction()

        async def failing_commit():
            raise OperationalError(f"COMMIT -- {SENTINEL}", {}, Exception(SENTINEL))

        real_rollback = session.rollback
        rollbacks = []

        async def rollback():
            rollbacks.append("rollback")
            if rollback_fails:
                raise OperationalError(f"ROLLBACK -- {SENTINEL}", {}, Exception(SENTINEL))
            await real_rollback()

        session.commit = failing_commit
        session.rollback = rollback
        with pytest.raises(DatabaseError) as raised:
            await _commit_before_answering(session)
        assert rollbacks == ["rollback"]
        assert raised.value.__suppress_context__ and SENTINEL not in str(raised.value)
        if not rollback_fails:
            assert not session.in_transaction()
        del session.commit, session.rollback
    async with session_factory() as session:
        names = (await session.execute(select(User.display_name))).scalars().all()
    assert "Pending person" not in names


# --- A member who is not an Admin --------------------------------------------------------


async def test_a_non_admin_principal_is_refused_on_every_route(
    session_factory, fake_provider, settings
) -> None:
    """`get_principal` is replaced with a Member-role principal; the token
    still authenticates through the real seam."""
    from app.api.deps import get_principal
    from app.core.config import get_settings
    from app.database.session import get_db_session, get_session_factory
    from app.llm.factory import get_llm_provider
    from app.main import create_app

    app = create_app()

    async def override_session():
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()

    member = Principal(member_id=uuid.uuid4(), organisation_id=DEFAULT_ORGANISATION_ID, role=Role.MEMBER)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_principal] = lambda: member
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS
        ) as http:
            for method, path, body in ROUTES_UNDER_TEST:
                response = await _request(http, method, path, body, LEGACY_ADMIN_MEMBER_ID)
                assert response.status_code == 403, (path, response.text)
                assert _code(response) == "admin_required", path
    finally:
        app.dependency_overrides.clear()
    async with session_factory() as session:
        assert (await session.execute(select(OrganisationMember.id))).scalars().all() == [LEGACY_ADMIN_MEMBER_ID]
        assert (await session.execute(select(LinkCode.id))).scalars().all() == []


# --- Listing --------------------------------------------------------------------------


@pytest.mark.parametrize("limit, expected", [(0, 422), (201, 422), (-1, 422), (1, 200), (200, 200)])
async def test_list_limit_bounds(client, limit, expected) -> None:
    response = await client.get("/api/organisation/members", params={"limit": limit})
    assert response.status_code == expected


async def test_list_limit_is_applied(client) -> None:
    await _invite(client, "One")
    await _invite(client, "Two")
    response = await client.get("/api/organisation/members", params={"limit": 2})
    assert [m["display_name"] for m in response.json()["members"]] == ["Admin", "One"]


async def test_linked_at_is_null_until_linked(client, session_factory, settings) -> None:
    invited = await _invite(client)
    member_id = invited["member"]["id"]
    listed = (await client.get("/api/organisation/members")).json()["members"]
    assert listed[1]["id"] == member_id and listed[1]["linked_at"] is None

    async with session_factory() as session:
        service = MembershipService(session, settings.model_copy(update={"TELEGRAM_ALLOWED_CHAT_ID": ""}))
        result = await service.redeem_link_code(invited["link_code"]["code"], TELEGRAM_PROVIDER, SUBJECT)
        assert result is RedeemResult.LINKED
        await session.commit()

    response = await client.get("/api/organisation/members")
    linked = response.json()["members"][1]
    assert linked["status"] == "active" and linked["linked_at"] is not None
    assert SUBJECT not in response.text


# --- Authentication and membership ------------------------------------------------------


@pytest.mark.parametrize("method, path, body", ROUTES_UNDER_TEST)
async def test_every_route_requires_the_token(anon_client, session_factory, method, path, body) -> None:
    response = await _request(anon_client, method, path, body, uuid.uuid4())
    assert response.status_code == 401
    assert _code(response) == "authentication_required"
    async with session_factory() as session:
        assert len((await session.execute(select(OrganisationMember))).scalars().all()) == 1


@pytest.mark.parametrize("method, path, body", ROUTES_UNDER_TEST)
async def test_a_suspended_organisation_is_refused(client, session_factory, method, path, body) -> None:
    member_id = (await _invite(client))["member"]["id"]
    async with session_factory() as session:
        await session.execute(
            update(Organisation)
            .where(Organisation.id == DEFAULT_ORGANISATION_ID)
            .values(status=OrganisationStatus.SUSPENDED.value)
        )
        await session.commit()

    response = await _request(client, method, path, body, member_id)

    assert response.status_code == 403
    assert _code(response) == "membership_inactive"
    async with session_factory() as session:
        statuses = (await session.execute(select(OrganisationMember.status))).scalars().all()
        codes = (await session.execute(select(LinkCode))).scalars().all()
    assert sorted(statuses) == ["active", "invited"]
    assert len(codes) == 1 and codes[0].invalidated_at is None


# --- Structure ----------------------------------------------------------------------------


def _route_functions(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
            for d in node.decorator_list
        ):
            yield node


def test_every_route_requires_an_admin() -> None:
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    routes = list(_route_functions(tree))
    assert sorted(f.name for f in routes) == [
        "get_organisation", "invite_member", "issue_link_code", "list_members", "revoke_member",
    ]
    for function in routes:
        annotations = [
            a.annotation.id for a in function.args.args + function.args.kwonlyargs
            if isinstance(a.annotation, ast.Name)
        ]
        assert "AdminPrincipal" in annotations, function.name
        # The organisation is the principal's, never a parameter.
        names = {a.arg for a in function.args.args + function.args.kwonlyargs}
        assert "organisation_id" not in names, function.name
        assert "principal.organisation_id" in ast.unparse(function), function.name


def _imported_modules(tree: ast.Module) -> set:
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_the_routes_do_not_reach_grants_the_task_layer_or_the_organisation_package() -> None:
    source = ROUTES.read_text(encoding="utf-8")
    for forbidden in ("GrantService", "ApprovalGrant", "approval_grants", "TaskService",
                      "MonitorRequestService"):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    modules = _imported_modules(tree)
    # Dependency direction: only `app.api.deps` reaches `app.organisation`.
    reached = {
        m for m in modules
        if m == "app.organisation" or m.startswith((
            "app.organisation.", "app.tasks", "app.authorization", "app.execution",
        ))
    }
    assert reached == set(), reached
    imported_from_deps = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.api.deps"
        for alias in node.names
    }
    assert "RevocationIncomplete" in imported_from_deps


def _calls(dependant) -> set:
    """Every callable in a route's dependency tree, at any depth."""
    found = set()
    stack = list(dependant.dependencies)
    while stack:
        current = stack.pop()
        found.add(current.call)
        stack.extend(current.dependencies)
    return found


def test_every_registered_route_depends_on_the_seam_and_an_admin() -> None:
    """Found through the application, not the decorators: whatever is mounted
    under /api/organisation authenticates and requires an Admin."""
    from app.api.deps import get_owner_id, require_admin
    from app.main import create_app

    app = create_app()
    paths = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/organisation"):
            calls = _calls(route.dependant)
            paths.setdefault(path, set()).update(getattr(route, "methods", set()))
            assert get_owner_id in calls, path
            assert require_admin in calls, path
    assert paths == {
        "/api/organisation": {"GET"},
        "/api/organisation/members": {"GET", "POST"},
        "/api/organisation/members/{member_id}/link-codes": {"POST"},
        "/api/organisation/members/{member_id}/revoke": {"POST"},
    }
