"""Stage 6M.3: automatic delivery, the runtime's third work source.

Through the real `BackgroundRuntime.tick()`, the real 6K composition, 6I with
its 6M.1 durable claim, and the 6J Telegram adapter with only its transport
stubbed. Notifications come from the real 6G/6H runtime path -- in the main
flow, from the very tick that then delivers them. A restart is a new runtime
and a freshly built composition (empty in-process memory) over the same
database. Every token is a synthetic sentinel.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.background import deliveries as auto
from app.background.runtime import BackgroundRuntime
from app.composition import notification_delivery as composition
from app.composition.notification_delivery import get_delivery_registry, notification_delivery_service
from app.database.metadata import Base
from app.database.session import configure_sqlite
from app.delivery.contract import DeliveryOutcome
from app.delivery.models import DeliveryRecordStatus, NotificationDelivery
from app.tasks.models import LOCAL_OWNER_ID, TaskNotification, TaskNotificationKind
from tests.support.stub_transport import StubTransport
from tests.test_delivery import OTHER, _monitor, met_notification, snapshot
from tests.test_notification_composition import stub_network

pytestmark = pytest.mark.anyio

TOKEN = "999000111:TOKEN-SENTINEL-6m3_Rt4v"
CHAT_ID = 535353


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_composition(monkeypatch, execution_settings):
    """No registry built yet, and the composition never reads the environment."""
    monkeypatch.setattr(composition, "_registry", None)
    monkeypatch.setattr(composition, "get_settings", lambda: execution_settings)


@pytest.fixture
def enabled(execution_settings):
    """Telegram configured (sentinel) and automatic delivery switched on."""
    execution_settings.TELEGRAM_BOT_TOKEN = TOKEN
    execution_settings.TELEGRAM_ALLOWED_CHAT_ID = str(CHAT_ID)
    execution_settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS = ["telegram"]
    return execution_settings


class SlowTransport(StubTransport):
    async def handle_async_request(self, request):
        await asyncio.sleep(0.2)
        return await super().handle_async_request(request)


async def records(session_factory, notification_id=None):
    async with session_factory() as session:
        query = select(NotificationDelivery)
        if notification_id is not None:
            query = query.where(NotificationDelivery.notification_id == notification_id)
        return list((await session.execute(query)).scalars())


async def more_notifications(session_factory, note, count):
    """`count` further notifications on `note`'s task, created now."""
    made = []
    async with session_factory() as session:
        for i in range(count):
            row = TaskNotification(owner_id=note.owner_id, task_id=note.task_id,
                                   kind=TaskNotificationKind.CONDITION_MET,
                                   check_number=note.check_number + 100 + i,
                                   created_at=datetime.now(timezone.utc) + timedelta(milliseconds=i))
            session.add(row)
            made.append(row)
        await session.commit()
    return made


# ============================================================================
# A. Opt-in
# ============================================================================


async def test_off_by_default_nothing_is_delivered_and_nothing_is_built(
    session_factory, execution_settings, workspace
) -> None:
    assert execution_settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS == []
    runtime = BackgroundRuntime(session_factory, execution_settings)
    assert runtime.deliveries_enabled is False
    note = await met_notification(session_factory, execution_settings, workspace)
    for _ in range(3):
        report = await runtime.tick()
        assert report.deliveries_attempted == 0
    assert await records(session_factory, note.id) == []
    assert composition._registry is None          # the delivery graph was never built


async def test_enabled_a_notification_made_in_a_tick_is_delivered_in_that_tick(
    session_factory, enabled, workspace
) -> None:
    """The full flow: a monitoring check recorded the notification, and the
    same tick delivered it through Telegram, durably."""
    runtime = BackgroundRuntime(session_factory, enabled)
    transport = stub_network(get_delivery_registry())
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_text("x", encoding="utf-8")
    task_id = await _monitor(session_factory, enabled)

    report = await runtime.tick()

    async with session_factory() as session:
        note = (await session.execute(
            select(TaskNotification).where(TaskNotification.task_id == task_id))).scalars().one()
    assert report.deliveries_attempted == 1
    assert transport.methods == ["POST"] and len(transport.connections) == 1
    assert json.loads(transport.bodies[0])["chat_id"] == CHAT_ID
    [record] = await records(session_factory, note.id)
    assert (record.adapter, record.status, record.attempts) == ("telegram", DeliveryRecordStatus.DELIVERED, 1)


# ============================================================================
# B. Only notifications created after enablement
# ============================================================================


async def test_a_notification_from_before_enablement_is_never_delivered(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)         # enabled after it
    transport = stub_network(get_delivery_registry())
    for _ in range(3):
        assert (await runtime.tick()).deliveries_attempted == 0
    assert transport.connections == [] and await records(session_factory, old.id) == []


async def test_a_notification_from_after_enablement_is_eligible(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    [new] = await more_notifications(session_factory, old, 1)
    transport = stub_network(get_delivery_registry())
    assert (await runtime.tick()).deliveries_attempted == 1
    assert len(transport.connections) == 1
    assert [r.notification_id for r in await records(session_factory)] == [new.id]


# ============================================================================
# C. At most once; never retried
# ============================================================================


async def test_a_delivered_notification_is_not_sent_again_over_many_ticks(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    await more_notifications(session_factory, old, 1)
    transport = stub_network(get_delivery_registry())
    attempted = [(await runtime.tick()).deliveries_attempted for _ in range(4)]
    assert attempted == [1, 0, 0, 0] and len(transport.connections) == 1


async def test_a_failure_is_recorded_and_never_retried_automatically(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    [new] = await more_notifications(session_factory, old, 1)
    transport = stub_network(get_delivery_registry(), StubTransport(status_code=500, payload={"ok": False}))
    attempted = [(await runtime.tick()).deliveries_attempted for _ in range(4)]
    assert attempted == [1, 0, 0, 0] and len(transport.connections) == 1
    [record] = await records(session_factory, new.id)
    assert (record.status, record.attempts) == (DeliveryRecordStatus.FAILED, 1)


async def test_a_failure_stays_retryable_by_a_person(
    session_factory, enabled, workspace
) -> None:
    """Automatic delivery never retries, but 6M.1's explicit retry is intact."""
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    [new] = await more_notifications(session_factory, old, 1)
    transport = stub_network(get_delivery_registry(), StubTransport(responses=[
        {"status_code": 500, "payload": {"ok": False}},
        {"status_code": 200, "payload": {"ok": True, "result": {"message_id": 1}}},
    ]))
    await runtime.tick()
    async with session_factory() as session:
        manual = await notification_delivery_service(session).deliver(new.id, "telegram")
    assert manual.outcome is DeliveryOutcome.DELIVERED and len(transport.connections) == 2
    assert (await runtime.tick()).deliveries_attempted == 0


async def test_another_channels_record_does_not_block_this_channel(
    session_factory, enabled, workspace
) -> None:
    """Exclusion is per channel: a failure recorded for `local` says nothing
    about whether the notification has gone out through `telegram`."""
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    [new] = await more_notifications(session_factory, old, 1)
    async with session_factory() as session:
        session.add(NotificationDelivery(
            notification_id=new.id, owner_id=new.owner_id, adapter="local",
            status=DeliveryRecordStatus.FAILED, attempts=1,
        ))
        await session.commit()
    transport = stub_network(get_delivery_registry())
    assert (await runtime.tick()).deliveries_attempted == 1 and len(transport.connections) == 1
    assert sorted((r.adapter, r.status.value) for r in await records(session_factory, new.id)) == [
        ("local", "failed"), ("telegram", "delivered")]


async def test_an_abandoned_claim_is_left_for_a_person(
    session_factory, enabled, workspace
) -> None:
    """A `sending` row past its lease is claimable by an explicit delivery,
    never by the automatic one: the lease is crash recovery, not a retry
    scheduler."""
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    [new] = await more_notifications(session_factory, old, 1)
    async with session_factory() as session:
        session.add(NotificationDelivery(
            notification_id=new.id, owner_id=new.owner_id, adapter="telegram",
            status=DeliveryRecordStatus.SENDING, attempts=1,
            lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        await session.commit()
    transport = stub_network(get_delivery_registry())
    assert (await runtime.tick()).deliveries_attempted == 0 and transport.connections == []
    [record] = await records(session_factory, new.id)
    assert (record.status, record.attempts) == (DeliveryRecordStatus.SENDING, 1)


# ============================================================================
# D. Restart and concurrency: the durable claim decides
# ============================================================================


async def test_a_restart_cannot_send_the_same_notification_again(
    session_factory, enabled, workspace, monkeypatch
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    first = BackgroundRuntime(session_factory, enabled)
    [new] = await more_notifications(session_factory, old, 1)
    before_restart = stub_network(get_delivery_registry())
    await first.tick()
    assert len(before_restart.connections) == 1

    # Restart: a new runtime and a newly built composition (empty memory).
    monkeypatch.setattr(composition, "_registry", None)
    after_restart = stub_network(get_delivery_registry())
    second = BackgroundRuntime(session_factory, enabled)
    for _ in range(2):
        await second.tick()
    # Even a pass whose boundary covers the notification finds no candidate...
    replay = await auto.run_automatic_deliveries(session_factory, enabled, since=new.created_at)
    # ...and the send path itself answers from the durable record.
    async with session_factory() as session:
        direct = await notification_delivery_service(session).deliver(new.id, "telegram")
    assert replay.attempted == 0 and direct.outcome is DeliveryOutcome.DUPLICATE
    assert after_restart.connections == []


async def test_concurrent_runtimes_send_each_notification_once(
    enabled, workspace, tmp_path, monkeypatch
) -> None:
    """Two runtimes ticking at once, each on its own connections (file-backed
    SQLite with a real pool). A barrier holds both passes at the moment they
    are about to deliver -- each has already read the same candidate, its
    read session closed -- so the candidate query cannot be what saves them:
    the committed claim lets exactly one reach the channel."""
    from app.delivery.service import NotificationDeliveryService

    real, seen, both_arrived = NotificationDeliveryService.deliver, [], asyncio.Event()

    async def wait_then_deliver(self, notification_id, adapter_name):
        seen.append(notification_id)
        if len(seen) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=5)
        return await real(self, notification_id, adapter_name)

    monkeypatch.setattr(NotificationDeliveryService, "deliver", wait_then_deliver)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auto.db'}")
    configure_sqlite(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        old = await met_notification(factory, enabled, workspace)
        one, two = BackgroundRuntime(factory, enabled), BackgroundRuntime(factory, enabled)
        [new] = await more_notifications(factory, old, 1)
        transport = stub_network(get_delivery_registry(), SlowTransport(
            payload={"ok": True, "result": {"message_id": 1}}))
        reports = await asyncio.gather(one.tick(), two.tick())
        stored = await records(factory, new.id)
    finally:
        await engine.dispose()
    assert seen == [new.id, new.id]                                # same candidate
    assert sum(r.deliveries_attempted for r in reports) == 2      # both tried...
    assert len(transport.connections) == 1                         # ...one sent
    assert [(r.status, r.attempts) for r in stored] == [(DeliveryRecordStatus.DELIVERED, 1)]


# ============================================================================
# E. Bounded, owner-scoped, responsive
# ============================================================================


async def test_at_most_three_per_tick_and_the_rest_on_later_ticks(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    made = await more_notifications(session_factory, old, 7)
    transport = stub_network(get_delivery_registry())
    attempted = [(await runtime.tick()).deliveries_attempted for _ in range(4)]
    assert auto.HARD_MAX_DELIVERIES_PER_TICK == 3
    assert attempted == [3, 3, 1, 0]
    assert len(transport.connections) == 7
    assert sorted(r.notification_id for r in await records(session_factory)) == sorted(n.id for n in made)


async def test_the_cap_is_shared_across_channels(
    session_factory, enabled, workspace, monkeypatch
) -> None:
    """The budget is per tick, not per channel: with two channel passes, the
    second gets only what the first left."""
    monkeypatch.setattr(auto, "usable_channels", lambda settings: ["telegram", "telegram"])
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    await more_notifications(session_factory, old, 7)
    transport = stub_network(get_delivery_registry())
    assert (await runtime.tick()).deliveries_attempted == auto.HARD_MAX_DELIVERIES_PER_TICK
    assert len(transport.connections) == 3


async def test_the_oldest_new_notifications_go_first(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    made = await more_notifications(session_factory, old, 5)
    stub_network(get_delivery_registry())
    await runtime.tick()
    assert {r.notification_id for r in await records(session_factory)} == {n.id for n in made[:3]}


async def test_another_owners_notification_is_never_delivered(
    session_factory, enabled, workspace
) -> None:
    runtime = BackgroundRuntime(session_factory, enabled)
    theirs = await met_notification(session_factory, enabled, workspace, owner=OTHER)
    transport = stub_network(get_delivery_registry())
    for _ in range(3):
        assert (await runtime.tick()).deliveries_attempted == 0
    assert transport.connections == [] and await records(session_factory, theirs.id) == []


async def test_an_unregistered_channel_is_skipped_and_the_rest_still_work(
    session_factory, enabled, workspace
) -> None:
    enabled.NOTIFICATION_AUTO_DELIVER_ADAPTERS = ["email", "telegram", "local"]
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    await more_notifications(session_factory, old, 1)
    transport = stub_network(get_delivery_registry())
    assert (await runtime.tick()).deliveries_attempted == 1
    assert len(transport.connections) == 1
    assert {r.adapter for r in await records(session_factory)} == {"telegram"}


async def test_only_unregistered_channels_means_nothing_happens(
    session_factory, enabled, workspace
) -> None:
    enabled.NOTIFICATION_AUTO_DELIVER_ADAPTERS = ["email"]
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    await more_notifications(session_factory, old, 1)
    assert (await runtime.tick()).deliveries_attempted == 0
    assert await records(session_factory) == []


async def test_unconfigured_telegram_means_nothing_happens(
    session_factory, execution_settings, workspace
) -> None:
    """The setting names a channel the composition did not register (no
    Telegram credentials): fail closed, quietly."""
    execution_settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS = ["telegram"]
    old = await met_notification(session_factory, execution_settings, workspace)
    runtime = BackgroundRuntime(session_factory, execution_settings)
    await more_notifications(session_factory, old, 1)
    assert (await runtime.tick()).deliveries_attempted == 0
    assert await records(session_factory) == [] and get_delivery_registry().names() == ()


async def test_a_failing_delivery_pass_never_breaks_the_tick(
    session_factory, enabled, workspace, monkeypatch
) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(auto, "run_automatic_deliveries", broken)
    runtime = BackgroundRuntime(session_factory, enabled)
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_text("x", encoding="utf-8")
    await _monitor(session_factory, enabled)
    report = await runtime.tick()
    assert report.deliveries_attempted == 0 and report.tasks_advanced == 1


async def test_delivery_changes_no_notification_task_or_read_state(
    session_factory, enabled, workspace
) -> None:
    old = await met_notification(session_factory, enabled, workspace)
    runtime = BackgroundRuntime(session_factory, enabled)
    await more_notifications(session_factory, old, 2)
    stub_network(get_delivery_registry())
    before = await snapshot(session_factory)
    await runtime.tick()
    assert await snapshot(session_factory) == before


async def test_the_runtime_still_starts_and_stops_with_delivery_on(
    session_factory, enabled
) -> None:
    runtime = BackgroundRuntime(session_factory, enabled)
    assert runtime.deliveries_enabled and runtime.has_work_sources
    runtime.start()
    assert await runtime.wait_for_ticks(1, timeout=5.0)
    await runtime.stop()
    assert not runtime.running
