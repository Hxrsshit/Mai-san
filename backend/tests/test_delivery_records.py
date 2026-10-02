"""Stage 6M.1: durable delivery records.

Through the real 6I service with real notifications from the 6G/6H runtime
path. Adapters are the 6I test doubles; the Telegram path is exercised by the
6K/6L suites, which run through these records too. A process restart is
modelled the only way that matters to the invariant: a fresh adapter (empty
in-memory duplicate memory) over the same database.

Concurrency is exercised on a file-backed SQLite with a real pool, so every
racing session has its own connection -- never the shared in-memory
connection. PostgreSQL concurrency is verified live, outside this suite.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.metadata import Base
from app.database.session import configure_sqlite
from app.delivery import records
from app.delivery.contract import DeliveryOutcome, DeliveryStatus, NotificationAdapter
from app.delivery.local import LocalRecordingAdapter
from app.delivery.models import DeliveryRecordStatus, NotificationDelivery
from app.delivery.service import ADAPTER_TIMEOUT_SECONDS, NotificationDeliveryService
from app.tasks.models import TaskNotification
from tests.test_delivery import (
    OTHER,
    OWNER,
    FailingAdapter,
    HangingAdapter,
    RaisingAdapter,
    SpyAdapter,
    WrongTypeAdapter,
    deliver,
    met_notification,
    registry_with,
    snapshot,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


class ScriptedAdapter(NotificationAdapter):
    """Answers from a script, one status per call; counts calls."""

    name = "scripted"

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = 0

    async def deliver(self, payload):
        self.calls += 1
        return self.statuses.pop(0)


class SlowAdapter(NotificationAdapter):
    """Holds its claim long enough for racing attempts to meet it."""

    name = "slow"

    def __init__(self):
        self.calls = 0

    async def deliver(self, payload):
        self.calls += 1
        await asyncio.sleep(0.2)
        return DeliveryStatus.DELIVERED


async def rows(session_factory, notification_id=None):
    async with session_factory() as session:
        query = select(NotificationDelivery)
        if notification_id is not None:
            query = query.where(NotificationDelivery.notification_id == notification_id)
        return list((await session.execute(query)).scalars().all())


# ============================================================================
# A. One success is recorded; every later attempt is DUPLICATE, unsent
# ============================================================================


async def test_a_delivery_is_recorded_once_and_durably(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    spy = SpyAdapter()
    result = await deliver(session_factory, note.id, "spy", registry_with(spy))

    assert result.outcome is DeliveryOutcome.DELIVERED
    [row] = await rows(session_factory, note.id)
    assert (row.adapter, row.status, row.attempts) == ("spy", DeliveryRecordStatus.DELIVERED, 1)
    assert row.owner_id == OWNER and row.delivered_at is not None and row.lease_expires_at is None


async def test_success_then_retry_is_duplicate_and_never_reaches_the_adapter(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    spy = SpyAdapter()
    registry = registry_with(spy)
    outcomes = [(await deliver(session_factory, note.id, "spy", registry)).outcome for _ in range(3)]
    assert outcomes == [DeliveryOutcome.DELIVERED] + [DeliveryOutcome.DUPLICATE] * 2
    assert len(spy.payloads) == 1
    [row] = await rows(session_factory, note.id)
    assert row.attempts == 1


async def test_a_restart_cannot_deliver_again(
    session_factory, execution_settings, workspace
) -> None:
    """A fresh adapter has an empty memory -- as after a restart. The record
    in the database still answers DUPLICATE, and the new adapter is never
    asked."""
    note = await met_notification(session_factory, execution_settings, workspace)
    before_restart = LocalRecordingAdapter()
    assert (await deliver(session_factory, note.id, "local",
                          registry_with(before_restart))).outcome is DeliveryOutcome.DELIVERED
    after_restart = LocalRecordingAdapter()
    replay = await deliver(session_factory, note.id, "local", registry_with(after_restart))
    assert replay.outcome is DeliveryOutcome.DUPLICATE
    assert after_restart.delivered == ()


async def test_an_adapters_own_duplicate_is_recorded_as_delivered(
    session_factory, execution_settings, workspace
) -> None:
    """An adapter answering DUPLICATE says it already sent this key; that is a
    delivery, so the record becomes `delivered` and is not retried."""
    note = await met_notification(session_factory, execution_settings, workspace)
    adapter = ScriptedAdapter(DeliveryStatus.DUPLICATE)
    assert (await deliver(session_factory, note.id, "scripted",
                          registry_with(adapter))).outcome is DeliveryOutcome.DUPLICATE
    [row] = await rows(session_factory, note.id)
    assert row.status is DeliveryRecordStatus.DELIVERED


async def test_each_adapter_has_its_own_record(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    local, spy = LocalRecordingAdapter(), SpyAdapter()
    registry = registry_with(local, spy)
    assert (await deliver(session_factory, note.id, "local", registry)).outcome is DeliveryOutcome.DELIVERED
    assert (await deliver(session_factory, note.id, "spy", registry)).outcome is DeliveryOutcome.DELIVERED
    assert sorted(r.adapter for r in await rows(session_factory, note.id)) == ["local", "spy"]


# ============================================================================
# B. A failure is recorded and stays retryable
# ============================================================================


@pytest.mark.parametrize("adapter,reason", [
    (FailingAdapter(), None),
    (RaisingAdapter(), "adapter_error"),
    (WrongTypeAdapter(), "adapter_invalid_status"),
])
async def test_a_failed_attempt_is_recorded_failed(
    session_factory, execution_settings, workspace, adapter, reason
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    result = await deliver(session_factory, note.id, adapter.name, registry_with(adapter))
    assert result.outcome is DeliveryOutcome.FAILED and result.reason == reason
    [row] = await rows(session_factory, note.id)
    assert row.status is DeliveryRecordStatus.FAILED
    assert row.delivered_at is None and row.lease_expires_at is None


async def test_a_timed_out_attempt_is_recorded_failed(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    result = await deliver(session_factory, note.id, "hanging",
                           registry_with(HangingAdapter()), timeout_seconds=0.05)
    assert result.reason == "adapter_timeout"
    [row] = await rows(session_factory, note.id)
    assert row.status is DeliveryRecordStatus.FAILED


async def test_failure_then_retry_delivers_once(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    adapter = ScriptedAdapter(DeliveryStatus.FAILED, DeliveryStatus.DELIVERED)
    registry = registry_with(adapter)
    first = await deliver(session_factory, note.id, "scripted", registry)
    second = await deliver(session_factory, note.id, "scripted", registry)
    third = await deliver(session_factory, note.id, "scripted", registry)
    assert [first.outcome, second.outcome, third.outcome] == [
        DeliveryOutcome.FAILED, DeliveryOutcome.DELIVERED, DeliveryOutcome.DUPLICATE,
    ]
    assert adapter.calls == 2
    [row] = await rows(session_factory, note.id)
    assert (row.status, row.attempts) == (DeliveryRecordStatus.DELIVERED, 2)


# ============================================================================
# C. Claims, leases and crashes
# ============================================================================


async def _plant(session_factory, note, adapter, *, status, lease, attempts=1):
    async with session_factory() as session:
        session.add(NotificationDelivery(
            notification_id=note.id, owner_id=note.owner_id, adapter=adapter,
            status=status, attempts=attempts, lease_expires_at=lease,
        ))
        await session.commit()


async def test_a_live_claim_refuses_a_second_attempt_unsent(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await _plant(session_factory, note, "spy", status=DeliveryRecordStatus.SENDING,
                 lease=datetime.now(timezone.utc) + timedelta(minutes=5))
    spy = SpyAdapter()
    result = await deliver(session_factory, note.id, "spy", registry_with(spy))
    assert (result.outcome, result.reason) == (DeliveryOutcome.REFUSED, "delivery_in_progress")
    assert spy.payloads == []


async def test_an_abandoned_claim_is_taken_over_after_its_lease(
    session_factory, execution_settings, workspace
) -> None:
    """A crash mid-attempt leaves `sending` behind. Once the lease passes the
    next attempt may send: the documented at-least-once window."""
    note = await met_notification(session_factory, execution_settings, workspace)
    await _plant(session_factory, note, "spy", status=DeliveryRecordStatus.SENDING,
                 lease=datetime.now(timezone.utc) - timedelta(seconds=1))
    spy = SpyAdapter()
    result = await deliver(session_factory, note.id, "spy", registry_with(spy))
    assert result.outcome is DeliveryOutcome.DELIVERED and len(spy.payloads) == 1
    [row] = await rows(session_factory, note.id)
    assert (row.status, row.attempts) == (DeliveryRecordStatus.DELIVERED, 2)


async def test_a_superseded_attempt_cannot_overwrite_its_successor(
    session_factory, execution_settings, workspace
) -> None:
    """The fencing token: an attempt whose lease was taken over finishes
    against a row that is no longer its own, and changes nothing."""
    note = await met_notification(session_factory, execution_settings, workspace)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with session_factory() as session:
        stale = await records.claim(session, notification_id=note.id, owner_id=OWNER,
                                    adapter="spy", now=past)
    async with session_factory() as session:
        fresh = await records.claim(session, notification_id=note.id, owner_id=OWNER, adapter="spy")
    assert isinstance(stale, records.Claim) and isinstance(fresh, records.Claim)
    assert (stale.attempt, fresh.attempt) == (1, 2)
    async with session_factory() as session:
        assert await records.finish(session, stale, delivered=False) is False
    [row] = await rows(session_factory, note.id)
    assert (row.status, row.attempts) == (DeliveryRecordStatus.SENDING, 2)
    async with session_factory() as session:
        assert await records.finish(session, fresh, delivered=True) is True
    [row] = await rows(session_factory, note.id)
    assert row.status is DeliveryRecordStatus.DELIVERED


def _stale_first_read(monkeypatch, stale):
    """Make the claim's first read return `stale` -- the row as it looked a
    moment ago -- so a race can be replayed deterministically."""
    real, calls = records._record, []

    async def first_read_stale(session, notification_id, adapter):
        calls.append(1)
        return stale if len(calls) == 1 else await real(session, notification_id, adapter)

    monkeypatch.setattr(records, "_record", first_read_stale)


async def test_losing_the_insert_race_defers_to_the_winners_row(
    session_factory, execution_settings, workspace, monkeypatch
) -> None:
    """Two first claims: both read "no row", both insert, the unique index
    refuses the second. The loser must take the winner's row as the answer,
    never its own unsaved claim."""
    note = await met_notification(session_factory, execution_settings, workspace)
    async with session_factory() as session:
        session.add(NotificationDelivery(
            notification_id=note.id, owner_id=OWNER, adapter="spy",
            status=DeliveryRecordStatus.DELIVERED, attempts=1,
            delivered_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    _stale_first_read(monkeypatch, None)
    async with session_factory() as session:
        answer = await records.claim(session, notification_id=note.id, owner_id=OWNER, adapter="spy")
    assert answer == records.ALREADY_DELIVERED
    [row] = await rows(session_factory, note.id)
    assert (row.status, row.attempts) == (DeliveryRecordStatus.DELIVERED, 1)


async def test_a_reclaim_from_a_stale_view_cannot_reuse_an_attempt_number(
    session_factory, execution_settings, workspace, monkeypatch
) -> None:
    """The re-claim is fenced on the attempt count it read. Replayed: this
    attempt read the row at attempt 1; meanwhile another took it over
    (attempt 2) and also stalled past its lease. Unfenced, this attempt would
    claim "attempt 2" too -- the same token as the stalled one, which could
    then overwrite it. Fenced, it backs off."""
    from types import SimpleNamespace

    note = await met_notification(session_factory, execution_settings, workspace)
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    await _plant(session_factory, note, "spy", status=DeliveryRecordStatus.SENDING,
                 lease=expired, attempts=2)
    [row] = await rows(session_factory, note.id)
    _stale_first_read(monkeypatch, SimpleNamespace(
        id=row.id, attempts=1, status=DeliveryRecordStatus.SENDING))
    async with session_factory() as session:
        answer = await records.claim(session, notification_id=note.id, owner_id=OWNER, adapter="spy")
    assert answer == records.IN_PROGRESS
    [row] = await rows(session_factory, note.id)
    assert row.attempts == 2


async def test_a_finished_claim_cannot_be_finished_again(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    async with session_factory() as session:
        held = await records.claim(session, notification_id=note.id, owner_id=OWNER, adapter="spy")
    async with session_factory() as session:
        assert await records.finish(session, held, delivered=True) is True
    async with session_factory() as session:
        assert await records.finish(session, held, delivered=False) is False
    [row] = await rows(session_factory, note.id)
    assert row.status is DeliveryRecordStatus.DELIVERED and row.delivered_at is not None


async def test_the_lease_outlasts_the_adapter_timeout() -> None:
    assert records.CLAIM_LEASE_SECONDS > 3 * ADAPTER_TIMEOUT_SECONDS


async def test_concurrent_attempts_send_exactly_once(
    execution_settings, workspace, tmp_path
) -> None:
    """Six attempts at once, each its own session on its own connection:
    one claim wins and sends; the others are refused while it holds the
    claim, or answered DUPLICATE once it has finished."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    configure_sqlite(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        note = await met_notification(factory, execution_settings, workspace)
        slow = SlowAdapter()
        registry = registry_with(slow)

        async def attempt():
            async with factory() as session:
                return await NotificationDeliveryService(session, OWNER, registry).deliver(note.id, "slow")

        results = await asyncio.gather(*(attempt() for _ in range(6)))
        stored = await rows(factory, note.id)
    finally:
        await engine.dispose()

    outcomes = [(r.outcome.value, r.reason) for r in results]
    assert outcomes.count(("delivered", None)) == 1
    assert set(outcomes) - {("delivered", None)} <= {("duplicate", None), ("refused", "delivery_in_progress")}
    assert slow.calls == 1
    assert [(r.status, r.attempts) for r in stored] == [(DeliveryRecordStatus.DELIVERED, 1)]


# ============================================================================
# D. Refusals record nothing; ownership; the notification is untouched
# ============================================================================


async def test_refusals_create_no_record(
    session_factory, execution_settings, workspace
) -> None:
    import uuid as _uuid

    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    spy = SpyAdapter()
    registry = registry_with(spy)
    results = [
        await deliver(session_factory, mine.id, "nope", registry),
        await deliver(session_factory, _uuid.uuid4(), "spy", registry),
        await deliver(session_factory, theirs.id, "spy", registry),          # as OWNER
        await deliver(session_factory, str(mine.id), "spy", registry),
    ]
    assert [r.reason for r in results] == [
        "unknown_adapter", "notification_not_found", "notification_not_found",
        "malformed_notification",
    ]
    assert await rows(session_factory) == [] and spy.payloads == []


async def test_another_owners_delivery_record_does_not_answer_for_mine(
    session_factory, execution_settings, workspace
) -> None:
    """Records are per notification, and a notification has one owner: the
    other owner's delivered record says nothing about my notification."""
    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    spy = SpyAdapter()
    registry = registry_with(spy)
    assert (await deliver(session_factory, theirs.id, "spy", registry, owner=OTHER)).outcome is DeliveryOutcome.DELIVERED
    assert (await deliver(session_factory, mine.id, "spy", registry)).outcome is DeliveryOutcome.DELIVERED
    owners = sorted((str(r.owner_id), r.notification_id == mine.id) for r in await rows(session_factory))
    assert owners == sorted([(str(OTHER), False), (str(OWNER), True)])


async def test_delivery_changes_no_notification_task_or_read_state(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    before = await snapshot(session_factory)
    await deliver(session_factory, note.id, "spy", registry_with(SpyAdapter()))
    await deliver(session_factory, note.id, "failing", registry_with(FailingAdapter()))
    assert await snapshot(session_factory) == before


async def test_records_go_with_their_notification(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await deliver(session_factory, note.id, "spy", registry_with(SpyAdapter()))
    async with session_factory() as session:
        await session.execute(delete(TaskNotification).where(TaskNotification.id == note.id))
        await session.commit()
    assert await rows(session_factory) == []


# ============================================================================
# E. The table holds the invariant, not the code
# ============================================================================


async def test_the_database_refuses_a_second_record_for_the_same_pair(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await _plant(session_factory, note, "spy", status=DeliveryRecordStatus.FAILED, lease=None)
    with pytest.raises(IntegrityError):
        await _plant(session_factory, note, "spy", status=DeliveryRecordStatus.FAILED, lease=None)


@pytest.mark.parametrize("status,lease,delivered_at,attempts", [
    (DeliveryRecordStatus.DELIVERED, None, None, 1),                       # delivered, no time
    (DeliveryRecordStatus.FAILED, None, "now", 1),                         # time, not delivered
    (DeliveryRecordStatus.SENDING, None, None, 1),                         # sending, no lease
    (DeliveryRecordStatus.FAILED, None, None, 0),                          # no attempt
])
async def test_the_database_refuses_inconsistent_rows(
    session_factory, execution_settings, workspace, status, lease, delivered_at, attempts
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            session.add(NotificationDelivery(
                notification_id=note.id, owner_id=note.owner_id, adapter="spy",
                status=status, attempts=attempts, lease_expires_at=lease,
                delivered_at=datetime.now(timezone.utc) if delivered_at else None,
            ))
            await session.commit()


def test_a_record_holds_no_content_recipient_credential_or_error() -> None:
    columns = [c.name for c in inspect(NotificationDelivery).columns]
    assert columns == [
        "id", "notification_id", "owner_id", "adapter", "status", "attempts",
        "lease_expires_at", "created_at", "updated_at", "delivered_at",
    ]
