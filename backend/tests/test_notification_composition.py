"""Stage 6K: the one process-lifetime notification delivery composition.

What is real: the 6G/6H path that produces notifications, the 6I service and
registry, the 6J adapter, the foundation's `BotApiSender`, `NetworkPolicy` and
`SecureHttpClient`. What is replaced: the httpx transport and DNS resolver
inside the composed sender (so no request leaves the machine), and the
module's cached registry, which each test resets so tests never share one.

No Telegram credential exists here; every token is a synthetic sentinel, and
no live Telegram request was made.
"""

import asyncio
import json
import logging
import socket
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from app.composition import notification_delivery as composition
from app.composition.notification_delivery import (
    build_delivery_registry,
    get_delivery_registry,
    notification_delivery_service,
)
from app.delivery.contract import DeliveryOutcome, DeliveryPayload, delivery_key
from app.delivery.local import LocalRecordingAdapter
from app.delivery.registry import AdapterRegistry
from app.delivery.service import NotificationDeliveryService
from app.tasks.models import LOCAL_OWNER_ID, TaskNotificationKind
from app.tasks.notifications import NotificationService
from app.telegram.client import BotApiSender
from app.telegram.notifier import TelegramNotificationAdapter
from tests.support.stub_transport import StubTransport
from tests.test_delivery import OTHER, met_notification, snapshot

pytestmark = pytest.mark.anyio

TOKEN = "999000111:TOKEN-SENTINEL-6k_Hn3p"
SECRET = "TOKEN-SENTINEL-6k_Hn3p"           # the part that survives URL-encoding
CHAT_ID = 424242
URL = "https://api.telegram.org/bot%s/sendMessage" % quote(TOKEN, safe="")


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_composition(monkeypatch):
    """Every test starts with no process registry built."""
    monkeypatch.setattr(composition, "_registry", None)


@pytest.fixture
def tg_settings(execution_settings):
    execution_settings.TELEGRAM_BOT_TOKEN = TOKEN
    execution_settings.TELEGRAM_ALLOWED_CHAT_ID = str(CHAT_ID)
    return execution_settings


@pytest.fixture
def composed(monkeypatch, tg_settings):
    """The process composition, built from these settings on first access."""
    monkeypatch.setattr(composition, "get_settings", lambda: tg_settings)
    return tg_settings


def stub_network(registry, transport=None):
    """Swap the composed sender's transport and resolver; the policy is kept."""
    transport = transport if transport is not None else StubTransport(
        payload={"ok": True, "result": {"message_id": 1}}
    )
    sender = registry.get("telegram")._sender
    sender._client._transport = transport
    sender._client._resolve = lambda host, port: [(2, 1, 6, "", ("149.154.167.220", 443))]
    return transport


def leaks(text):
    return SECRET in text or "sendMessage" in text or "/bot" in text


async def deliver_via_composition(session_factory, notification_id, adapter="telegram",
                                  owner=LOCAL_OWNER_ID):
    """One explicit delivery, as a production caller would make it: its own
    session, its own 6I service, the process's one registry."""
    async with session_factory() as session:
        service = notification_delivery_service(session, owner)
        result = await service.deliver(notification_id, adapter)
        assert not session.new and not session.dirty and not session.deleted
        await session.rollback()
        return result


# ============================================================================
# A. One registry, one adapter, for the life of the process
# ============================================================================


def test_one_registry_is_built_and_reused(composed, monkeypatch) -> None:
    builds = []
    real = composition.build_delivery_registry

    def counting(settings):
        builds.append(settings)
        return real(settings)

    monkeypatch.setattr(composition, "build_delivery_registry", counting)
    registries = {id(get_delivery_registry()) for _ in range(25)}
    assert len(builds) == 1 and len(registries) == 1


def test_one_telegram_adapter_is_constructed_and_reused(composed, monkeypatch) -> None:
    constructed = []
    real = composition.TelegramNotificationAdapter

    def counting(settings):
        adapter = real(settings)
        constructed.append(adapter)
        return adapter

    monkeypatch.setattr(composition, "TelegramNotificationAdapter", counting)
    adapters = {id(get_delivery_registry().get("telegram")) for _ in range(25)}
    assert len(constructed) == 1
    assert adapters == {id(constructed[0])}


async def test_every_delivery_service_shares_the_one_registry_and_adapter(
    composed, session_factory
) -> None:
    services = []
    for _ in range(5):
        async with session_factory() as session:
            services.append(notification_delivery_service(session))
    assert len({id(s) for s in services}) == 5                 # per call: own session
    assert {id(s._registry) for s in services} == {id(get_delivery_registry())}
    assert all(type(s) is NotificationDeliveryService for s in services)


#: Run in a separate interpreter. Spawning Python threads inside the shared
#: test process makes this host's Python 3.9 + SQLite segfault later in the
#: suite (it changes which thread finalizes leftover aiosqlite connections --
#: Known defect 4). Bisected: with this race in-process, the following
#: standing-grant tests crash 3/3; in a subprocess, never.
_THREAD_RACE = """
import json, threading
from app.core.config import Settings
from app.composition import notification_delivery as c

settings = Settings(_env_file=None, GROQ_API_KEY="x",
                    TELEGRAM_BOT_TOKEN="999000111:TOKEN-SENTINEL-6k_Hn3p",
                    TELEGRAM_ALLOWED_CHAT_ID="424242")
c.get_settings = lambda: settings
builds, adapters = [], []
real_build, real_adapter = c.build_delivery_registry, c.TelegramNotificationAdapter

def counting_build(s):
    builds.append(1)
    return real_build(s)

def counting_adapter(s):
    a = real_adapter(s)
    adapters.append(a)
    return a

c.build_delivery_registry = counting_build
c.TelegramNotificationAdapter = counting_adapter
gate = threading.Barrier(16)
seen = []

def worker():
    gate.wait()
    seen.append(id(c.get_delivery_registry()))

threads = [threading.Thread(target=worker) for _ in range(16)]
for t in threads: t.start()
for t in threads: t.join()
print(json.dumps({"builds": len(builds), "adapters": len(adapters),
                  "distinct": len(set(seen)), "seen": len(seen)}))
"""


def test_racing_threads_build_exactly_one_registry_and_one_adapter() -> None:
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _THREAD_RACE], cwd=backend, capture_output=True,
        text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(backend), "LOG_LEVEL": "WARNING"},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert SECRET not in result.stdout + result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome == {"builds": 1, "adapters": 1, "distinct": 1, "seen": 16}


def test_importing_the_module_builds_nothing() -> None:
    """The fixture reset the cache; nothing in the import path rebuilt it."""
    import importlib

    importlib.reload(composition)
    try:
        assert composition._registry is None
    finally:
        importlib.reload(composition)


# ============================================================================
# B. Telegram is registered only when configured; otherwise fail closed
# ============================================================================


def test_a_configured_telegram_is_registered_and_the_registry_sealed(tg_settings) -> None:
    registry = build_delivery_registry(tg_settings)
    assert registry.names() == ("telegram",)
    assert registry.sealed
    adapter = registry.get("telegram")
    assert type(adapter) is TelegramNotificationAdapter and adapter.configured
    assert type(adapter._sender) is BotApiSender


@pytest.mark.parametrize("token,chat", [
    ("", str(CHAT_ID)),                         # missing token
    (TOKEN, ""),                                # missing chat id
    ("", ""),                                   # nothing configured
    ("short", str(CHAT_ID)),                    # implausible token
    ("123456:abc/def_ghijklmn", str(CHAT_ID)),  # token that would reshape the path
    (TOKEN, "abc"),                             # malformed chat id
    (TOKEN, "0"),                               # chat id zero
    (TOKEN, "12.5"),
])
async def test_missing_or_invalid_telegram_configuration_fails_closed(
    execution_settings, session_factory, workspace, monkeypatch, token, chat
) -> None:
    execution_settings.TELEGRAM_BOT_TOKEN = token
    execution_settings.TELEGRAM_ALLOWED_CHAT_ID = chat
    registry = build_delivery_registry(execution_settings)
    assert registry.names() == () and registry.sealed

    monkeypatch.setattr(composition, "get_settings", lambda: execution_settings)
    note = await met_notification(session_factory, execution_settings, workspace)
    result = await deliver_via_composition(session_factory, note.id)
    assert (result.outcome, result.reason) == (DeliveryOutcome.REFUSED, "unknown_adapter")


def test_the_local_recording_adapter_is_not_registered_in_production(tg_settings) -> None:
    registry = build_delivery_registry(tg_settings)
    assert registry.get("local") is None
    assert not any(isinstance(registry.get(n), LocalRecordingAdapter) for n in registry.names())


def test_composition_opens_no_connection(tg_settings, monkeypatch) -> None:
    def no_network(*args, **kwargs):
        raise AssertionError("network access during composition")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    registry = build_delivery_registry(tg_settings)
    # The sender's httpx client is created on its first request, not before.
    assert registry.get("telegram")._sender._client._client is None


def test_the_composed_sender_carries_the_foundation_policy(tg_settings) -> None:
    policy = build_delivery_registry(tg_settings).get("telegram")._sender._client._policy
    assert policy.allowed_hosts == frozenset({"api.telegram.org"})
    assert policy.allowed_methods == frozenset({"POST"})
    assert policy.follow_redirects is False and policy.max_redirects == 0


# ============================================================================
# C. The registry is closed
# ============================================================================


def test_nothing_can_be_added_to_the_composed_registry(composed) -> None:
    registry = get_delivery_registry()
    for adapter in (LocalRecordingAdapter(), TelegramNotificationAdapter(composed)):
        with pytest.raises(RuntimeError):
            registry.register(adapter)
    assert registry.names() == ("telegram",)


def test_an_unconfigured_composition_is_sealed_too(execution_settings, monkeypatch) -> None:
    monkeypatch.setattr(composition, "get_settings", lambda: execution_settings)
    registry = get_delivery_registry()
    assert registry.names() == () and registry.sealed
    with pytest.raises(RuntimeError):
        registry.register(LocalRecordingAdapter())


# ============================================================================
# D. End to end, and why one adapter matters
# ============================================================================


async def test_an_explicit_delivery_reaches_telegram_through_the_composition(
    composed, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    result = await deliver_via_composition(session_factory, note.id)

    assert result.outcome is DeliveryOutcome.DELIVERED
    assert result.delivery_key == delivery_key(note.id, "telegram")
    assert transport.connections == [URL] and transport.methods == ["POST"]
    body = json.loads(transport.bodies[0])
    assert sorted(body) == ["chat_id", "text"] and body["chat_id"] == CHAT_ID


async def test_separate_requests_share_one_duplicate_memory(
    composed, session_factory, workspace
) -> None:
    """The reason this stage exists. Each call builds its own 6I service with
    its own session, yet the second, third and fourth are DUPLICATE: they all
    reach the same adapter instance."""
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    outcomes = [
        (await deliver_via_composition(session_factory, note.id)).outcome
        for _ in range(4)
    ]
    assert outcomes == [DeliveryOutcome.DELIVERED] + [DeliveryOutcome.DUPLICATE] * 3
    assert len(transport.connections) == 1


async def test_a_registry_rebuilt_per_call_would_have_delivered_again(
    composed, session_factory, workspace
) -> None:
    """The counterfactual: building per call loses the memory and re-sends.
    This is exactly what the process-lifetime composition prevents."""
    note = await met_notification(session_factory, composed, workspace)
    sent = 0
    for _ in range(2):
        registry = build_delivery_registry(composed)          # rebuilt each time
        transport = stub_network(registry)
        async with session_factory() as session:
            await NotificationDeliveryService(session, LOCAL_OWNER_ID, registry).deliver(
                note.id, "telegram",
            )
            await session.rollback()
        sent += len(transport.connections)
    assert sent == 2


async def test_racing_deliveries_through_the_composed_adapter_send_once(
    composed, session_factory, workspace
) -> None:
    """Racing at the shared part: the one composed adapter.

    Deliberately not five concurrent *sessions*: the test fixture's SQLite is
    a single shared connection (StaticPool), so concurrent sessions corrupt
    its transaction state -- the documented 6F limitation. The full concurrent
    path, with separate sessions on real connections, is proven against live
    PostgreSQL in this stage's Docker verification."""
    note = await met_notification(session_factory, composed, workspace)
    registry = get_delivery_registry()
    transport = stub_network(registry)
    payload = DeliveryPayload(
        notification_id=note.id, task_id=note.task_id, kind=note.kind,
        check_number=note.check_number, created_at=note.created_at,
        delivery_key=delivery_key(note.id, "telegram"),
    )
    adapters = [get_delivery_registry().get("telegram") for _ in range(5)]
    assert len({id(a) for a in adapters}) == 1
    statuses = await asyncio.gather(*(a.deliver(payload) for a in adapters))
    assert sorted(s.value for s in statuses) == ["delivered"] + ["duplicate"] * 4
    assert len(transport.connections) == 1
    # And a later explicit delivery through the 6I path sees the same memory.
    after = await deliver_via_composition(session_factory, note.id)
    assert after.outcome is DeliveryOutcome.DUPLICATE


async def test_ownership_and_refusals_are_6is_unchanged(
    composed, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    other = await deliver_via_composition(session_factory, note.id, owner=OTHER)
    missing = await deliver_via_composition(session_factory, uuid.uuid4())
    malformed = await deliver_via_composition(session_factory, str(note.id))
    unknown = await deliver_via_composition(session_factory, note.id, adapter="local")
    assert other.reason == missing.reason == "notification_not_found"
    assert malformed.reason == "malformed_notification"
    assert unknown.reason == "unknown_adapter"
    assert transport.connections == []


async def test_the_default_owner_is_the_single_local_owner(
    composed, session_factory, workspace
) -> None:
    async with session_factory() as session:
        service = notification_delivery_service(session)
        assert service._notifications._owner_id == LOCAL_OWNER_ID


async def test_a_failure_is_one_attempt_and_changes_nothing(
    composed, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    before = await snapshot(session_factory)
    transport = stub_network(get_delivery_registry(), StubTransport(status_code=500, payload={}))
    result = await deliver_via_composition(session_factory, note.id)
    assert result.outcome is DeliveryOutcome.FAILED
    assert len(transport.connections) == 1                    # no automatic retry
    assert await snapshot(session_factory) == before


async def test_delivery_never_marks_read_or_changes_any_state(
    composed, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    before = await snapshot(session_factory)
    stub_network(get_delivery_registry())
    for _ in range(3):
        await deliver_via_composition(session_factory, note.id)
    assert await snapshot(session_factory) == before
    async with session_factory() as session:
        [unread] = await NotificationService(session, LOCAL_OWNER_ID).unread()
        assert unread.id == note.id and unread.read_at is None


# ============================================================================
# E. No secret in what composition returns or logs
# ============================================================================


async def test_no_token_in_composition_objects_or_logs(
    composed, session_factory, workspace, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        registry = get_delivery_registry()
        note = await met_notification(session_factory, composed, workspace)
        stub_network(registry)
        result = await deliver_via_composition(session_factory, note.id)
    assert not leaks(repr(result)) and not leaks(repr(registry.names()))
    adapter = registry.get("telegram")
    assert not any(TOKEN in repr(v) for v in vars(adapter).values() if not isinstance(v, BotApiSender))
    mai = [r for r in caplog.records
           if not r.name.startswith(("httpx", "httpcore", "aiosqlite", "asyncio"))]
    for record in mai:
        assert not leaks(record.getMessage() + repr(record.__dict__))
    composed_log = [r for r in mai if r.name == "app.composition.notification_delivery"]
    assert [r.adapters for r in composed_log] == ["telegram"]


async def test_nothing_reaches_the_real_log_output_at_debug(
    composed, session_factory, workspace, capsys
) -> None:
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved = (list(root.handlers), root.level,
             {n: logging.getLogger(n).level for n in ("httpx", "httpcore")})
    try:
        configure_logging("DEBUG", "json")
        registry = get_delivery_registry()
        note = await met_notification(session_factory, composed, workspace)
        stub_network(registry)
        await deliver_via_composition(session_factory, note.id)
        out = capsys.readouterr().out
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        for name, level in saved[2].items():
            logging.getLogger(name).setLevel(level)
    assert "Notification delivery composed" in out          # logging was live
    assert not leaks(out) and TOKEN not in out and "api.telegram.org" not in out
