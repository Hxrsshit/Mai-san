"""Stage 6M.4: one API token, one request seam, fail closed.

Through the real app. The suite's test token (see `conftest.py`) is a
deterministic, obviously fake value; other tokens here are synthetic too.
Every route the app registers -- with every optional router switched on --
is exercised without a credential and must refuse; the public allow-list is
exercised and must not.
"""

import hashlib
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core import auth
from app.core.config import Settings, get_settings
from app.core.owner import CONFIGURED_OWNER_ID
from app.database.session import get_db_session, get_session_factory
from app.llm.factory import get_llm_provider
from app.main import create_app
from app.tasks.models import LOCAL_OWNER_ID, Task, TaskNotification
from tests.conftest import AUTH_HEADERS, TEST_API_TOKEN
from tests.test_delivery import OTHER, met_notification

pytestmark = pytest.mark.anyio

PUBLIC = {
    ("GET", "/health"),
    ("GET", "/api/integrations/google/callback"),
    ("GET", "/api/integrations/gmail/callback"),
    ("POST", "/api/telegram/webhook"),
}
WRONG = "an-entirely-different-synthetic-token"


@pytest.fixture(autouse=True)
async def _other_owner_is_an_active_member(session_factory):
    """Stage 7B unit 8b: work runs only for an active owner, so the second
    owner these tests use is a real member."""
    from tests.scopes import make_active_member
    from tests.test_delivery import OTHER as _OTHER

    await make_active_member(session_factory, _OTHER)


@pytest.fixture(autouse=True)
def _catalog():
    from app.tools import catalog  # noqa: F401


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture
def every_router(execution_settings):
    """Settings under which every optional router is registered."""
    execution_settings.MEMORY_ENABLED = True
    execution_settings.HISTORY_IMPORT_ENABLED = True
    execution_settings.REMINDERS_ENABLED = True
    return execution_settings


@asynccontextmanager
async def app_client(monkeypatch, app_settings, factory, fake_provider, headers=None):
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: app_settings)
    app = create_app()

    async def override_session():
        async with factory() as session:
            yield session
            if session.in_transaction():
                await session.commit()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: app_settings
    app.dependency_overrides[get_session_factory] = lambda: factory
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                           base_url="http://test", headers=headers or {}) as http:
        http.app = app
        yield http
    app.dependency_overrides.clear()


def routes(app):
    """(method, path) for every API route the app registered."""
    return sorted(
        (method, r.path) for r in app.routes if isinstance(r, APIRoute) for method in r.methods
    )


def concrete(path: str) -> str:
    return path.replace("{", "").replace("}", "") if "{" not in path else "/".join(
        str(uuid.uuid4()) if part.startswith("{") else part for part in path.split("/"))


# ============================================================================
# A. The core authenticator
# ============================================================================


def test_the_right_token_authenticates_as_the_configured_owner() -> None:
    settings = Settings(_env_file=None, MAI_API_TOKEN_SHA256=digest("tok-123"))
    assert auth.authenticate("tok-123", settings) == CONFIGURED_OWNER_ID == LOCAL_OWNER_ID
    assert LOCAL_OWNER_ID is CONFIGURED_OWNER_ID                     # one value, re-exported


@pytest.mark.parametrize("token", ["tok-124", "TOK-123", "tok-123 ", "", None, 123, b"tok-123",
                                   digest("tok-123"), "x" * (auth.MAX_TOKEN_CHARS + 1)])
def test_anything_else_is_refused(token) -> None:
    settings = Settings(_env_file=None, MAI_API_TOKEN_SHA256=digest("tok-123"))
    assert auth.authenticate(token, settings) is None


def test_an_oversized_token_is_refused_even_if_it_is_the_token() -> None:
    """The length bound is enforced before hashing: a token over the limit is
    never accepted, even one whose digest is the configured one."""
    huge = "x" * (auth.MAX_TOKEN_CHARS + 1)
    settings = Settings(_env_file=None, MAI_API_TOKEN_SHA256=digest(huge))
    assert auth.authenticate(huge, settings) is None
    at_limit = "y" * auth.MAX_TOKEN_CHARS
    assert auth.authenticate(at_limit, Settings(_env_file=None, MAI_API_TOKEN_SHA256=digest(at_limit))) == CONFIGURED_OWNER_ID


def test_the_configured_digest_may_be_upper_case() -> None:
    settings = Settings(_env_file=None, MAI_API_TOKEN_SHA256=digest("tok-123").upper())
    assert auth.authenticate("tok-123", settings) == CONFIGURED_OWNER_ID


@pytest.mark.parametrize("configured", ["", "   ", "abc", digest("x")[:63], digest("x") + "0", "z" * 64,
                                        "tok-123"])
def test_no_or_a_malformed_digest_is_unconfigured_and_accepts_nothing(configured) -> None:
    settings = Settings(_env_file=None, MAI_API_TOKEN_SHA256=configured)
    assert auth.is_configured(settings) is False
    assert auth.authenticate("tok-123", settings) is None
    assert auth.authenticate(configured, settings) is None


def test_the_comparison_is_constant_time_over_sha256_digests(monkeypatch) -> None:
    seen, real = [], hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    settings = Settings(_env_file=None, MAI_API_TOKEN_SHA256=digest("tok-123"))
    auth.authenticate("tok-999", settings)
    assert seen == [(digest("tok-999"), digest("tok-123"))]


def test_the_default_is_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("MAI_API_TOKEN_SHA256", raising=False)
    assert Settings(_env_file=None).MAI_API_TOKEN_SHA256 == ""
    assert auth.is_configured(Settings(_env_file=None)) is False


# ============================================================================
# B. The request seam over HTTP
# ============================================================================


async def test_no_credential_is_401_authentication_required(anon_client) -> None:
    response = await anon_client.get("/api/tasks")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "authentication_required"


@pytest.mark.parametrize("value", ["Bearer", "Bearer ", "Basic " + TEST_API_TOKEN, "Token " + TEST_API_TOKEN,
                                   TEST_API_TOKEN, f"Bearer {TEST_API_TOKEN} extra", "Bearer\t"])
async def test_a_malformed_header_is_401_authentication_required(anon_client, value) -> None:
    response = await anon_client.get("/api/tasks", headers={"Authorization": value})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_wrong_token_is_401_invalid_credentials(anon_client) -> None:
    response = await anon_client.get("/api/tasks", headers={"Authorization": f"Bearer {WRONG}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_the_right_token_is_accepted(anon_client, scheme) -> None:
    response = await anon_client.get("/api/tasks", headers={"Authorization": f"{scheme} {TEST_API_TOKEN}"})
    assert response.status_code == 200


async def test_an_unconfigured_server_refuses_everything_with_503(
    monkeypatch, settings, session_factory, fake_provider
) -> None:
    unconfigured = settings.model_copy(update={"MAI_API_TOKEN_SHA256": ""})
    async with app_client(monkeypatch, unconfigured, session_factory, fake_provider, AUTH_HEADERS) as http:
        for headers in ({}, AUTH_HEADERS, {"Authorization": f"Bearer {WRONG}"}):
            response = await http.get("/api/tasks", headers=headers)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "authentication_not_configured"
        assert (await http.get("/health")).status_code == 200


async def test_refusals_never_echo_the_presented_token(anon_client) -> None:
    sentinel = "presented-token-sentinel-6m4"
    for value in (f"Bearer {sentinel}", f"Basic {sentinel}", sentinel):
        response = await anon_client.get("/api/tasks", headers={"Authorization": value})
        assert sentinel not in response.text and sentinel not in json.dumps(dict(response.headers))


# ============================================================================
# C. Coverage: every route outside the allow-list refuses; the allow-list does not
# ============================================================================


async def test_every_registered_route_outside_the_allow_list_refuses_without_a_token(
    monkeypatch, every_router, session_factory, fake_provider
) -> None:
    async with app_client(monkeypatch, every_router, session_factory, fake_provider) as http:
        registered = routes(http.app)
        assert set(PUBLIC) <= set(registered)
        protected = [r for r in registered if r not in PUBLIC]
        assert len(protected) > 40
        refused = {}
        for method, path in protected:
            response = await http.request(method, concrete(path), json={})
            refused[(method, path)] = response.status_code
    assert {k: v for k, v in refused.items() if v != 401} == {}


async def test_the_allow_list_is_reachable_without_a_token(
    monkeypatch, every_router, session_factory, fake_provider
) -> None:
    async with app_client(monkeypatch, every_router, session_factory, fake_provider) as http:
        assert (await http.get("/health")).status_code == 200
        for path in ("/api/integrations/google/callback", "/api/integrations/gmail/callback"):
            response = await http.get(path, params={"state": "nope", "code": "nope"})
            assert response.status_code not in (401, 503), path            # its own refusal
        webhook = await http.post("/api/telegram/webhook", json={})
        assert webhook.status_code == 404                                   # Telegram unconfigured: inert
        assert (await http.get("/docs")).status_code == 200
        assert (await http.get("/openapi.json")).status_code == 200


# ============================================================================
# D. The owner is the credential's, never the caller's
# ============================================================================


async def test_no_request_value_can_select_another_owner(
    client, session_factory, execution_settings, workspace
) -> None:
    mine = await met_notification(session_factory, execution_settings, workspace)
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    forged = {"owner_id": str(OTHER), "owner": str(OTHER)}
    headers = {"X-Owner-Id": str(OTHER), "X-User-Id": str(OTHER), "X-Mai-Owner": str(OTHER)}

    tasks = (await client.get("/api/tasks", params=forged, headers=headers)).json()["tasks"]
    assert {t["id"] for t in tasks} == {str(mine.task_id)}
    assert {t["owner_id"] for t in tasks} == {str(CONFIGURED_OWNER_ID)}
    assert (await client.get(f"/api/tasks/{theirs.task_id}", params=forged, headers=headers)).status_code == 404

    listed = (await client.get("/api/task-notifications", params=forged, headers=headers)).json()
    assert [n["id"] for n in listed["notifications"]] == [str(mine.id)]
    assert (await client.get(f"/api/task-notifications/{theirs.id}", headers=headers)).status_code == 404
    assert (await client.post(f"/api/task-notifications/{theirs.id}/read", json={}, headers=headers)).status_code == 404
    assert (await client.post(f"/api/task-notifications/{theirs.id}/read",
                              json={"owner_id": str(OTHER)})).status_code == 422
    delivery = await client.post(f"/api/task-notifications/{theirs.id}/deliveries",
                                 json={"adapter": "telegram"}, params=forged, headers=headers)
    assert delivery.status_code == 404
    async with session_factory() as session:
        read_at = (await session.execute(
            select(TaskNotification.read_at).where(TaskNotification.id == theirs.id))).scalar_one()
    assert read_at is None


async def test_the_seam_returns_the_configured_owner_for_the_token(session_factory) -> None:
    # Changed deliberately in 7A: the seam is async and checks membership in
    # the database, so it is awaited with a session whose schema carries the
    # default organisation's active Admin.
    from app.api.deps import get_owner_id

    async with session_factory() as session:
        owner = await get_owner_id(Settings(_env_file=None), session, f"Bearer {TEST_API_TOKEN}")
    assert owner == CONFIGURED_OWNER_ID


async def test_another_owners_task_state_is_invisible(
    client, session_factory, execution_settings, workspace
) -> None:
    theirs = await met_notification(session_factory, execution_settings, workspace, owner=OTHER)
    for path in (f"/api/tasks/{theirs.task_id}", f"/api/tasks/{theirs.task_id}/plan",
                 f"/api/tasks/{theirs.task_id}/steps", f"/api/tasks/{theirs.task_id}/events"):
        assert (await client.get(path)).status_code == 404, path
    activity = (await client.get("/api/tasks/activity")).text
    assert str(theirs.task_id) not in activity


# ============================================================================
# E. Telegram: its own authentication, untouched; the API token grants nothing
# ============================================================================


@pytest.fixture
def telegram_settings(settings):
    settings.TELEGRAM_BOT_TOKEN = "999000111:TOKEN-SENTINEL-6m4_tg"
    settings.TELEGRAM_WEBHOOK_SECRET = "webhook-secret-sentinel-6m4"
    settings.TELEGRAM_ALLOWED_CHAT_ID = "575757"
    settings.TELEGRAM_CONVERSATION_ID = str(uuid.uuid4())
    return settings


async def test_the_api_token_does_not_authenticate_the_telegram_webhook(
    monkeypatch, telegram_settings, session_factory, fake_provider
) -> None:
    async with app_client(monkeypatch, telegram_settings, session_factory, fake_provider) as http:
        update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 575757}, "text": "hi"}}
        as_api = await http.post("/api/telegram/webhook", json=update, headers=AUTH_HEADERS)
        token_as_secret = await http.post("/api/telegram/webhook", json=update,
                                          headers={"X-Telegram-Bot-Api-Secret-Token": TEST_API_TOKEN})
    assert as_api.status_code == 401 and as_api.json() == {"detail": "Unauthorized"}
    assert token_as_secret.status_code == 401


# ============================================================================
# F. The token never leaks
# ============================================================================


async def test_the_token_is_in_no_log_record_or_response(client, anon_client, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    texts = []
    for http, value in ((client, None), (anon_client, f"Bearer {TEST_API_TOKEN}x"),
                        (anon_client, f"Basic {TEST_API_TOKEN}")):
        response = await (http.get("/api/tasks") if value is None
                          else http.get("/api/tasks", headers={"Authorization": value}))
        texts.append(response.text + json.dumps(dict(response.headers)))
    logged = " ".join(r.getMessage() + repr(r.__dict__) for r in caplog.records)
    assert TEST_API_TOKEN not in logged
    assert all(TEST_API_TOKEN not in t for t in texts)
