"""Stage 6L: a person asks, over HTTP, for one notification to be delivered.

Through the real app: the route, `deps.py`, the 6K composition, the 6I
service and the 6J adapter with the foundation's `BotApiSender`. Notifications
come from the real 6G/6H runtime path. What is replaced: the composed sender's
httpx transport and DNS resolver (no request leaves the machine), the module's
cached registry (reset per test), and the settings the app and composition
read (never the environment, so no real credential can be picked up).

Every token is a synthetic sentinel; no live Telegram request was made.
"""

import asyncio
import builtins
import importlib
import json
import logging
import socket
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.composition import notification_delivery as composition
from app.composition.notification_delivery import get_delivery_registry
from app.core.config import get_settings
from app.database.metadata import Base
from app.database.session import configure_sqlite, get_db_session
from app.delivery.contract import delivery_key
from app.delivery.service import NotificationDeliveryService
from app.llm.factory import get_llm_provider
from app.main import create_app
from app.tasks.models import LOCAL_OWNER_ID
from app.tasks.notifications import NotificationService
from app.telegram import client as telegram_client
from app.telegram import notifier as telegram_notifier
from tests.support.stub_transport import StubTransport
from tests.test_delivery import OTHER, met_notification, snapshot
from tests.test_notification_composition import stub_network

pytestmark = pytest.mark.anyio

TOKEN = "999000111:TOKEN-SENTINEL-6l_Qz8w"
SECRET = "TOKEN-SENTINEL-6l_Qz8w"           # the part that survives URL-encoding
CHAT_ID = 515151
URL = "https://api.telegram.org/bot%s/sendMessage" % quote(TOKEN, safe="")
DB_SECRET = "postgresql+asyncpg://mai:db-password-sentinel-6l@db:5432/mai"
PATH_SECRET = "/srv/mai/internal/path-sentinel-6l.py"


def route(notification_id) -> str:
    return f"/api/task-notifications/{notification_id}/deliveries"


def leaks(text: str) -> bool:
    return any(s in text for s in (SECRET, "sendMessage", "/bot", "db-password-sentinel",
                                   "path-sentinel", "Traceback"))


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch, execution_settings):
    """Every test starts with no process registry, and the composition reads
    these settings -- never the environment -- unless a test configures it."""
    monkeypatch.setattr(composition, "_registry", None)
    monkeypatch.setattr(composition, "get_settings", lambda: execution_settings)
    return execution_settings


@pytest.fixture
def composed(monkeypatch, execution_settings):
    execution_settings.TELEGRAM_BOT_TOKEN = TOKEN
    execution_settings.TELEGRAM_ALLOWED_CHAT_ID = str(CHAT_ID)
    monkeypatch.setattr(composition, "get_settings", lambda: execution_settings)
    return execution_settings


@asynccontextmanager
async def app_client(monkeypatch, app_settings, factory, fake_provider, raise_app_exceptions=True):
    """The real app, built from `app_settings`, on `factory`'s database."""
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
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture
async def http(monkeypatch, execution_settings, session_factory, fake_provider):
    async with app_client(monkeypatch, execution_settings, session_factory, fake_provider) as c:
        yield c


async def post(http, notification_id, adapter="telegram", **kwargs):
    kwargs.setdefault("json", {"adapter": adapter})
    return await http.post(route(notification_id), **kwargs)


# ============================================================================
# A. A person asks; the existing layers deliver
# ============================================================================


async def test_a_person_delivers_a_notification_through_telegram(
    composed, http, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())

    response = await post(http, note.id)

    assert response.status_code == 200
    assert response.json() == {
        "outcome": "delivered", "reason": None, "adapter": "telegram",
        "notification_id": str(note.id), "delivery_key": delivery_key(note.id, "telegram"),
    }
    assert transport.connections == [URL] and transport.methods == ["POST"]
    body = json.loads(transport.bodies[0])
    assert sorted(body) == ["chat_id", "text"] and body["chat_id"] == CHAT_ID


async def test_repeating_the_request_is_duplicate_and_sends_once(
    composed, http, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    outcomes = [(await post(http, note.id)).json()["outcome"] for _ in range(4)]
    assert outcomes == ["delivered", "duplicate", "duplicate", "duplicate"]
    assert len(transport.connections) == 1


async def test_delivery_changes_nothing_and_marks_nothing_read(
    composed, http, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    stub_network(get_delivery_registry())
    before = await snapshot(session_factory)
    assert (await post(http, note.id)).status_code == 200
    assert await snapshot(session_factory) == before
    async with session_factory() as session:
        assert (await NotificationService(session, LOCAL_OWNER_ID).get(note.id)).read_at is None


async def test_the_route_goes_through_the_6i_service_and_the_6k_registry(
    composed, http, session_factory, workspace, monkeypatch
) -> None:
    """One 6I `deliver` call per request, on a service holding the process's
    one registry and the server's owner. The request builds no adapter and no
    sender: both already exist inside the composition."""
    note = await met_notification(session_factory, composed, workspace)
    registry = get_delivery_registry()
    stub_network(registry)
    calls, owners, built = [], [], []
    real_init, real_deliver = NotificationDeliveryService.__init__, NotificationDeliveryService.deliver

    def init(self, session, owner_id, registry, *a, **k):
        owners.append(owner_id)
        real_init(self, session, owner_id, registry, *a, **k)

    async def deliver(self, notification_id, adapter_name):
        calls.append((self._registry, notification_id, adapter_name))
        return await real_deliver(self, notification_id, adapter_name)

    monkeypatch.setattr(NotificationDeliveryService, "__init__", init)
    monkeypatch.setattr(NotificationDeliveryService, "deliver", deliver)
    for cls in (telegram_notifier.TelegramNotificationAdapter, telegram_client.BotApiSender):
        monkeypatch.setattr(cls, "__init__", lambda *a, _c=cls, **k: built.append(_c))

    for _ in range(2):
        assert (await post(http, note.id)).status_code == 200

    assert [(c[0] is registry, c[1], c[2]) for c in calls] == [(True, note.id, "telegram")] * 2
    assert owners == [LOCAL_OWNER_ID, LOCAL_OWNER_ID]
    assert built == []
    assert get_delivery_registry() is registry


async def test_the_route_exists_with_execution_switched_off(
    monkeypatch, execution_settings, session_factory, fake_provider
) -> None:
    """Ungated: delivery is not tool execution, so the execution switch does
    not govern it. Unconfigured, it is inert (see the refusals below)."""
    switched_off = execution_settings.model_copy(update={"EXECUTION_ENABLED": False})
    async with app_client(monkeypatch, switched_off, session_factory, fake_provider) as http:
        assert not any(r.path.startswith("/api/executions") for r in http._transport.app.routes)
        response = await post(http, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "unknown_adapter"


# ============================================================================
# B. Refusals: nothing is sent
# ============================================================================


async def test_a_missing_notification_is_404(composed, http) -> None:
    transport = stub_network(get_delivery_registry())
    response = await post(http, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "notification_not_found"
    assert transport.connections == []


async def test_another_owners_notification_is_indistinguishable_from_a_missing_one(
    composed, http, session_factory, workspace
) -> None:
    theirs = await met_notification(session_factory, composed, workspace, owner=OTHER)
    transport = stub_network(get_delivery_registry())
    before = await snapshot(session_factory)

    other = await post(http, theirs.id)
    missing = await post(http, uuid.uuid4())

    assert other.status_code == missing.status_code == 404
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}
    assert strip(other) == strip(missing)
    assert transport.connections == []
    assert await snapshot(session_factory) == before


async def test_an_unknown_adapter_is_404(composed, http, session_factory, workspace) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    for name in ("email", "local", "push", "sms"):
        response = await post(http, note.id, adapter=name)
        assert response.status_code == 404, name
        assert response.json()["error"]["message"] == "unknown_adapter"
    assert transport.connections == []


async def test_an_unconfigured_telegram_is_the_same_as_an_unknown_one(
    unconfigured, http, session_factory, workspace, monkeypatch
) -> None:
    note = await met_notification(session_factory, unconfigured, workspace)

    def no_network(*args, **kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    telegram = await post(http, note.id, adapter="telegram")
    unknown = await post(http, note.id, adapter="email")
    assert telegram.status_code == unknown.status_code == 404
    assert telegram.json()["error"]["message"] == unknown.json()["error"]["message"] == "unknown_adapter"
    assert get_delivery_registry().names() == ()


async def test_malformed_requests_are_422_and_send_nothing(
    composed, http, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    bad = [
        await http.post(route("not-a-uuid"), json={"adapter": "telegram"}),
        await http.post(route("1 OR 1=1"), json={"adapter": "telegram"}),
        await http.post(route(note.id)),                                   # no body
        await http.post(route(note.id), json={}),
        await http.post(route(note.id), json={"adapter": ""}),
        await http.post(route(note.id), json={"adapter": None}),
        await http.post(route(note.id), json={"adapter": 1}),
        await http.post(route(note.id), json={"adapter": ["telegram"]}),
        await http.post(route(note.id), json={"adapter": {"name": "telegram"}}),
        await http.post(route(note.id), json={"adapter": "t" * 33}),
        await http.post(route(note.id), json=["telegram"]),
        await http.post(route(note.id), content=b"{not json", headers={"content-type": "application/json"}),
    ]
    assert [r.status_code for r in bad] == [422] * len(bad)
    assert {r.json()["error"]["code"] for r in bad} == {"validation_error"}
    assert transport.connections == []


async def test_only_post_is_offered(composed, http) -> None:
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        response = await http.request(method, route(uuid.uuid4()))
        assert response.status_code == 405, method


# ============================================================================
# C. The caller controls the notification id and the channel name. Nothing else.
# ============================================================================


@pytest.mark.parametrize("field,value", [
    ("owner_id", str(OTHER)),
    ("chat_id", 666),
    ("telegram_chat_id", "666"),
    ("bot_token", "123:ATTACKER"),
    ("token", "123:ATTACKER"),
    ("url", "https://attacker.test/collect"),
    ("host", "attacker.test"),
    ("recipient", "@attacker"),
    ("text", "attacker text"),
    ("message", "attacker text"),
    ("task_id", str(uuid.uuid4())),
    ("notification_id", str(uuid.uuid4())),
    ("delivery_key", "forged"),
])
async def test_any_field_but_the_adapter_is_refused(
    composed, http, session_factory, workspace, field, value
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    response = await http.post(route(note.id), json={"adapter": "telegram", field: value})
    assert response.status_code == 422
    assert transport.connections == []


async def test_query_parameters_and_headers_cannot_redirect_anything(
    composed, http, session_factory, workspace
) -> None:
    """Values a caller might try outside the body are ignored: the one
    configured chat, host and token are used, for the server's owner."""
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    response = await http.post(
        route(note.id),
        json={"adapter": "telegram"},
        params={"owner_id": str(OTHER), "chat_id": "666", "url": "https://attacker.test/",
                "token": "123:ATTACKER", "adapter": "email"},
        headers={"X-Owner-Id": str(OTHER), "X-Telegram-Chat-Id": "666",
                 "X-Forwarded-Host": "attacker.test", "Host": "attacker.test"},
    )
    assert response.status_code == 200 and response.json()["adapter"] == "telegram"
    assert transport.connections == [URL]
    assert json.loads(transport.bodies[0])["chat_id"] == CHAT_ID
    assert "ATTACKER" not in transport.connections[0] and "attacker" not in transport.connections[0]


@pytest.mark.parametrize("name", [
    "__import__('os').system('id')",
    "app.telegram.notifier.TelegramNotificationAdapter",
    "app.telegram.notifier",
    "os.system",
    "../telegram",
    "telegram/../../etc/passwd",
    "telegram;rm -rf /",
    "telegram\x00",
    "telеgram",                   # Cyrillic "e"
    "telegram.__class__",
    "__class__",
    "_sender",
    "https://attacker.test/",
])
async def test_adapter_names_are_data_and_never_reach_code(
    composed, http, session_factory, workspace, monkeypatch, name
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    imported = []
    real_import, real_import_module = builtins.__import__, importlib.import_module
    with pytest.MonkeyPatch.context() as spy:
        spy.setattr(builtins, "__import__",
                    lambda n, *a, **k: (imported.append(n), real_import(n, *a, **k))[1])
        spy.setattr(importlib, "import_module",
                    lambda n, *a, **k: (imported.append(n), real_import_module(n, *a, **k))[1])
        response = await post(http, note.id, adapter=name)

    assert response.status_code in (404, 422), name
    assert transport.connections == []
    assert name not in imported and name.strip().lower() not in imported
    assert get_delivery_registry().names() == ("telegram",)


async def test_the_one_normalisation_is_6is_strip_and_lowercase(
    composed, http, session_factory, workspace
) -> None:
    """Not a bypass: `TELEGRAM` and ` telegram ` name the same registered
    channel, by 6I's documented canonicalisation, and share its duplicate
    memory."""
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    outcomes = [(await post(http, note.id, adapter=n)).json()
                for n in ("TELEGRAM", " telegram ", "Telegram")]
    assert [o["outcome"] for o in outcomes] == ["delivered", "duplicate", "duplicate"]
    assert {o["adapter"] for o in outcomes} == {"telegram"}
    assert len(transport.connections) == 1


async def test_a_cross_site_form_post_cannot_trigger_a_delivery(
    composed, http, session_factory, workspace
) -> None:
    """A page on another origin can send a form post without a preflight.
    The route reads only a JSON body, so such a post is refused unsent."""
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())
    for content_type, body in (
        ("application/x-www-form-urlencoded", b"adapter=telegram"),
        ("text/plain", b'{"adapter": "telegram"}'),
        ("multipart/form-data; boundary=x",
         b'--x\r\nContent-Disposition: form-data; name="adapter"\r\n\r\ntelegram\r\n--x--\r\n'),
    ):
        response = await http.post(route(note.id), content=body,
                                   headers={"content-type": content_type})
        assert response.status_code == 422, content_type
    assert transport.connections == []


async def test_a_disallowed_origin_gets_no_cors_permission(composed, http) -> None:
    response = await http.options(route(uuid.uuid4()), headers={
        "Origin": "https://attacker.test",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert response.headers.get("access-control-allow-origin") not in ("https://attacker.test", "*")


# ============================================================================
# D. Failures are codes; nothing internal leaves
# ============================================================================


async def test_a_channel_failure_is_502_with_a_code(
    composed, http, session_factory, workspace
) -> None:
    note = await met_notification(session_factory, composed, workspace)
    stub_network(get_delivery_registry(), StubTransport(status_code=500, payload={"ok": False}))
    before = await snapshot(session_factory)
    response = await post(http, note.id)
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "delivery_failed"
    assert await snapshot(session_factory) == before


async def test_a_failure_is_not_remembered_as_delivered(
    composed, http, session_factory, workspace
) -> None:
    """6J records only accepted deliveries, so a person can ask again after a
    failure and it is sent, not answered DUPLICATE."""
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry(), StubTransport(responses=[
        {"status_code": 500, "payload": {"ok": False}},
        {"status_code": 200, "payload": {"ok": True, "result": {"message_id": 2}}},
    ]))
    assert (await post(http, note.id)).status_code == 502
    assert (await post(http, note.id)).json()["outcome"] == "delivered"
    assert len(transport.connections) == 2


def captured(caplog) -> str:
    return " ".join(r.getMessage() + " " + repr(r.__dict__) for r in caplog.records)


async def test_delivery_errors_expose_no_token_url_credentials_path_or_trace(
    composed, http, session_factory, workspace, caplog
) -> None:
    """An adapter that raises, and a transport that raises inside the real
    sender, each carrying a token, the URL, a database URL and a path. The
    response is a code, and no log record holds any of it -- not even as
    attached exception info."""
    note = await met_notification(session_factory, composed, workspace)
    registry = get_delivery_registry()
    stub_network(registry)
    poisoned = f"boom {TOKEN} {URL} {DB_SECRET} {PATH_SECRET}"
    caplog.set_level(logging.DEBUG)

    async def raising(payload):
        raise RuntimeError(poisoned)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry.get("telegram"), "deliver", raising)
        adapter_raised = await post(http, note.id)
    stub_network(registry, StubTransport(raise_error=RuntimeError(poisoned)))
    transport_raised = await post(http, note.id)

    assert [adapter_raised.status_code, transport_raised.status_code] == [502, 502]
    assert adapter_raised.json()["error"]["message"] == "adapter_error"
    assert transport_raised.json()["error"]["message"] == "delivery_failed"
    for response in (adapter_raised, transport_raised):
        assert not leaks(response.text), response.text
    assert not leaks(captured(caplog))
    assert not [r for r in caplog.records if r.exc_info]


async def test_an_unexpected_internal_error_is_the_apps_generic_500(
    composed, session_factory, workspace, monkeypatch, fake_provider
) -> None:
    """Outside 6I's containment (here the owner-scoped read itself fails),
    the app's existing handler answers: a fixed message, nothing internal.
    (That handler logs the traceback server-side, as it does for every
    route; the response carries none of it.)"""
    note = await met_notification(session_factory, composed, workspace)
    transport = stub_network(get_delivery_registry())

    async def broken_get(self, notification_id):
        raise RuntimeError(f"boom {TOKEN} {DB_SECRET} {PATH_SECRET}")

    monkeypatch.setattr(NotificationService, "get", broken_get)
    async with app_client(monkeypatch, composed, session_factory, fake_provider,
                          raise_app_exceptions=False) as http:
        response = await post(http, note.id)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["message"] == "An unexpected error occurred."
    assert not leaks(response.text)
    assert transport.connections == []


# ============================================================================
# E. Concurrency, on separate connections
# ============================================================================


async def test_concurrent_requests_send_exactly_once(
    composed, monkeypatch, fake_provider, workspace, tmp_path
) -> None:
    """Five requests at once, each on its own database connection (a
    file-backed SQLite with a real pool, never the shared in-memory
    connection): exactly one is delivered and one request leaves. Since 6M.1
    the others are answered from the durable record: `duplicate` once it says
    delivered, or 409 `delivery_in_progress` while the winner still holds it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'deliveries.db'}")
    configure_sqlite(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        note = await met_notification(factory, composed, workspace)
        transport = stub_network(get_delivery_registry())
        async with app_client(monkeypatch, composed, factory, fake_provider) as http:
            responses = await asyncio.gather(*(post(http, note.id) for _ in range(5)))
    finally:
        await engine.dispose()

    answers = [
        r.json()["outcome"] if r.status_code == 200 else f"{r.status_code}:{r.json()['error']['message']}"
        for r in responses
    ]
    assert answers.count("delivered") == 1
    assert set(answers) - {"delivered"} <= {"duplicate", "409:delivery_in_progress"}
    assert len(transport.connections) == 1
