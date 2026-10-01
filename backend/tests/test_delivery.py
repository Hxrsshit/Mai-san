"""Stage 6I: the notification delivery boundary.

Notifications here are real: produced by the 6G/6H path -- the background
runtime, `TaskRunner.check`, the real authorization and execution services,
`record_outcome` -- against a temporary workspace. The boundary then reads
them through the owner-scoped `NotificationService` and hands them to an
adapter. Nothing on the production path is mocked; only adapters and, for the
malformed-data cases, the read itself are substituted.
"""

import asyncio
import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.authorization.grants import GrantService
from app.background.runtime import run_due_tasks
from app.delivery.contract import (
    DeliveryOutcome,
    DeliveryPayload,
    DeliveryResult,
    DeliveryStatus,
    NotificationAdapter,
    delivery_key,
)
from app.delivery.local import LocalRecordingAdapter
from app.delivery.registry import AdapterRegistry
from app.delivery.service import NotificationDeliveryService
from app.execution.models import Execution
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks.models import (
    MAX_CONSECUTIVE_FAILURES,
    Task,
    TaskEvent,
    TaskNotification,
    TaskNotificationKind,
)
from app.tasks.notifications import NotificationService
from app.tasks.service import TaskService
from app.tasks.states import TaskState
from app.tools.schemas import RiskLevel

pytestmark = pytest.mark.anyio

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
OTHER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")
LIST = "list_workspace_files"
INTERVAL = 300

#: Placed in the task objective. It must never reach an adapter or a log.
OBJECTIVE_SENTINEL = "objective-sentinel-6i-9d4f"
#: An adapter raises with this. It must never reach a result or a log.
ERROR_SENTINEL = "adapter-error-sentinel-6i-token-5e2a"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


# --- Producing real notifications through the 6G/6H path ---------------------


def _plan() -> Plan:
    return Plan(goal=Goal(summary="Watch", source_intent=IntentType.ACTION), tasks=[
        PlanTask(id="watch", title="Check", dependencies=[], order=1, depth=0,
                 capability=LIST, arguments={})
    ])


async def _monitor(session_factory, settings, *, owner=OWNER, path="files"):
    async with session_factory() as session:
        service = TaskService(session, settings=settings, owner_id=owner)
        created = await service.create_for_user(f"Watch {OBJECTIVE_SENTINEL}")
        assert (await service.attach_plan(created.task_id, _plan())).ok
        assert (await service.configure_monitoring(created.task_id, {
            "condition": {"kind": "count", "path": path, "operator": "gte",
                          "expected": 2},
            "interval_seconds": INTERVAL,
        })).ok
        await service.authorize_plan(created.task_id)
        await service.transition(created.task_id, TaskState.QUEUED, actor="user")
        await GrantService(session, owner_id=owner).create(LIST, RiskLevel.LOW)
        assert (await service.schedule_background(created.task_id)).ok
        await session.commit()
        return created.task_id


async def met_notification(session_factory, settings, workspace, *, owner=OWNER):
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_text("x", encoding="utf-8")
    task_id = await _monitor(session_factory, settings, owner=owner)
    await run_due_tasks(session_factory, settings, datetime.now(timezone.utc))
    async with session_factory() as session:
        note = (await session.execute(
            select(TaskNotification).where(TaskNotification.task_id == task_id)
        )).scalars().one()
    assert note.kind is TaskNotificationKind.CONDITION_MET
    return note


async def failed_notification(session_factory, settings, workspace):
    task_id = await _monitor(session_factory, settings, path="missing")
    moment = datetime.now(timezone.utc)
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        await run_due_tasks(session_factory, settings, moment)
        moment += timedelta(days=1)
    async with session_factory() as session:
        note = (await session.execute(
            select(TaskNotification).where(TaskNotification.task_id == task_id)
        )).scalars().one()
    assert note.kind is TaskNotificationKind.MONITORING_FAILED
    return note


def registry_with(*adapters) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


async def deliver(session_factory, notification_id, adapter_name, registry,
                  owner=OWNER, **kwargs) -> DeliveryResult:
    async with session_factory() as session:
        service = NotificationDeliveryService(session, owner, registry, **kwargs)
        result = await service.deliver(notification_id, adapter_name)
        # The boundary writes nothing; prove there is nothing pending.
        assert not session.new and not session.dirty and not session.deleted
        await session.rollback()
        return result


async def snapshot(session_factory):
    """Every row count and every piece of state delivery must not touch."""
    async with session_factory() as session:
        counts = tuple([
            (await session.execute(select(func.count()).select_from(m))).scalar()
            for m in (Task, TaskEvent, TaskNotification, Execution)
        ])
        tasks = tuple(sorted(
            (str(t.id), t.state.value, t.check_count, str(t.next_run_at))
            for t in (await session.execute(select(Task))).scalars()
        ))
        notes = tuple(sorted(
            (str(n.id), str(n.read_at), n.check_number, n.kind.value)
            for n in (await session.execute(select(TaskNotification))).scalars()
        ))
        return counts, tasks, notes


class RaisingAdapter(NotificationAdapter):
    name = "raising"

    def __init__(self):
        self.calls = 0

    async def deliver(self, payload):
        self.calls += 1
        raise RuntimeError(f"channel refused with {ERROR_SENTINEL}")


class FailingAdapter(NotificationAdapter):
    name = "failing"

    async def deliver(self, payload):
        return DeliveryStatus.FAILED


class WrongTypeAdapter(NotificationAdapter):
    name = "wrong_type"

    async def deliver(self, payload):
        return "delivered"


class HangingAdapter(NotificationAdapter):
    name = "hanging"

    async def deliver(self, payload):
        await asyncio.sleep(30)
        return DeliveryStatus.DELIVERED


class SpyAdapter(NotificationAdapter):
    name = "spy"

    def __init__(self):
        self.payloads = []

    async def deliver(self, payload):
        self.payloads.append(payload)
        return DeliveryStatus.DELIVERED


# ============================================================================
# A. The registry
# ============================================================================


def test_an_adapter_registers_and_is_found_by_its_canonical_name() -> None:
    local = LocalRecordingAdapter()
    registry = registry_with(local)
    assert registry.names() == ("local",)
    assert len(registry) == 1
    assert registry.get("local") is local
    assert registry.get("  LOCAL ") is local
    assert registry.get("loc") is None
    assert registry.get(None) is None
    assert registry.get(7) is None


def test_a_duplicate_name_is_refused() -> None:
    registry = registry_with(LocalRecordingAdapter())
    with pytest.raises(ValueError):
        registry.register(LocalRecordingAdapter())
    assert len(registry) == 1


@pytest.mark.parametrize("name", [
    "", "x", "1local", "local-adapter", "local adapter", "lo.cal", "a" * 33,
    "../etc", "local;rm",
])
def test_a_badly_named_adapter_is_refused(name) -> None:
    class Named(NotificationAdapter):
        pass

    adapter = Named()
    adapter.name = name
    with pytest.raises(ValueError):
        AdapterRegistry().register(adapter)


def test_something_that_is_not_an_adapter_is_refused() -> None:
    class Impostor:
        name = "impostor"

        async def deliver(self, payload):
            return DeliveryStatus.DELIVERED

    with pytest.raises(TypeError):
        AdapterRegistry().register(Impostor())


def test_a_sealed_registry_accepts_nothing() -> None:
    registry = registry_with(LocalRecordingAdapter())
    registry.seal()
    assert registry.sealed
    with pytest.raises(RuntimeError):
        registry.register(SpyAdapter())
    assert registry.names() == ("local",)


# ============================================================================
# B. Successful delivery, and what the adapter receives
# ============================================================================


async def test_a_notification_reaches_the_adapter(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    local = LocalRecordingAdapter()
    result = await deliver(session_factory, note.id, "local", registry_with(local))

    assert result == DeliveryResult(
        outcome=DeliveryOutcome.DELIVERED, adapter="local",
        notification_id=note.id,
        delivery_key=f"notification:{note.id}:adapter:local",
    )
    [payload] = local.delivered
    assert payload.notification_id == note.id
    assert payload.task_id == note.task_id
    assert payload.kind is TaskNotificationKind.CONDITION_MET
    assert payload.check_number == note.check_number == 1
    assert payload.created_at == note.created_at
    assert payload.delivery_key == result.delivery_key


async def test_a_failure_notification_is_delivered_with_its_kind(
    session_factory, execution_settings, workspace
) -> None:
    note = await failed_notification(session_factory, execution_settings, workspace)
    local = LocalRecordingAdapter()
    result = await deliver(session_factory, note.id, "local", registry_with(local))
    assert result.outcome is DeliveryOutcome.DELIVERED
    [payload] = local.delivered
    assert payload.kind is TaskNotificationKind.MONITORING_FAILED
    assert payload.check_number == MAX_CONSECUTIVE_FAILURES


async def test_the_adapter_receives_only_the_permitted_fields(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    spy = SpyAdapter()
    await deliver(session_factory, note.id, "spy", registry_with(spy))
    [payload] = spy.payloads
    assert type(payload) is DeliveryPayload
    assert sorted(payload.model_dump()) == [
        "check_number", "created_at", "delivery_key", "kind", "notification_id",
        "task_id",
    ]
    assert OBJECTIVE_SENTINEL not in repr(payload.model_dump())
    # The owner and the proving execution stay behind the boundary.
    assert str(OWNER) not in repr(payload.model_dump())
    assert str(note.execution_id) not in repr(payload.model_dump())


def test_the_payload_is_closed_frozen_and_strict() -> None:
    fields = dict(
        notification_id=uuid.uuid4(), task_id=uuid.uuid4(),
        kind=TaskNotificationKind.CONDITION_MET, check_number=1,
        created_at=datetime.now(timezone.utc), delivery_key="k",
    )
    payload = DeliveryPayload(**fields)
    with pytest.raises(ValidationError):
        DeliveryPayload(**fields, objective="leak")
    with pytest.raises(ValidationError):
        payload.kind = TaskNotificationKind.MONITORING_FAILED
    for bad in (
        {"kind": "condition_met"}, {"check_number": -1}, {"check_number": "1"},
        {"created_at": "2026-01-01"}, {"notification_id": "not-a-uuid"},
        {"delivery_key": ""},
    ):
        with pytest.raises(ValidationError):
            DeliveryPayload(**{**fields, **bad})


def test_a_result_reason_is_a_code_never_a_message() -> None:
    with pytest.raises(ValidationError):
        DeliveryResult(outcome=DeliveryOutcome.FAILED, reason="Channel said: token=abc")
    assert DeliveryResult(outcome=DeliveryOutcome.FAILED, reason="adapter_error").reason


# ============================================================================
# C. Idempotency through the delivery key
# ============================================================================


async def test_the_same_notification_through_the_same_adapter_is_delivered_once(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    local = LocalRecordingAdapter()
    registry = registry_with(local)
    first = await deliver(session_factory, note.id, "local", registry)
    again = [await deliver(session_factory, note.id, "local", registry) for _ in range(3)]

    assert first.outcome is DeliveryOutcome.DELIVERED
    assert {r.outcome for r in again} == {DeliveryOutcome.DUPLICATE}
    assert {r.delivery_key for r in again} == {first.delivery_key}
    assert len(local.delivered) == 1


async def test_each_adapter_has_its_own_delivery_identity(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    local, spy = LocalRecordingAdapter(), SpyAdapter()
    registry = registry_with(local, spy)
    a = await deliver(session_factory, note.id, "local", registry)
    b = await deliver(session_factory, note.id, "spy", registry)
    assert a.outcome is b.outcome is DeliveryOutcome.DELIVERED
    assert a.delivery_key != b.delivery_key
    assert a.delivery_key == delivery_key(note.id, "local")
    assert b.delivery_key == delivery_key(note.id, "spy")


async def test_distinct_notifications_have_distinct_identities(
    session_factory, execution_settings, workspace
) -> None:
    met = await met_notification(session_factory, execution_settings, workspace)
    failed = await failed_notification(session_factory, execution_settings, workspace)
    local = LocalRecordingAdapter()
    registry = registry_with(local)
    await deliver(session_factory, met.id, "local", registry)
    await deliver(session_factory, failed.id, "local", registry)
    assert len({p.delivery_key for p in local.delivered}) == 2


# ============================================================================
# D. Refusals: nothing reaches any adapter
# ============================================================================


async def test_an_unknown_adapter_is_refused(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    spy = SpyAdapter()
    registry = registry_with(spy)
    for name in ("telegram", "", None, 7, "spy2", "sp", "local"):
        result = await deliver(session_factory, note.id, name, registry)
        assert (result.outcome, result.reason) == (
            DeliveryOutcome.REFUSED, "unknown_adapter",
        ), name
    assert spy.payloads == []
    # The registered one, under its canonical spelling, is found.
    assert (await deliver(session_factory, note.id, "  SPY ", registry)).outcome is (
        DeliveryOutcome.DELIVERED
    )
    assert len(spy.payloads) == 1


async def test_a_missing_notification_is_refused(
    session_factory, execution_settings, workspace
) -> None:
    spy = SpyAdapter()
    result = await deliver(session_factory, uuid.uuid4(), "spy", registry_with(spy))
    assert (result.outcome, result.reason) == (
        DeliveryOutcome.REFUSED, "notification_not_found",
    )
    assert spy.payloads == []


@pytest.mark.parametrize("bad_id", [None, "", "not-a-uuid", 12, b"x"])
async def test_a_malformed_notification_id_is_refused(
    session_factory, bad_id
) -> None:
    spy = SpyAdapter()
    result = await deliver(session_factory, bad_id, "spy", registry_with(spy))
    assert (result.outcome, result.reason) == (
        DeliveryOutcome.REFUSED, "malformed_notification",
    )
    assert spy.payloads == []


async def test_a_notification_id_as_text_is_refused_not_coerced(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    spy = SpyAdapter()
    result = await deliver(session_factory, str(note.id), "spy", registry_with(spy))
    assert result.reason == "malformed_notification"
    assert spy.payloads == []


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def _valid_row(**overrides):
    base = dict(
        id=uuid.uuid4(), task_id=uuid.uuid4(),
        kind=TaskNotificationKind.CONDITION_MET, check_number=1,
        created_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return _Row(**base)


@pytest.mark.parametrize("row", [
    _valid_row(kind="condition_met"),
    _valid_row(kind="send_telegram"),
    _valid_row(check_number=-1),
    _valid_row(check_number="1"),
    _valid_row(created_at="2026-10-01"),
    _valid_row(task_id="not-a-uuid"),
    _Row(id=uuid.uuid4(), kind=TaskNotificationKind.CONDITION_MET),  # fields missing
])
async def test_malformed_notification_data_is_refused_safely(
    session_factory, monkeypatch, row
) -> None:
    async def fake_get(self, notification_id):
        return row

    monkeypatch.setattr(NotificationService, "get", fake_get)
    spy = SpyAdapter()
    result = await deliver(session_factory, row.id, "spy", registry_with(spy))
    assert (result.outcome, result.reason) == (
        DeliveryOutcome.REFUSED, "malformed_notification",
    )
    assert spy.payloads == []


# ============================================================================
# E. Owner isolation
# ============================================================================


async def test_another_owner_cannot_deliver_the_notification(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    spy = SpyAdapter()
    result = await deliver(
        session_factory, note.id, "spy", registry_with(spy), owner=OTHER,
    )
    assert (result.outcome, result.reason) == (
        DeliveryOutcome.REFUSED, "notification_not_found",
    )
    assert spy.payloads == []


async def test_each_owner_delivers_only_their_own(
    session_factory, execution_settings, workspace
) -> None:
    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(
        session_factory, execution_settings, workspace, owner=OTHER,
    )
    local = LocalRecordingAdapter()
    registry = registry_with(local)
    assert (await deliver(session_factory, theirs.id, "local", registry)).reason == (
        "notification_not_found"
    )
    assert (await deliver(
        session_factory, theirs.id, "local", registry, owner=OTHER,
    )).outcome is DeliveryOutcome.DELIVERED
    assert (await deliver(session_factory, mine.id, "local", registry)).outcome is (
        DeliveryOutcome.DELIVERED
    )
    assert {p.notification_id for p in local.delivered} == {mine.id, theirs.id}


# ============================================================================
# F. Failure containment, and nothing changes
# ============================================================================


async def test_an_adapter_that_reports_failure_is_a_failed_result(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    result = await deliver(session_factory, note.id, "failing", registry_with(FailingAdapter()))
    assert result.outcome is DeliveryOutcome.FAILED
    assert result.delivery_key == delivery_key(note.id, "failing")


async def test_an_adapter_that_raises_is_contained_and_its_error_never_leaks(
    session_factory, execution_settings, workspace, caplog
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    raising = RaisingAdapter()
    with caplog.at_level("DEBUG"):
        result = await deliver(session_factory, note.id, "raising", registry_with(raising))
    assert (result.outcome, result.reason) == (DeliveryOutcome.FAILED, "adapter_error")
    assert raising.calls == 1                      # one attempt, no retry
    assert ERROR_SENTINEL not in repr(result)
    assert ERROR_SENTINEL not in caplog.text
    for record in caplog.records:
        assert ERROR_SENTINEL not in repr(record.__dict__)


async def test_an_adapter_returning_the_wrong_type_is_a_failure(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    result = await deliver(
        session_factory, note.id, "wrong_type", registry_with(WrongTypeAdapter()),
    )
    assert (result.outcome, result.reason) == (
        DeliveryOutcome.FAILED, "adapter_invalid_status",
    )


async def test_an_adapter_that_hangs_times_out(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    result = await deliver(
        session_factory, note.id, "hanging", registry_with(HangingAdapter()),
        timeout_seconds=0.05,
    )
    assert (result.outcome, result.reason) == (DeliveryOutcome.FAILED, "adapter_timeout")


async def test_delivery_changes_no_task_notification_or_execution_state(
    session_factory, execution_settings, workspace
) -> None:
    met = await met_notification(session_factory, execution_settings, workspace)
    failed = await failed_notification(session_factory, execution_settings, workspace)
    before = await snapshot(session_factory)

    registry = registry_with(
        LocalRecordingAdapter(), RaisingAdapter(), FailingAdapter(), WrongTypeAdapter(),
    )
    for note in (met, failed):
        for name in ("local", "local", "raising", "failing", "wrong_type", "telegram"):
            await deliver(session_factory, note.id, name, registry)
        await deliver(session_factory, note.id, "local", registry, owner=OTHER)

    assert await snapshot(session_factory) == before
    # Delivered is not read: both are still the owner's unread notifications.
    async with session_factory() as session:
        assert {n.id for n in await NotificationService(session, OWNER).unread()} == {
            met.id, failed.id,
        }


async def test_delivery_never_runs_the_task_again(
    session_factory, execution_settings, workspace
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)
    await deliver(session_factory, note.id, "local", registry_with(LocalRecordingAdapter()))
    async with session_factory() as session:
        task = (await session.execute(select(Task).where(Task.id == note.task_id))).scalars().one()
        executions = (await session.execute(select(func.count()).select_from(Execution))).scalar()
    assert task.state is TaskState.COMPLETED and task.next_run_at is None
    assert executions == 1


# ============================================================================
# G. The local adapter makes no network call
# ============================================================================


async def test_the_local_adapter_delivers_with_the_network_unavailable(
    session_factory, execution_settings, workspace, monkeypatch
) -> None:
    note = await met_notification(session_factory, execution_settings, workspace)

    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    local = LocalRecordingAdapter()
    result = await deliver(session_factory, note.id, "local", registry_with(local))
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert len(local.delivered) == 1
