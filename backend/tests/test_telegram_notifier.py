"""Stage 6J: the Telegram notification adapter.

What runs for real: the 6G/6H path that produces a notification, the 6I
`NotificationDeliveryService`, the adapter, the foundation's `BotApiSender`
and -- crucially -- the foundation's real `NetworkPolicy` inside a real
`SecureHttpClient`. What is replaced is only the last inch: the httpx
transport (a stub that records requests and never opens a socket) and the DNS
resolver. So "the host is fixed", "redirects are refused" and "loopback is
refused" are proven through the code a real request would take, not asserted
about a mock.

No real Telegram credential exists in this environment, so no live Telegram
request was made by these tests or by this stage. Every credential here is a
synthetic sentinel.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import pytest
from pydantic import ValidationError

from app.delivery.contract import (
    DeliveryOutcome,
    DeliveryPayload,
    DeliveryStatus,
    NotificationAdapter,
    delivery_key,
)
from app.delivery.local import LocalRecordingAdapter
from app.delivery.registry import AdapterRegistry
from app.integrations.errors import NetworkPolicyViolation
from app.tasks.models import TaskNotificationKind
from app.telegram.client import TELEGRAM_API_HOST, TELEGRAM_MESSAGE_LIMIT, BotApiSender
from app.telegram import notifier as notifier_module
from app.telegram.notifier import (
    HEADLINES,
    REMEMBERED_KEYS,
    TelegramNotificationAdapter,
    parse_chat_id,
    render_message,
    token_is_plausible,
)
from tests.support.stub_transport import StubTransport
from tests.test_delivery import (
    OBJECTIVE_SENTINEL,
    OTHER,
    deliver,
    failed_notification,
    met_notification,
    registry_with,
    snapshot,
)

pytestmark = pytest.mark.anyio

#: A synthetic credential. If it appears anywhere but the request URL, it leaked.
TOKEN = "999000111:TOKEN-SENTINEL-6j_x9q2Lm"
#: The part of it that survives URL-encoding. The foundation's sender
#: percent-encodes the token into the path (`:` becomes `%3A`), so a leak
#: check for the raw token alone would miss exactly the form that is logged.
SECRET = "TOKEN-SENTINEL-6j_x9q2Lm"
CHAT_ID = 424242
URL = "https://%s/bot%s/sendMessage" % (TELEGRAM_API_HOST, quote(TOKEN, safe=""))

OK_BODY = {"ok": True, "result": {"message_id": 1}}


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


@pytest.fixture
def tg_settings(execution_settings):
    execution_settings.TELEGRAM_BOT_TOKEN = TOKEN
    execution_settings.TELEGRAM_ALLOWED_CHAT_ID = str(CHAT_ID)
    return execution_settings


def public_resolver(host, port):
    return [(2, 1, 6, "", ("149.154.167.220", 443))]


def resolver_to(address):
    def resolve(host, port):
        return [(2, 1, 6, "", (address, 443))]
    return resolve


def ok_transport(**overrides):
    kwargs = dict(payload=OK_BODY)
    kwargs.update(overrides)
    return StubTransport(**kwargs)


def stack(settings, transport=None, resolve=public_resolver):
    """The real sender and real policy, with only transport and DNS replaced."""
    transport = transport if transport is not None else ok_transport()
    sender = BotApiSender(settings)
    sender._client._transport = transport
    sender._client._resolve = resolve
    return TelegramNotificationAdapter(settings, sender=sender), sender, transport


def make_payload(adapter_name="telegram", **overrides):
    notification_id = overrides.pop("notification_id", uuid.uuid4())
    fields = dict(
        notification_id=notification_id,
        task_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        kind=TaskNotificationKind.CONDITION_MET,
        check_number=3,
        created_at=datetime(2026, 10, 1, 14, 3, 9, tzinfo=timezone.utc),
        delivery_key=delivery_key(notification_id, adapter_name),
    )
    fields.update(overrides)
    return DeliveryPayload(**fields)


def mai_records(caplog):
    """Records from Mai's own loggers. In production `httpx`/`httpcore` are
    pinned to WARNING by `configure_logging`; the test environment does not
    apply that pin, so their records are covered separately, under the real
    configuration, by the `configure_logging` tests below."""
    return [r for r in caplog.records
            if not r.name.startswith(("httpx", "httpcore", "aiosqlite", "asyncio"))]


def leaks(text):
    return SECRET in text or "sendMessage" in text or "/bot" in text


class SpySender:
    def __init__(self, error=None):
        self.calls = []
        self._error = error

    async def send_text(self, chat_id, text):
        self.calls.append((chat_id, text))
        if self._error is not None:
            raise self._error


# ============================================================================
# A. Registration through the 6I registry
# ============================================================================


def test_the_adapter_registers_through_the_existing_registry(tg_settings) -> None:
    adapter = TelegramNotificationAdapter(tg_settings)
    registry = AdapterRegistry()
    registry.register(LocalRecordingAdapter())
    registry.register(adapter)
    assert registry.names() == ("local", "telegram")
    assert registry.get("telegram") is adapter
    assert registry.get(" TELEGRAM ") is adapter
    assert isinstance(adapter, NotificationAdapter) and adapter.name == "telegram"


def test_a_second_telegram_adapter_cannot_replace_the_first(tg_settings) -> None:
    first = TelegramNotificationAdapter(tg_settings)
    registry = registry_with(first)
    with pytest.raises(ValueError):
        registry.register(TelegramNotificationAdapter(tg_settings))
    assert registry.get("telegram") is first
    registry.seal()
    with pytest.raises(RuntimeError):
        registry.register(LocalRecordingAdapter())


# ============================================================================
# B. Configuration fails closed
# ============================================================================


@pytest.mark.parametrize("token", ["", "   ", None, "short", "has space inside:token1234",
                                   "123456:abc/def_ghijklmn", "123456:abc?x=1_ghijklmn",
                                   "123456:abc#frag_ghijklmn", "x" * 201])
async def test_a_missing_or_malformed_token_sends_nothing(tg_settings, token) -> None:
    tg_settings.TELEGRAM_BOT_TOKEN = token if token is not None else ""
    spy = SpySender()
    adapter = TelegramNotificationAdapter(tg_settings, sender=spy)
    assert adapter.configured is False
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert spy.calls == []


@pytest.mark.parametrize("chat_id", ["", "  ", "abc", "0", "-0", "1_0", "12.5", "1e3",
                                     "+5", "--5", "5 5", "9" * 21, "٣٤"])
async def test_a_missing_or_malformed_chat_id_sends_nothing(tg_settings, chat_id) -> None:
    tg_settings.TELEGRAM_ALLOWED_CHAT_ID = chat_id
    spy = SpySender()
    adapter = TelegramNotificationAdapter(tg_settings, sender=spy)
    assert adapter.configured is False
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert spy.calls == []


def test_valid_chat_ids_and_tokens_are_accepted() -> None:
    assert [parse_chat_id(v) for v in ("424242", "-1001234567890", " 77 ")] == [
        424242, -1001234567890, 77,
    ]
    assert parse_chat_id(None) is None and parse_chat_id(5) is None
    assert token_is_plausible(TOKEN) and not token_is_plausible(None)


async def test_unconfigured_creates_no_sender_and_makes_no_request(
    execution_settings, monkeypatch
) -> None:
    execution_settings.TELEGRAM_BOT_TOKEN = ""
    execution_settings.TELEGRAM_ALLOWED_CHAT_ID = ""
    built = []
    monkeypatch.setattr(notifier_module, "BotApiSender", lambda s: built.append(s))
    adapter = TelegramNotificationAdapter(execution_settings)
    assert built == [] and adapter.configured is False
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED


def test_the_adapter_can_be_built_where_no_event_loop_exists(tg_settings) -> None:
    """Regression. On Python 3.9 a lock made in `__init__` binds to
    `get_event_loop()`, which raises once any earlier `asyncio.run()` has
    cleared the loop -- so building the adapter from synchronous code failed
    depending on test order. This sets up exactly that state."""
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = None
    asyncio.set_event_loop(None)
    try:
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop()            # the state really is "no loop"
        adapter = TelegramNotificationAdapter(tg_settings)
        assert adapter.configured
    finally:
        asyncio.set_event_loop(previous)


async def test_an_adapter_built_without_a_loop_works_once_one_is_running(tg_settings) -> None:
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = None
    asyncio.set_event_loop(None)
    try:
        adapter, _, transport = stack(tg_settings)
    finally:
        asyncio.set_event_loop(previous)
    payload = make_payload()
    statuses = await asyncio.gather(*(adapter.deliver(payload) for _ in range(4)))
    assert sorted(s.value for s in statuses) == ["delivered", "duplicate", "duplicate", "duplicate"]
    assert len(transport.connections) == 1


async def test_a_configured_adapter_keeps_neither_settings_nor_token(tg_settings) -> None:
    adapter = TelegramNotificationAdapter(tg_settings)
    assert adapter.configured
    held = [repr(v) for v in vars(adapter).values()]
    assert not any(TOKEN in h for h in held)
    assert not any(v is tg_settings for v in vars(adapter).values())


# ============================================================================
# C. The message
# ============================================================================


def test_the_message_is_exactly_this_for_a_met_condition() -> None:
    assert render_message(make_payload()) == (
        "Mai notification\n"
        "Monitoring condition met.\n"
        "Task: 11111111-2222-3333-4444-555555555555\n"
        "Check: 3\n"
        "Time: 2026-10-01 14:03:09 UTC"
    )


def test_the_message_is_exactly_this_for_a_failure() -> None:
    payload = make_payload(kind=TaskNotificationKind.MONITORING_FAILED, check_number=0)
    assert render_message(payload) == (
        "Mai notification\n"
        "Monitoring stopped after repeated failures.\n"
        "Task: 11111111-2222-3333-4444-555555555555\n"
        "Check: 0\n"
        "Time: 2026-10-01 14:03:09 UTC"
    )


def test_the_time_is_utc_whatever_the_zone_and_naive_means_utc() -> None:
    expected = "Time: 2026-10-01 14:03:09 UTC"
    ist = timezone(timedelta(hours=5, minutes=30))
    for created in (
        datetime(2026, 10, 1, 19, 33, 9, tzinfo=ist),
        datetime(2026, 10, 1, 14, 3, 9, tzinfo=timezone.utc),
        datetime(2026, 10, 1, 14, 3, 9),
    ):
        assert render_message(make_payload(created_at=created)).endswith(expected)


def test_every_notification_kind_has_its_own_headline() -> None:
    assert set(HEADLINES) == set(TaskNotificationKind)
    assert len(set(HEADLINES.values())) == len(HEADLINES)
    for kind in TaskNotificationKind:
        assert HEADLINES[kind] in render_message(make_payload(kind=kind))


def test_a_kind_without_a_headline_gets_a_generic_line_not_a_failure(monkeypatch) -> None:
    """New kinds are expected to get their own headline (the test above fails
    until they do), but one that slips through is delivered, not dropped."""
    monkeypatch.delitem(HEADLINES, TaskNotificationKind.CONDITION_MET)
    text = render_message(make_payload(kind=TaskNotificationKind.CONDITION_MET))
    assert text.splitlines()[1] == "Monitoring update."
    assert text.splitlines()[0] == "Mai notification"


async def test_a_kind_without_a_headline_is_still_delivered(tg_settings, monkeypatch) -> None:
    monkeypatch.delitem(HEADLINES, TaskNotificationKind.MONITORING_FAILED)
    adapter, _, transport = stack(tg_settings)
    payload = make_payload(kind=TaskNotificationKind.MONITORING_FAILED)
    assert await adapter.deliver(payload) is DeliveryStatus.DELIVERED
    assert json.loads(transport.bodies[0])["text"].splitlines()[1] == "Monitoring update."


def test_the_message_is_deterministic() -> None:
    payload = make_payload()
    assert {render_message(payload) for _ in range(5)} == {render_message(payload)}


def test_the_message_is_bounded_by_deterministic_truncation() -> None:
    huge = make_payload(check_number=10 ** 4000)           # 4001 digits
    text = render_message(huge)
    assert len(text) == TELEGRAM_MESSAGE_LIMIT
    assert text == render_message(huge)
    assert text.startswith("Mai notification\nMonitoring condition met.\nTask: ")


def test_the_message_has_no_markup_and_only_permitted_content() -> None:
    text = render_message(make_payload())
    assert all(ch not in text for ch in "<>&*_`[]")  # nothing a parse mode could read
    payload = make_payload()
    for leak in (OBJECTIVE_SENTINEL, str(payload.notification_id), payload.delivery_key,
                 "http", "token", "owner"):
        assert leak not in text


# ============================================================================
# D. Delivery through the real 6I service and the real network policy
# ============================================================================


async def test_a_notification_becomes_exactly_one_telegram_request(
    session_factory, tg_settings, workspace
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    adapter, _, transport = stack(tg_settings)
    result = await deliver(session_factory, note.id, "telegram", registry_with(adapter))

    assert result.outcome is DeliveryOutcome.DELIVERED
    assert result.delivery_key == "notification:%s:adapter:telegram" % note.id
    assert transport.connections == [URL]
    assert transport.methods == ["POST"]
    created = note.created_at
    created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
    assert json.loads(transport.bodies[0]) == {
        "chat_id": CHAT_ID,
        "text": (
            "Mai notification\nMonitoring condition met.\nTask: %s\nCheck: 1\n"
            "Time: %s UTC" % (note.task_id, created.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        ),
    }


async def test_a_failure_notification_is_delivered_with_its_own_message(
    session_factory, tg_settings, workspace
) -> None:
    note = await failed_notification(session_factory, tg_settings, workspace)
    adapter, _, transport = stack(tg_settings)
    result = await deliver(session_factory, note.id, "telegram", registry_with(adapter))
    assert result.outcome is DeliveryOutcome.DELIVERED
    body = json.loads(transport.bodies[0])
    assert body["text"].splitlines()[1] == "Monitoring stopped after repeated failures."


async def test_the_request_is_https_post_to_the_fixed_host_with_a_two_key_body(
    session_factory, tg_settings, workspace
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    adapter, _, transport = stack(tg_settings)
    await deliver(session_factory, note.id, "telegram", registry_with(adapter))

    request = httpx.URL(transport.connections[0])
    assert (request.scheme, request.host, request.port, request.path) == (
        "https", "api.telegram.org", None, "/bot%s/sendMessage" % TOKEN,
    )
    assert request.query == b"" and request.fragment == ""
    body = json.loads(transport.bodies[0])
    assert sorted(body) == ["chat_id", "text"]            # no parse_mode, no extras
    assert isinstance(body["chat_id"], int) and body["chat_id"] == CHAT_ID


async def test_the_same_notification_is_sent_once_and_then_reported_duplicate(
    session_factory, tg_settings, workspace
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    adapter, _, transport = stack(tg_settings)
    registry = registry_with(adapter)
    first = await deliver(session_factory, note.id, "telegram", registry)
    again = [await deliver(session_factory, note.id, "telegram", registry) for _ in range(3)]
    assert first.outcome is DeliveryOutcome.DELIVERED
    assert {r.outcome for r in again} == {DeliveryOutcome.DUPLICATE}
    assert {r.delivery_key for r in again} == {first.delivery_key}
    assert len(transport.connections) == 1


async def test_racing_deliveries_of_one_notification_send_once(tg_settings) -> None:
    adapter, _, transport = stack(tg_settings)
    payload = make_payload()
    statuses = await asyncio.gather(*(adapter.deliver(payload) for _ in range(6)))
    assert sorted(s.value for s in statuses) == ["delivered"] + ["duplicate"] * 5
    assert len(transport.connections) == 1


async def test_distinct_notifications_are_each_sent(tg_settings) -> None:
    adapter, _, transport = stack(tg_settings)
    for _ in range(3):
        assert await adapter.deliver(make_payload()) is DeliveryStatus.DELIVERED
    assert len(transport.connections) == 3


async def test_the_delivery_memory_is_bounded(tg_settings, monkeypatch) -> None:
    monkeypatch.setattr(notifier_module, "REMEMBERED_KEYS", 2)
    adapter, _, transport = stack(tg_settings)
    payloads = [make_payload() for _ in range(3)]
    for p in payloads:
        await adapter.deliver(p)
    assert len(adapter._delivered) == 2
    # The oldest was forgotten, so only it is sent again; the others are not.
    assert await adapter.deliver(payloads[0]) is DeliveryStatus.DELIVERED
    assert await adapter.deliver(payloads[2]) is DeliveryStatus.DUPLICATE
    assert REMEMBERED_KEYS == 4096


async def test_a_failed_send_is_not_remembered_and_is_not_retried(tg_settings) -> None:
    failing = ok_transport(status_code=500, payload={"ok": False})
    adapter, sender, _ = stack(tg_settings, transport=failing)
    payload = make_payload()
    assert await adapter.deliver(payload) is DeliveryStatus.FAILED
    assert len(failing.connections) == 1            # no automatic retry

    healthy = ok_transport()
    sender._client._transport = healthy
    sender._client._client = None                   # rebuild over the new transport
    assert await adapter.deliver(payload) is DeliveryStatus.DELIVERED   # explicit retry
    assert len(healthy.connections) == 1


# ============================================================================
# E. Failures are delivery failures
# ============================================================================

FAILURES = [
    ("telegram 400", dict(status_code=400, payload={"ok": False, "description": "Bad Request"})),
    ("telegram 401", dict(status_code=401, payload={"ok": False})),
    ("telegram 403", dict(status_code=403, payload={"ok": False})),
    ("telegram 404", dict(status_code=404, payload={"ok": False})),
    ("telegram 429", dict(status_code=429, payload={"ok": False}, headers={"Retry-After": "1"})),
    ("telegram 500", dict(status_code=500, payload={"ok": False})),
    ("telegram 502", dict(status_code=502, body=b"<html>bad gateway</html>")),
    ("telegram 503", dict(status_code=503, payload={"ok": False})),
    ("200 not json", dict(status_code=200, body=b"not json at all")),
    ("200 empty", dict(status_code=200, body=b"")),
    ("200 ok false", dict(status_code=200, payload={"ok": False})),
    ("200 ok missing", dict(status_code=200, payload={"result": {}})),
    ("200 ok string", dict(status_code=200, payload={"ok": "true"})),
    ("200 ok 1", dict(status_code=200, payload={"ok": 1})),
    ("200 a list", dict(status_code=200, body=b"[true]")),
    ("200 null", dict(status_code=200, body=b"null")),
    ("redirect", dict(status_code=302, headers={"Location": "http://127.0.0.1:8000/steal"})),
    ("timeout", dict(raise_error=httpx.ReadTimeout("timed out on " + URL))),
    ("connect timeout", dict(raise_error=httpx.ConnectTimeout("timed out on " + URL))),
    ("network failure", dict(raise_error=httpx.ConnectError("refused " + URL))),
    ("reset", dict(raise_error=httpx.RemoteProtocolError("reset while posting " + URL))),
]


@pytest.mark.parametrize("name,kwargs", FAILURES, ids=[f[0] for f in FAILURES])
async def test_every_telegram_failure_is_a_failed_delivery(
    tg_settings, caplog, name, kwargs
) -> None:
    transport = StubTransport(**kwargs)
    adapter, _, _ = stack(tg_settings, transport=transport)
    with caplog.at_level("DEBUG"):
        status = await adapter.deliver(make_payload())
    assert status is DeliveryStatus.FAILED
    assert len(transport.connections) == 1          # one attempt, never a retry
    assert all(not c.startswith("http://") for c in transport.connections)
    for record in mai_records(caplog):
        assert not leaks(record.getMessage() + repr(record.__dict__))
        assert record.exc_info is None


@pytest.mark.parametrize("status", [199, 300, 301, 302, 307, 400, 401, 403, 404, 429, 500, 502, 503])
async def test_a_non_success_status_fails_even_when_the_body_says_ok(tg_settings, status) -> None:
    """The status check is its own guard: a proxy or gateway can answer with a
    non-2xx status and a body that happens to read `{"ok": true}`."""
    transport = StubTransport(status_code=status, payload={"ok": True, "result": {}})
    adapter, _, _ = stack(tg_settings, transport=transport)
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert len(transport.connections) == 1


async def test_a_redirect_is_not_followed(tg_settings) -> None:
    transport = ok_transport(
        status_code=307, headers={"Location": "https://127.0.0.1/steal"}, payload={}
    )
    adapter, _, _ = stack(tg_settings, transport=transport)
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert transport.connections == [URL]           # the redirect target was never contacted


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "172.16.5.4", "192.168.1.1",
                                     "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
                                     "fe80::1", "fd00::1"])
async def test_a_telegram_host_resolving_to_a_private_address_is_refused(
    tg_settings, address
) -> None:
    transport = ok_transport()
    adapter, _, _ = stack(tg_settings, transport=transport, resolve=resolver_to(address))
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert transport.connections == []              # nothing connected at all


async def test_an_unresolvable_host_is_refused(tg_settings) -> None:
    def broken(host, port):
        raise OSError("no such host")

    transport = ok_transport()
    adapter, _, _ = stack(tg_settings, transport=transport, resolve=broken)
    assert await adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert transport.connections == []


# ============================================================================
# F. The destination cannot be steered
# ============================================================================


async def test_the_policy_attached_to_the_sender_is_exactly_telegram_post_only(tg_settings) -> None:
    policy = BotApiSender(tg_settings)._client._policy
    assert policy.allowed_hosts == frozenset({"api.telegram.org"})
    assert policy.allowed_methods == frozenset({"POST"})
    assert policy.follow_redirects is False and policy.max_redirects == 0
    assert policy.retries.max_attempts == 1 and policy.extra_request_headers == frozenset()


@pytest.mark.parametrize("url", [
    "https://evil.example/bot%s/sendMessage" % TOKEN,
    "https://api.telegram.org.evil.example/bot%s/sendMessage" % TOKEN,
    "https://evil.example/@api.telegram.org/x",
    "http://api.telegram.org/bot%s/sendMessage" % TOKEN,
    "ftp://api.telegram.org/bot%s/sendMessage" % TOKEN,
    "https://127.0.0.1/bot%s/sendMessage" % TOKEN,
    "https://localhost/bot%s/sendMessage" % TOKEN,
    "https://[::1]/bot%s/sendMessage" % TOKEN,
    "https://169.254.169.254/latest/meta-data",
    "https://api.telegram.org:8443/bot%s/sendMessage" % TOKEN,
])
async def test_the_sender_s_client_refuses_any_other_destination(tg_settings, url) -> None:
    _, sender, transport = stack(tg_settings)
    with pytest.raises(NetworkPolicyViolation):
        await sender._client.post_json(url, {"chat_id": CHAT_ID, "text": "x"})
    assert transport.connections == []


async def test_the_sender_s_client_refuses_a_get(tg_settings) -> None:
    _, sender, transport = stack(tg_settings)
    with pytest.raises(NetworkPolicyViolation):
        await sender._client.get(URL)
    assert transport.connections == []


async def test_the_chat_id_comes_only_from_configuration(tg_settings) -> None:
    spy = SpySender()
    adapter = TelegramNotificationAdapter(tg_settings, sender=spy)
    for _ in range(3):
        assert await adapter.deliver(make_payload()) is DeliveryStatus.DELIVERED
    assert {chat for chat, _ in spy.calls} == {CHAT_ID}


def test_a_payload_cannot_carry_a_chat_a_url_or_a_channel() -> None:
    for extra in ({"chat_id": 1}, {"url": "https://evil.example"},
                  {"channel": "other"}, {"recipient": "x"}, {"parse_mode": "HTML"}):
        with pytest.raises(ValidationError):
            make_payload(**extra)


async def test_a_look_alike_payload_with_a_chat_id_is_refused(tg_settings) -> None:
    class Hostile:
        notification_id = uuid.uuid4()
        task_id = uuid.uuid4()
        kind = TaskNotificationKind.CONDITION_MET
        check_number = 1
        created_at = datetime.now(timezone.utc)
        delivery_key = "notification:x:adapter:telegram"
        chat_id = 999

    spy = SpySender()
    adapter = TelegramNotificationAdapter(tg_settings, sender=spy)
    for bad in (Hostile(), None, {"chat_id": 999}, "payload", 7, object()):
        assert await adapter.deliver(bad) is DeliveryStatus.FAILED
    assert spy.calls == []


async def test_a_key_minted_for_another_adapter_is_refused(tg_settings) -> None:
    spy = SpySender()
    adapter = TelegramNotificationAdapter(tg_settings, sender=spy)
    notification_id = uuid.uuid4()
    for key in (delivery_key(notification_id, "local"),
                delivery_key(uuid.uuid4(), "telegram"),
                "notification:%s:adapter:telegram " % notification_id):
        payload = make_payload(notification_id=notification_id, delivery_key=key)
        assert await adapter.deliver(payload) is DeliveryStatus.FAILED
    assert spy.calls == []


# ============================================================================
# G. Secrets: the token appears in the request URL and nowhere else
# ============================================================================


async def test_the_token_never_reaches_a_result_a_log_or_a_message(
    session_factory, tg_settings, workspace, caplog
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    adapter, _, transport = stack(tg_settings)
    with caplog.at_level("DEBUG"):
        result = await deliver(session_factory, note.id, "telegram", registry_with(adapter))
    assert not leaks(repr(result))
    assert not leaks(json.loads(transport.bodies[0])["text"])
    for record in mai_records(caplog):
        assert not leaks(record.getMessage() + repr(record.__dict__))


async def test_an_exception_carrying_the_token_and_url_is_contained(
    session_factory, tg_settings, workspace, caplog
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    spy = SpySender(error=RuntimeError("could not POST " + URL + " using " + TOKEN))
    adapter = TelegramNotificationAdapter(tg_settings, sender=spy)
    with caplog.at_level("DEBUG"):
        result = await deliver(session_factory, note.id, "telegram", registry_with(adapter))
    assert result.outcome is DeliveryOutcome.FAILED
    assert len(spy.calls) == 1
    assert not leaks(repr(result))
    for record in mai_records(caplog):
        assert not leaks(record.getMessage() + repr(record.__dict__))
        assert record.exc_info is None


async def test_failure_logs_carry_only_an_adapter_a_reason_and_an_id(
    tg_settings, caplog
) -> None:
    adapter, _, _ = stack(tg_settings, transport=ok_transport(status_code=500, payload={}))
    payload = make_payload()
    with caplog.at_level("DEBUG"):
        await adapter.deliver(payload)
    mine = [r for r in mai_records(caplog) if r.name == "app.telegram.notifier"]
    assert len(mine) == 1
    record = mine[0]
    # The wording is prose, not an invariant (the structural test pins that it
    # is one constant with no formatting). What matters is level and fields.
    assert record.levelno == logging.WARNING
    assert (record.adapter, record.reason, record.notification_id) == (
        "telegram", "telegram_send_failed", str(payload.notification_id),
    )


# ============================================================================
# H. Boundaries preserved
# ============================================================================


async def test_missing_malformed_and_foreign_notifications_never_reach_telegram(
    session_factory, tg_settings, workspace
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    adapter, _, transport = stack(tg_settings)
    registry = registry_with(adapter)
    assert (await deliver(session_factory, uuid.uuid4(), "telegram", registry)).reason == (
        "notification_not_found"
    )
    for bad in (None, "", "not-a-uuid", str(note.id), 7):
        assert (await deliver(session_factory, bad, "telegram", registry)).reason == (
            "malformed_notification"
        )
    assert (await deliver(session_factory, note.id, "telegram", registry, owner=OTHER)).reason == (
        "notification_not_found"
    )
    assert (await deliver(session_factory, note.id, "tg", registry)).reason == "unknown_adapter"
    assert transport.connections == []


async def test_telegram_delivery_changes_no_task_notification_or_execution_state(
    session_factory, tg_settings, workspace
) -> None:
    met = await met_notification(session_factory, tg_settings, workspace)
    failed = await failed_notification(session_factory, tg_settings, workspace)
    before = await snapshot(session_factory)

    for kwargs in (dict(), dict(status_code=500, payload={}),
                   dict(raise_error=httpx.ConnectError("refused " + URL)),
                   dict(status_code=200, body=b"nonsense")):
        adapter, _, _ = stack(tg_settings, transport=StubTransport(**kwargs) if kwargs else None)
        registry = registry_with(adapter)
        for note in (met, failed):
            await deliver(session_factory, note.id, "telegram", registry)
    # And while unconfigured.
    tg_settings.TELEGRAM_BOT_TOKEN = ""
    for note in (met, failed):
        await deliver(session_factory, note.id, "telegram",
                      registry_with(TelegramNotificationAdapter(tg_settings)))

    assert await snapshot(session_factory) == before


async def test_the_local_adapter_still_works_beside_telegram(
    session_factory, tg_settings, workspace
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    local = LocalRecordingAdapter()
    adapter, _, transport = stack(tg_settings)
    registry = registry_with(local, adapter)
    a = await deliver(session_factory, note.id, "local", registry)
    b = await deliver(session_factory, note.id, "telegram", registry)
    assert a.outcome is b.outcome is DeliveryOutcome.DELIVERED
    assert a.delivery_key != b.delivery_key
    assert len(local.delivered) == 1 and len(transport.connections) == 1
    assert (await deliver(session_factory, note.id, "local", registry)).outcome is (
        DeliveryOutcome.DUPLICATE
    )


async def test_an_unreachable_telegram_leaves_the_notification_deliverable_later(
    session_factory, tg_settings, workspace
) -> None:
    note = await met_notification(session_factory, tg_settings, workspace)
    down, sender, _ = stack(
        tg_settings, transport=StubTransport(raise_error=httpx.ConnectError("down " + URL)),
    )
    registry = registry_with(down)
    assert (await deliver(session_factory, note.id, "telegram", registry)).outcome is (
        DeliveryOutcome.FAILED
    )
    up = ok_transport()
    sender._client._transport = up
    sender._client._client = None
    assert (await deliver(session_factory, note.id, "telegram", registry)).outcome is (
        DeliveryOutcome.DELIVERED
    )
    assert len(up.connections) == 1


# ============================================================================
# I. Under Mai's real logging configuration
# ============================================================================


@pytest.fixture
def real_logging(capsys):
    """Apply `configure_logging` for real, then put the process back."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved = (list(root.handlers), root.level,
             {n: logging.getLogger(n).level for n in ("httpx", "httpcore")})
    try:
        yield configure_logging
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        for name, level in saved[2].items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("fmt", ["json", "console"])
async def test_nothing_reaches_the_real_log_output_even_at_debug(
    real_logging, capsys, tg_settings, fmt
) -> None:
    """The end-to-end claim: with Mai's own logging configuration at its most
    verbose, a success and a failure leave neither the token, nor the URL, nor
    the endpoint name anywhere in what is written."""
    real_logging("DEBUG", fmt)
    ok_adapter, _, _ = stack(tg_settings)
    bad_adapter, _, _ = stack(tg_settings, transport=ok_transport(status_code=500, payload={}))
    boom = TelegramNotificationAdapter(
        tg_settings, sender=SpySender(error=RuntimeError("POST " + URL + " " + TOKEN)),
    )
    assert await ok_adapter.deliver(make_payload()) is DeliveryStatus.DELIVERED
    assert await bad_adapter.deliver(make_payload()) is DeliveryStatus.FAILED
    assert await boom.deliver(make_payload()) is DeliveryStatus.FAILED

    out = capsys.readouterr().out
    # Logging really was live: the failures were written, by Mai's logger, with
    # their reason code (wording is prose and deliberately not pinned).
    assert "app.telegram.notifier" in out and "telegram_send_failed" in out
    assert not leaks(out) and TOKEN not in out and quote(TOKEN, safe="") not in out
    assert "api.telegram.org" not in out


async def test_the_httpx_pin_is_what_keeps_the_token_out_of_logs(
    real_logging, tg_settings
) -> None:
    """A control for the test above. httpx writes the full request URL -- token
    included -- at INFO. `configure_logging` pins `httpx` to WARNING, and that
    pin is the only thing standing between the token and the log, because the
    redactor has no pattern for a Telegram token. Lift the pin and it leaks."""
    real_logging("DEBUG", "console")
    httpx_logger = logging.getLogger("httpx")
    assert httpx_logger.level == logging.WARNING        # the pin, as configured

    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    collector = Collect(level=logging.DEBUG)
    httpx_logger.addHandler(collector)
    try:
        adapter, _, _ = stack(tg_settings)
        await adapter.deliver(make_payload())
        assert seen == []                               # pinned: nothing emitted

        httpx_logger.setLevel(logging.INFO)             # lift the pin
        adapter, _, _ = stack(tg_settings)
        await adapter.deliver(make_payload())
    finally:
        httpx_logger.removeHandler(collector)
    assert seen and any(SECRET in line for line in seen)
