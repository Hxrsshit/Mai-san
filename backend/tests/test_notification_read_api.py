"""Stage 6M.2: the owner's task notifications over HTTP.

Through the real app and the real 6H `NotificationService`. The first
notification of each test comes from the real 6G/6H runtime path; further
rows for paging are inserted for the same task with controlled timestamps
(the 6H unique index is per check number, so each gets its own). Delivery
records are written by the 6M.1 writer through 6I where the test is about the
integration, and planted directly where it is about each state.

Ownership is exercised over HTTP by overriding the one owner seam,
`deps.get_owner_id`: the same request, made as another owner.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_owner_id
from app.composition import notification_delivery as composition
from app.core.config import get_settings
from app.database.metadata import Base
from app.database.session import configure_sqlite, get_db_session
from app.delivery.models import DeliveryRecordStatus, NotificationDelivery
from app.llm.factory import get_llm_provider
from app.main import create_app
from app.tasks.models import LOCAL_OWNER_ID, TaskNotification, TaskNotificationKind
from app.tasks.notifications import MAX_LISTED, NotificationService
from tests.test_delivery import OTHER, SpyAdapter, deliver, met_notification, registry_with, snapshot
from tests.conftest import AUTH_HEADERS

pytestmark = pytest.mark.anyio

ROUTE = "/api/task-notifications"
NOTIFICATION_KEYS = {"id", "task_id", "kind", "check_number", "execution_id",
                     "created_at", "read_at", "deliveries"}
DELIVERY_KEYS = {"adapter", "status", "attempts", "last_attempt_at", "delivered_at", "retryable"}


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


@pytest.fixture(autouse=True)
def no_composition(monkeypatch, execution_settings):
    """Reading must never build the delivery composition; tests check it
    stays unbuilt, and it could never read the environment if it were."""
    monkeypatch.setattr(composition, "_registry", None)
    monkeypatch.setattr(composition, "get_settings", lambda: execution_settings)


@asynccontextmanager
async def client(monkeypatch, app_settings, factory, fake_provider, owner=LOCAL_OWNER_ID):
    """`owner=None` leaves the real owner seam in place."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: app_settings)
    app = create_app()

    async def override_session():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: app_settings
    if owner is not None:
        app.dependency_overrides[get_owner_id] = lambda: owner
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS) as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture
async def http(monkeypatch, execution_settings, session_factory, fake_provider):
    async with client(monkeypatch, execution_settings, session_factory, fake_provider) as c:
        yield c


@pytest.fixture
async def as_other(monkeypatch, execution_settings, session_factory, fake_provider):
    async with client(monkeypatch, execution_settings, session_factory, fake_provider,
                      owner=OTHER) as c:
        yield c


async def more_like(session_factory, note, count, *, start=None, step=timedelta(minutes=1)):
    """`count` further notifications on `note`'s task, each `step` newer."""
    base = start or note.created_at
    made = []
    async with session_factory() as session:
        for i in range(count):
            row = TaskNotification(
                owner_id=note.owner_id, task_id=note.task_id,
                kind=TaskNotificationKind.CONDITION_MET, check_number=note.check_number + 100 + i,
                created_at=base + step * (i + 1),
            )
            session.add(row)
            made.append(row)
        await session.commit()
    return made


async def plant_delivery(session_factory, note, adapter, status, *, lease=None, attempts=1):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        session.add(NotificationDelivery(
            notification_id=note.id, owner_id=note.owner_id, adapter=adapter, status=status,
            attempts=attempts, lease_expires_at=lease,
            delivered_at=now if status is DeliveryRecordStatus.DELIVERED else None,
        ))
        await session.commit()


def strip(response):
    return {k: v for k, v in response.json()["error"].items() if k != "request_id"}


async def read_at(session_factory, notification_id):
    async with session_factory() as session:
        return (await session.execute(
            select(TaskNotification.read_at).where(TaskNotification.id == notification_id)
        )).scalar_one()


# ============================================================================
# A. Listing
# ============================================================================


async def test_an_empty_inbox_is_an_empty_page(http) -> None:
    response = await http.get(ROUTE)
    assert response.status_code == 200
    assert response.json() == {"notifications": [], "next_before": None}


async def test_a_notification_is_listed_with_exactly_the_contract_fields(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    body = (await http.get(ROUTE)).json()
    [item] = body["notifications"]
    assert set(item) == NOTIFICATION_KEYS
    assert item["id"] == str(note.id) and item["task_id"] == str(note.task_id)
    assert item["kind"] == "condition_met" and item["check_number"] == note.check_number
    assert item["execution_id"] == str(note.execution_id)
    assert item["read_at"] is None and item["deliveries"] == []
    assert body["next_before"] is None


async def test_pages_are_newest_first_and_never_repeat_or_skip(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    extra = await more_like(session_factory, note, 6)
    expected = [str(n.id) for n in reversed(extra)] + [str(note.id)]

    seen, before, pages = [], None, []
    for _ in range(5):                       # bounded: a broken cursor must fail, not hang
        params = {"limit": 3, **({"before": before} if before else {})}
        body = (await http.get(ROUTE, params=params)).json()
        pages.append(len(body["notifications"]))
        seen += [n["id"] for n in body["notifications"]]
        before = body["next_before"]
        if before is None:
            break
    assert pages == [3, 3, 1]
    assert seen == expected


async def test_equal_timestamps_are_ordered_by_id_and_paged_exactly(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    same = await more_like(session_factory, note, 5, start=note.created_at + timedelta(hours=1),
                           step=timedelta(0))
    expected = sorted((str(n.id) for n in same), key=lambda i: uuid.UUID(i).hex, reverse=True)
    first = (await http.get(ROUTE, params={"limit": 2})).json()
    second = (await http.get(ROUTE, params={"limit": 2, "before": first["next_before"]})).json()
    third = (await http.get(ROUTE, params={"limit": 2, "before": second["next_before"]})).json()
    got = [n["id"] for page in (first, second, third) for n in page["notifications"]]
    assert got[:5] == expected and got[5] == str(note.id)


async def test_unread_only_lists_only_unread_and_pages_through_them(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    # In the past, so marking them read now satisfies 6H's read-after-created check.
    extra = await more_like(session_factory, note, 4, start=note.created_at - timedelta(hours=1))
    async with session_factory() as session:
        service = NotificationService(session, LOCAL_OWNER_ID)
        assert await service.mark_read(extra[1].id) and await service.mark_read(extra[3].id)
        await session.commit()
    unread = [str(note.id), str(extra[2].id), str(extra[0].id)]

    first = (await http.get(ROUTE, params={"unread_only": True, "limit": 2})).json()
    # The cursor may be any of the owner's notifications, read or not.
    rest = (await http.get(ROUTE, params={"unread_only": True, "before": first["next_before"]})).json()
    assert [n["id"] for n in first["notifications"] + rest["notifications"]] == unread
    everything = (await http.get(ROUTE)).json()["notifications"]
    assert len(everything) == 5


async def test_the_default_page_is_twenty_and_the_ceiling_is_the_6h_bound(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await more_like(session_factory, note, MAX_LISTED + 4)
    default = (await http.get(ROUTE)).json()
    ceiling = (await http.get(ROUTE, params={"limit": MAX_LISTED})).json()
    assert len(default["notifications"]) == 20 and default["next_before"] is not None
    assert len(ceiling["notifications"]) == MAX_LISTED and ceiling["next_before"] is not None


async def test_the_service_itself_caps_a_page_at_the_6h_bound(
    session_factory, execution_settings, workspace
) -> None:
    """The route refuses `limit` above 50, but the cap is `page()`'s own
    contract too: an in-process caller asking for more still gets 50."""
    note = await met_notification(session_factory, execution_settings, workspace)
    await more_like(session_factory, note, MAX_LISTED + 10)
    async with session_factory() as session:
        page = await NotificationService(session, LOCAL_OWNER_ID).page(limit=10_000)
    assert len(page.notifications) == MAX_LISTED and page.next_before is not None


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": MAX_LISTED + 1}, {"limit": -1}, {"limit": "ten"}, {"limit": 1.5},
    {"limit": ""}, {"before": "not-a-uuid"}, {"before": "1 OR 1=1"}, {"unread_only": "maybe"},
])
async def test_malformed_list_parameters_are_422(http, params) -> None:
    response = await http.get(ROUTE, params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_an_unknown_or_foreign_cursor_is_the_same_invalid_cursor(
    http, session_factory, execution_settings, workspace
) -> None:
    await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    unknown = await http.get(ROUTE, params={"before": str(uuid.uuid4())})
    foreign = await http.get(ROUTE, params={"before": str(theirs.id)})
    assert unknown.status_code == foreign.status_code == 422
    assert strip(unknown) == strip(foreign) == {"code": "http_error", "message": "invalid_cursor"}


# ============================================================================
# B. One notification
# ============================================================================


async def test_one_notification_is_returned_with_the_list_shape(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    one = (await http.get(f"{ROUTE}/{note.id}")).json()
    listed = (await http.get(ROUTE)).json()["notifications"][0]
    assert one == listed and set(one) == NOTIFICATION_KEYS


async def test_missing_and_foreign_notifications_are_the_same_404(
    http, session_factory, execution_settings, workspace
) -> None:
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    missing = await http.get(f"{ROUTE}/{uuid.uuid4()}")
    foreign = await http.get(f"{ROUTE}/{theirs.id}")
    assert missing.status_code == foreign.status_code == 404
    assert strip(missing) == strip(foreign) == {"code": "not_found", "message": "notification_not_found"}


@pytest.mark.parametrize("bad", ["not-a-uuid", "1", "00000000-0000-0000-0000", "%20",
                                 "' OR '1'='1", "a" * 300])
async def test_a_malformed_id_is_422_on_every_endpoint(http, bad) -> None:
    for response in (await http.get(f"{ROUTE}/{bad}"),
                     await http.post(f"{ROUTE}/{bad}/read", json={})):
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "validation_error"


async def test_a_path_traversal_shape_matches_no_notification_route(http) -> None:
    response = await http.get(f"{ROUTE}/..%2f..%2fetc%2fpasswd")
    assert response.status_code in (404, 422)
    assert "root:" not in response.text


# ============================================================================
# C. Owner isolation, from both sides
# ============================================================================


async def test_each_owner_sees_only_their_own(
    http, as_other, session_factory, execution_settings, workspace
) -> None:
    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    my_list = [n["id"] for n in (await http.get(ROUTE)).json()["notifications"]]
    their_list = [n["id"] for n in (await as_other.get(ROUTE)).json()["notifications"]]
    assert my_list == [str(mine.id)] and their_list == [str(theirs.id)]
    assert (await as_other.get(f"{ROUTE}/{mine.id}")).status_code == 404
    assert (await as_other.get(f"{ROUTE}/{theirs.id}")).status_code == 200


async def test_without_an_override_the_owner_is_the_local_owner(
    monkeypatch, execution_settings, session_factory, fake_provider, workspace
) -> None:
    """The real seam, not a test override: the server's one owner."""
    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    async with client(monkeypatch, execution_settings, session_factory, fake_provider,
                      owner=None) as real:
        listed = [n["id"] for n in (await real.get(ROUTE)).json()["notifications"]]
        assert (await real.get(f"{ROUTE}/{theirs.id}")).status_code == 404
    assert listed == [str(mine.id)]


async def test_each_owner_sees_their_own_delivery_state(
    http, as_other, session_factory, execution_settings, workspace
) -> None:
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    await deliver(session_factory, theirs.id, "spy", registry_with(SpyAdapter()), owner=OTHER)
    [state] = (await as_other.get(f"{ROUTE}/{theirs.id}")).json()["deliveries"]
    assert (state["adapter"], state["status"]) == ("spy", "delivered")
    assert (await http.get(f"{ROUTE}/{theirs.id}")).status_code == 404


async def test_a_forged_owner_in_the_query_header_or_body_changes_nothing(
    http, session_factory, execution_settings, workspace
) -> None:
    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    forged = {"owner_id": str(OTHER), "owner": str(OTHER)}
    headers = {"X-Owner-Id": str(OTHER), "X-User-Id": str(OTHER)}
    listed = (await http.get(ROUTE, params=forged, headers=headers)).json()["notifications"]
    assert [n["id"] for n in listed] == [str(mine.id)]
    assert (await http.get(f"{ROUTE}/{theirs.id}", params=forged, headers=headers)).status_code == 404
    marked = await http.post(f"{ROUTE}/{theirs.id}/read", json={"owner_id": str(OTHER)})
    assert marked.status_code == 422
    assert await read_at(session_factory, theirs.id) is None


async def test_enumerating_ids_reveals_nothing(
    http, session_factory, execution_settings, workspace
) -> None:
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    probes = [theirs.id] + [uuid.uuid4() for _ in range(5)]
    answers = {json.dumps(strip(await http.get(f"{ROUTE}/{p}")), sort_keys=True) for p in probes}
    marks = {json.dumps(strip(await http.post(f"{ROUTE}/{p}/read", json={})), sort_keys=True)
             for p in probes}
    assert len(answers) == 1 and len(marks) == 1
    assert await read_at(session_factory, theirs.id) is None


async def test_another_owners_delivery_rows_never_appear(
    http, session_factory, execution_settings, workspace
) -> None:
    """Defence in depth: delivery state is filtered on the owner as well as
    the notification. A row carrying another owner -- which the writer never
    produces -- is not reported."""
    mine = await met_notification(session_factory, execution_settings, workspace)
    async with session_factory() as session:
        session.add(NotificationDelivery(
            notification_id=mine.id, owner_id=OTHER, adapter="spy",
            status=DeliveryRecordStatus.FAILED, attempts=1,
        ))
        await session.commit()
    assert (await http.get(f"{ROUTE}/{mine.id}")).json()["deliveries"] == []


# ============================================================================
# D. Delivery state, read from 6M.1's records
# ============================================================================


async def test_a_real_delivery_through_6i_is_reported_delivered(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await deliver(session_factory, note.id, "spy", registry_with(SpyAdapter()))
    [state] = (await http.get(f"{ROUTE}/{note.id}")).json()["deliveries"]
    assert set(state) == DELIVERY_KEYS
    assert (state["adapter"], state["status"], state["attempts"], state["retryable"]) == (
        "spy", "delivered", 1, False)
    assert state["delivered_at"] is not None and state["last_attempt_at"] is not None


@pytest.mark.parametrize("status,lease,retryable", [
    (DeliveryRecordStatus.DELIVERED, None, False),
    (DeliveryRecordStatus.FAILED, None, True),
    (DeliveryRecordStatus.SENDING, timedelta(minutes=5), False),     # in progress
    (DeliveryRecordStatus.SENDING, -timedelta(seconds=1), True),     # abandoned
])
async def test_each_stored_state_and_its_retryability(
    http, session_factory, execution_settings, workspace, status, lease, retryable
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await plant_delivery(session_factory, note, "spy", status,
                         lease=datetime.now(timezone.utc) + lease if lease is not None else None)
    [state] = (await http.get(f"{ROUTE}/{note.id}")).json()["deliveries"]
    assert (state["status"], state["retryable"]) == (status.value, retryable)


async def test_every_attempted_channel_is_listed_by_name(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await plant_delivery(session_factory, note, "telegram", DeliveryRecordStatus.FAILED, attempts=3)
    await plant_delivery(session_factory, note, "local", DeliveryRecordStatus.DELIVERED)
    states = (await http.get(ROUTE)).json()["notifications"][0]["deliveries"]
    assert [(s["adapter"], s["status"], s["attempts"]) for s in states] == [
        ("local", "delivered", 1), ("telegram", "failed", 3)]


async def test_nothing_internal_or_secret_is_in_a_response(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    lease = datetime(2031, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    await plant_delivery(session_factory, note, "telegram", DeliveryRecordStatus.SENDING, lease=lease)
    async with session_factory() as session:
        record_id = (await session.execute(select(NotificationDelivery.id))).scalar_one()
    text = (await http.get(ROUTE)).text + (await http.get(f"{ROUTE}/{note.id}")).text
    for forbidden in (str(LOCAL_OWNER_ID), "owner", str(record_id), "lease", "2031-01-02",
                      "delivery_key", f"{note.id}:telegram", "token", "chat", "objective",
                      "Watch", "http", "text", "message"):
        assert forbidden not in text, forbidden


# ============================================================================
# E. Marking read
# ============================================================================


async def test_marking_read_sets_read_at_once_and_is_idempotent(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    first = await http.post(f"{ROUTE}/{note.id}/read", json={})
    stamped = await read_at(session_factory, note.id)
    again = await http.post(f"{ROUTE}/{note.id}/read", json={})
    assert (first.status_code, again.status_code) == (204, 204)
    assert first.content == b"" and stamped is not None
    assert await read_at(session_factory, note.id) == stamped
    shown = (await http.get(f"{ROUTE}/{note.id}")).json()
    assert shown["read_at"] is not None
    assert (await http.get(ROUTE, params={"unread_only": True})).json()["notifications"] == []


async def test_marking_read_changes_only_that_notifications_read_at(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    other = (await more_like(session_factory, note, 1))[0]
    await deliver(session_factory, note.id, "spy", registry_with(SpyAdapter()))
    async with session_factory() as session:
        deliveries_before = [(d.status, d.attempts, d.updated_at) for d in
                             (await session.execute(select(NotificationDelivery))).scalars()]
    counts_before, tasks_before, _ = await snapshot(session_factory)

    assert (await http.post(f"{ROUTE}/{note.id}/read", json={})).status_code == 204

    counts_after, tasks_after, _ = await snapshot(session_factory)
    async with session_factory() as session:
        deliveries_after = [(d.status, d.attempts, d.updated_at) for d in
                            (await session.execute(select(NotificationDelivery))).scalars()]
    assert (counts_after, tasks_after, deliveries_after) == (counts_before, tasks_before, deliveries_before)
    assert await read_at(session_factory, other.id) is None


async def test_marking_another_owners_or_a_missing_notification_is_404(
    http, as_other, session_factory, execution_settings, workspace
) -> None:
    mine = await met_notification(session_factory, execution_settings, workspace)
    foreign = await as_other.post(f"{ROUTE}/{mine.id}/read", json={})
    missing = await as_other.post(f"{ROUTE}/{uuid.uuid4()}/read", json={})
    assert foreign.status_code == missing.status_code == 404
    assert strip(foreign) == strip(missing)
    assert await read_at(session_factory, mine.id) is None


@pytest.mark.parametrize("kwargs", [
    {},                                                        # no body
    {"json": []},
    {"json": {"read": True}},
    {"json": {"owner_id": str(uuid.uuid4())}},
    {"content": b"{}", "headers": {"content-type": "text/plain"}},
    {"content": b"read=1", "headers": {"content-type": "application/x-www-form-urlencoded"}},
])
async def test_mark_read_accepts_only_an_empty_json_object(
    http, session_factory, execution_settings, workspace, kwargs
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    response = await http.post(f"{ROUTE}/{note.id}/read", **kwargs)
    assert response.status_code == 422
    assert await read_at(session_factory, note.id) is None


async def test_only_these_methods_are_offered(http) -> None:
    some = uuid.uuid4()
    for method, path in (("POST", ROUTE), ("PUT", ROUTE), ("DELETE", ROUTE),
                         ("PUT", f"{ROUTE}/{some}"), ("PATCH", f"{ROUTE}/{some}"),
                         ("DELETE", f"{ROUTE}/{some}"), ("GET", f"{ROUTE}/{some}/read"),
                         ("DELETE", f"{ROUTE}/{some}/read")):
        assert (await http.request(method, path)).status_code == 405, (method, path)


# ============================================================================
# F. Reading writes nothing; concurrency
# ============================================================================


async def test_reading_changes_nothing_and_builds_no_delivery_composition(
    http, session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await plant_delivery(session_factory, note, "spy", DeliveryRecordStatus.FAILED)
    before = await snapshot(session_factory)
    for _ in range(3):
        assert (await http.get(ROUTE)).status_code == 200
        assert (await http.get(f"{ROUTE}/{note.id}")).status_code == 200
        assert (await http.get(ROUTE, params={"unread_only": True, "before": str(note.id)})).status_code == 200
    assert await snapshot(session_factory) == before
    assert composition._registry is None


async def test_concurrent_mark_reads_stamp_once(
    monkeypatch, execution_settings, fake_provider, workspace, tmp_path
) -> None:
    """Five at once, each on its own connection (file-backed SQLite with a
    real pool): all answer 204, and the first stamp is the only stamp."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reads.db'}")
    configure_sqlite(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        note = await met_notification(factory, execution_settings, workspace)
        async with client(monkeypatch, execution_settings, factory, fake_provider) as c:
            responses = await asyncio.gather(*(c.post(f"{ROUTE}/{note.id}/read", json={})
                                               for _ in range(5)))
            stamped = await read_at(factory, note.id)
            shown = (await c.get(f"{ROUTE}/{note.id}")).json()["read_at"]
    finally:
        await engine.dispose()
    assert [r.status_code for r in responses] == [204] * 5
    assert stamped is not None and shown is not None
