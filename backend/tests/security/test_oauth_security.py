"""Stage 4F-G: the OAuth flow, and what it must never hand back.

Every test uses synthetic credentials. The sentinels are deliberately
distinctive so a leak anywhere is unmistakable.
"""

import ast
import asyncio
import json
import os
import pathlib
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.config import Settings
from app.integrations.errors import NetworkPolicyViolation
from app.integrations.google_calendar import (
    CALENDAR_READONLY_SCOPE,
    REQUIRED_SCOPES,
    api_policy,
)
from app.integrations.http_client import SecureHttpClient
from app.integrations.oauth import (
    AUTHORIZE_ENDPOINT,
    TOKEN_ENDPOINT,
    TOKEN_HOST,
    OAuthError,
    PendingAuthorizations,
    build_authorization_url,
    exchange_code,
    refresh_token,
    token_policy,
    validate_redirect_uri,
)
from app.integrations.token_store import FileTokenStore, StoredToken, TokenStoreError

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

ACCESS = "ya29.ACCESS-SENTINEL-NEVER-REAL-0123456789"
REFRESH = "1//REFRESH-SENTINEL-NEVER-REAL-9876543210"
CLIENT_SECRET = "GOCSPX-CLIENT-SECRET-SENTINEL"
REDIRECT = "http://127.0.0.1:8000/api/integrations/google/callback"


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "GROQ_API_KEY": "x",
        "GOOGLE_OAUTH_CLIENT_ID": "cid.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
        "GOOGLE_OAUTH_REDIRECT_URI": REDIRECT,
    }
    base.update(overrides)
    return Settings(**base)


class TokenTransport(httpx.AsyncBaseTransport):
    """Answers the token endpoint. Records everything that reached it."""

    def __init__(self, status_code=200, payload=None):
        self.connections = []
        self.bodies = []
        self.request_headers = []
        self._status = status_code
        self._payload = payload if payload is not None else {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "expires_in": 3600,
            "scope": CALENDAR_READONLY_SCOPE,
        }

    async def handle_async_request(self, request):
        self.connections.append(str(request.url))
        self.bodies.append(request.content)
        self.request_headers.append(dict(request.headers))
        return httpx.Response(self._status, json=self._payload)


def _client(transport, policy=None):
    return SecureHttpClient(
        policy=policy or token_policy(),
        transport=transport,
        resolve=lambda host, port: [(2, 1, 6, "", ("142.250.72.1", port))],
    )


# --- Scope (§3) -------------------------------------------------------------


def test_only_the_narrowest_read_scope_is_requested() -> None:
    """`calendar.events.readonly`, not `calendar.readonly`.

    The wider scope also grants calendar metadata, settings and access-control
    lists, none of which listing events needs. A scope is issued once and
    cannot be narrowed afterwards.
    """
    assert REQUIRED_SCOPES == frozenset({
        "https://www.googleapis.com/auth/calendar.events.readonly"
    })


@pytest.mark.parametrize(
    "forbidden",
    [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
)
def test_no_wider_scope_is_ever_requested(forbidden) -> None:
    pending = PendingAuthorizations()
    started = build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    requested = parse_qs(urlparse(started.authorization_url).query)["scope"][0]
    assert forbidden not in requested.split(), forbidden


def test_the_authorization_url_is_googles_and_carries_pkce() -> None:
    pending = PendingAuthorizations()
    started = build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    assert started.authorization_url.startswith(AUTHORIZE_ENDPOINT)
    query = parse_qs(urlparse(started.authorization_url).query)

    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["state"] == [started.state]
    assert query["redirect_uri"] == [REDIRECT]


def test_the_authorization_url_carries_no_verifier_and_no_secret() -> None:
    """The URL is shown to a user and logged by browsers.

    The PKCE verifier is what makes the challenge meaningful; sending it
    alongside would defeat the exchange entirely.
    """
    pending = PendingAuthorizations()
    started = build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    assert "code_verifier" not in started.authorization_url
    assert "client_secret" not in started.authorization_url
    assert CLIENT_SECRET not in started.authorization_url


# --- State: CSRF and replay (§17) -------------------------------------------


def test_a_state_is_single_use() -> None:
    """Consuming removes it, so a replayed callback finds nothing.

    A property of the data structure rather than a check someone must
    remember to write.
    """
    pending = PendingAuthorizations()
    started = build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    assert pending.consume(started.state) is not None
    assert pending.consume(started.state) is None


@pytest.mark.parametrize(
    "forged", ["", "   ", "not-a-real-state", "0" * 43, "../../etc"],
)
def test_a_forged_state_is_not_accepted(forged) -> None:
    pending = PendingAuthorizations()
    build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    assert pending.consume(forged) is None


def test_an_expired_state_is_not_accepted(monkeypatch) -> None:
    import app.integrations.oauth as module

    pending = PendingAuthorizations()
    started = build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    # Advance past the TTL.
    real_monotonic = module.time.monotonic
    monkeypatch.setattr(
        module.time, "monotonic",
        lambda: real_monotonic() + module.STATE_TTL_SECONDS + 1,
    )

    assert pending.consume(started.state) is None


def test_states_are_unpredictable_and_distinct() -> None:
    pending = PendingAuthorizations()
    states = {
        build_authorization_url(
            client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
            pending=pending,
        ).state
        for _ in range(50)
    }

    assert len(states) == 50
    assert all(len(state) >= 32 for state in states)


# --- Redirect URI (§17) -----------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "https://attacker.test/callback",
        "http://attacker.test:8000/callback",
        "http://127.0.0.1.evil.test:8000/cb",
        "http://169.254.169.254:8000/cb",
        "https://127.0.0.1:8000/cb",
        "http://127.0.0.1/cb",
        "http://127.0.0.1:80/cb",
        "http://127.0.0.1:8000/cb?next=https://evil.test",
        "http://127.0.0.1:8000/cb#frag",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "", "   ", "not a uri",
    ],
)
def test_a_non_loopback_redirect_is_refused(hostile) -> None:
    """The code is delivered to a listener on the user's own machine.

    A redirect anywhere else hands the authorization code to whoever owns
    that host.
    """
    with pytest.raises(OAuthError):
        validate_redirect_uri(hostile)


@pytest.mark.parametrize(
    "permitted",
    [
        "http://127.0.0.1:8000/api/integrations/google/callback",
        "http://localhost:9000/cb",
        "http://[::1]:8000/cb",
    ],
)
def test_a_loopback_redirect_is_permitted(permitted) -> None:
    validate_redirect_uri(permitted)


def test_a_tampered_redirect_is_refused_at_exchange_time() -> None:
    """Checked when the URL is built *and* when the code is exchanged.

    So a redirect cannot be swapped between the two.
    """
    from app.integrations.oauth import PendingAuthorization

    tampered = PendingAuthorization(
        state="s", code_verifier="v", redirect_uri="https://attacker.test/cb",
        scopes=(CALENDAR_READONLY_SCOPE,), created_at=0.0,
    )
    transport = TokenTransport()

    with pytest.raises(OAuthError):
        asyncio.get_event_loop().run_until_complete(
            exchange_code(_client(transport), "cid", CLIENT_SECRET, "code", tampered)
        )

    assert transport.connections == []


# --- Network boundary (§4, §17) ---------------------------------------------


def test_the_token_client_reaches_one_host_with_one_verb() -> None:
    policy = token_policy()

    assert policy.allowed_hosts == frozenset({TOKEN_HOST})
    assert policy.allowed_methods == frozenset({"POST"})
    assert policy.follow_redirects is False
    assert policy.retries.max_attempts == 1


def test_the_api_client_reaches_one_host_with_one_verb() -> None:
    policy = api_policy()

    assert policy.allowed_hosts == frozenset({"www.googleapis.com"})
    assert policy.allowed_methods == frozenset({"GET"})
    assert policy.follow_redirects is False


def test_the_consent_host_is_in_no_policy() -> None:
    """Mai never connects to `accounts.google.com`. The browser does.

    Its absence from every policy is what makes that structural rather than
    incidental.
    """
    for policy in (token_policy(), api_policy()):
        assert "accounts.google.com" not in policy.allowed_hosts


@pytest.mark.parametrize(
    "hostile",
    [
        "https://accounts.google.com/o/oauth2/token",
        "https://www.googleapis.com/token",
        "https://oauth2.googleapis.com.evil.test/token",
        "https://127.0.0.1/token",
        "https://169.254.169.254/token",
        "http://oauth2.googleapis.com/token",
        "https://oauth2.googleapis.com:8443/token",
        "https://attacker.test/token",
    ],
)
async def test_the_token_client_refuses_every_other_destination(hostile) -> None:
    transport = TokenTransport()
    client = _client(transport)

    with pytest.raises(NetworkPolicyViolation):
        await client.post_form(hostile, form={"grant_type": "refresh_token"})

    assert transport.connections == []


@pytest.mark.parametrize(
    "hostile",
    [
        "https://www.googleapis.com.evil.test/calendar/v3/x",
        "https://oauth2.googleapis.com/calendar/v3/x",
        "https://127.0.0.1/calendar/v3/x",
        "https://169.254.169.254/",
        "http://www.googleapis.com/calendar/v3/x",
        "https://www.googleapis.com:8443/calendar/v3/x",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
    ],
)
async def test_the_api_client_refuses_every_other_destination(hostile) -> None:
    transport = TokenTransport()
    client = _client(transport, policy=api_policy())

    with pytest.raises(NetworkPolicyViolation):
        await client.get(hostile)

    assert transport.connections == []


async def test_the_api_client_cannot_post() -> None:
    """Read-only at the transport, not merely by convention."""
    client = _client(TokenTransport(), policy=api_policy())

    with pytest.raises(NetworkPolicyViolation) as refusal:
        await client.post_json(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            json_body={"summary": "injected"},
        )

    assert refusal.value.detail == "method"


async def test_the_token_client_cannot_get() -> None:
    client = _client(TokenTransport())

    with pytest.raises(NetworkPolicyViolation) as refusal:
        await client.get(TOKEN_ENDPOINT)

    assert refusal.value.detail == "method"


# --- Credential isolation (§5) ----------------------------------------------


async def test_the_client_secret_and_code_travel_only_in_the_body() -> None:
    from app.integrations.oauth import PendingAuthorization

    pending = PendingAuthorization(
        state="s", code_verifier="verifier-value", redirect_uri=REDIRECT,
        scopes=(CALENDAR_READONLY_SCOPE,), created_at=0.0,
    )
    transport = TokenTransport()

    await exchange_code(
        _client(transport), "cid", CLIENT_SECRET, "auth-code", pending
    )

    assert CLIENT_SECRET not in transport.connections[0]
    assert "auth-code" not in transport.connections[0]
    body = transport.bodies[0].decode()
    assert CLIENT_SECRET in body and "verifier-value" in body


async def test_no_token_reaches_the_logs(caplog) -> None:
    import logging

    from app.integrations.oauth import PendingAuthorization

    caplog.set_level(logging.DEBUG)
    pending = PendingAuthorization(
        state="s", code_verifier="v", redirect_uri=REDIRECT,
        scopes=(CALENDAR_READONLY_SCOPE,), created_at=0.0,
    )

    await exchange_code(
        _client(TokenTransport()), "cid", CLIENT_SECRET, "auth-code", pending
    )

    for secret in (ACCESS, REFRESH, CLIENT_SECRET, "auth-code"):
        assert secret not in caplog.text, secret


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
async def test_no_error_carries_token_material(status, caplog) -> None:
    """The token endpoint's error body can echo the request back.

    The request carries the client secret, the code and the verifier, so the
    body is never carried into an exception or a log line.
    """
    import logging

    from app.integrations.oauth import PendingAuthorization

    caplog.set_level(logging.DEBUG)
    transport = TokenTransport(
        status_code=status,
        payload={"error": "invalid_grant",
                 "error_description": f"secret={CLIENT_SECRET} code=auth-code"},
    )
    pending = PendingAuthorization(
        state="s", code_verifier="v", redirect_uri=REDIRECT,
        scopes=(CALENDAR_READONLY_SCOPE,), created_at=0.0,
    )

    with pytest.raises(OAuthError) as failure:
        await exchange_code(
            _client(transport), "cid", CLIENT_SECRET, "auth-code", pending
        )

    assert CLIENT_SECRET not in str(failure.value)
    assert CLIENT_SECRET not in caplog.text
    assert "auth-code" not in caplog.text


def test_a_stored_token_never_renders_its_values() -> None:
    """`repr` is overridden, so a debugger or a log line cannot print one."""
    token = StoredToken(ACCESS, REFRESH, None, (CALENDAR_READONLY_SCOPE,))

    for rendering in (repr(token), str(token), f"{token}"):
        assert ACCESS not in rendering
        assert REFRESH not in rendering


def test_the_token_store_writes_with_tight_permissions() -> None:
    directory = tempfile.mkdtemp()
    store = FileTokenStore(directory)
    store.save("google", StoredToken(ACCESS, REFRESH, None, ()))

    path = pathlib.Path(directory) / "google.default.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (stat.S_IMODE(pathlib.Path(directory).stat().st_mode) & 0o077)


def test_a_widened_token_file_is_refused_rather_than_used() -> None:
    """A token readable by other accounts should be re-issued, not trusted."""
    directory = tempfile.mkdtemp()
    store = FileTokenStore(directory)
    store.save("google", StoredToken(ACCESS, REFRESH, None, ()))

    path = pathlib.Path(directory) / "google.default.json"
    os.chmod(path, 0o644)

    with pytest.raises(TokenStoreError):
        store.load("google")


@pytest.mark.parametrize(
    ("provider", "account"),
    [("../../etc/passwd", "default"), ("google", "../../../root"),
     ("google/../..", "a/b"), ("\x00google", "default")],
)
def test_a_token_path_cannot_leave_its_directory(provider, account) -> None:
    directory = tempfile.mkdtemp()
    store = FileTokenStore(directory)

    resolved = store._path(provider, account).resolve()
    assert resolved.parent == pathlib.Path(directory).resolve()


def test_no_token_is_written_to_the_database() -> None:
    """Structural: the token store imports no database module."""
    tree = ast.parse((APP / "integrations" / "token_store.py").read_text())
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for module in modules:
            assert not module.startswith("app.database"), module
            assert not module.startswith("sqlalchemy"), module


def test_the_connection_read_schema_carries_no_credential() -> None:
    from app.api.routes.integrations import ConnectionRead

    assert set(ConnectionRead.model_fields) == {
        "integration", "state", "connected", "granted_scopes", "required_scopes",
    }
    for forbidden in ("token", "secret", "code", "refresh", "access"):
        assert not any(
            forbidden in name for name in ConnectionRead.model_fields
        ), forbidden


# --- Refresh and revocation -------------------------------------------------


async def test_a_refresh_carries_the_refresh_token_forward() -> None:
    """Google usually omits it from a refresh response.

    Dropping it would disconnect the integration on the very first refresh.
    """
    transport = TokenTransport(
        payload={"access_token": "ya29.NEW", "expires_in": 3600}
    )
    existing = StoredToken(ACCESS, REFRESH, None, (CALENDAR_READONLY_SCOPE,))

    refreshed = await refresh_token(
        _client(transport), "cid", CLIENT_SECRET, existing
    )

    assert refreshed.access_token == "ya29.NEW"
    assert refreshed.refresh_token == REFRESH
    assert refreshed.scopes == (CALENDAR_READONLY_SCOPE,)


async def test_a_refresh_without_a_refresh_token_is_refused() -> None:
    transport = TokenTransport()
    token = StoredToken(ACCESS, "", None, ())

    with pytest.raises(OAuthError) as failure:
        await refresh_token(_client(transport), "cid", CLIENT_SECRET, token)

    assert failure.value.reason == "no_refresh_token"
    assert transport.connections == []


async def test_a_revoked_grant_surfaces_as_a_refusal() -> None:
    transport = TokenTransport(status_code=400, payload={"error": "invalid_grant"})
    token = StoredToken(ACCESS, REFRESH, None, (CALENDAR_READONLY_SCOPE,))

    with pytest.raises(OAuthError) as failure:
        await refresh_token(_client(transport), "cid", CLIENT_SECRET, token)

    assert failure.value.reason == "oauth_invalid_grant"


def test_an_expiring_token_is_treated_as_expired_early() -> None:
    """A token that expires mid-request is a race the caller cannot handle."""
    from app.integrations.token_store import EXPIRY_SKEW_SECONDS

    nearly = StoredToken(
        ACCESS, REFRESH,
        datetime.now(timezone.utc) + timedelta(seconds=EXPIRY_SKEW_SECONDS - 10),
        (),
    )
    assert nearly.expired

    comfortable = StoredToken(
        ACCESS, REFRESH,
        datetime.now(timezone.utc) + timedelta(seconds=EXPIRY_SKEW_SECONDS + 600),
        (),
    )
    assert not comfortable.expired


def test_a_token_with_no_expiry_is_treated_as_expired() -> None:
    """A lifetime the application cannot see is not one to gamble on."""
    assert StoredToken(ACCESS, REFRESH, None, ()).expired


# --- No second HTTP client (§4) ---------------------------------------------


def test_no_oauth_library_was_introduced() -> None:
    """Every OAuth library ships its own transport.

    Adding one would create exactly the unpoliced outbound path Stage 4F-C
    removed, and the repository-wide invariant would need an exception.
    """
    import ast

    backend = pathlib.Path(__file__).resolve().parents[2]
    for path in (backend / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                root = module.split(".")[0]
                assert root not in {
                    "authlib", "oauthlib", "requests_oauthlib", "google",
                    "googleapiclient", "google_auth_oauthlib",
                }, f"{path.name} imports {module}"


# --- Gaps mutation testing found --------------------------------------------


def test_a_lapsed_state_is_refused_by_the_expiry_check_itself(monkeypatch) -> None:
    """The check, not the sweep.

    Mutation testing found that removing the expiry check changed nothing:
    `_sweep` ran first and had already discarded the entry, so the guard was
    unreachable. `consume` now takes the entry before sweeping, which makes
    the explicit check the one that decides -- and testable.
    """
    import app.integrations.oauth as module

    pending = PendingAuthorizations()
    started = build_authorization_url(
        client_id="cid", redirect_uri=REDIRECT, scopes=REQUIRED_SCOPES,
        pending=pending,
    )

    # Freeze the sweep so only the expiry check can refuse it.
    monkeypatch.setattr(PendingAuthorizations, "_sweep", lambda self: None)
    real_monotonic = module.time.monotonic
    monkeypatch.setattr(
        module.time, "monotonic",
        lambda: real_monotonic() + module.STATE_TTL_SECONDS + 1,
    )

    assert pending.consume(started.state) is None


async def test_a_partial_grant_is_not_stored(monkeypatch, tmp_path) -> None:
    """Google lets a user deselect scopes at the consent screen.

    A grant that does not cover the read must not be saved: keeping it would
    leave the integration looking connected and failing on every use, and
    would hold a token Mai has no use for.
    """
    import app.api.routes.integrations as routes
    from app.integrations.http_client import SecureHttpClient

    settings = _settings(MAI_CREDENTIAL_DIR=str(tmp_path / "creds"))

    # A grant covering something else entirely.
    transport = TokenTransport(payload={
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/userinfo.email",
    })
    monkeypatch.setattr(
        "app.integrations.http_client.SecureHttpClient",
        lambda **kwargs: SecureHttpClient(
            policy=kwargs.get("policy") or token_policy(),
            transport=transport,
            resolve=lambda host, port: [(2, 1, 6, "", ("142.250.72.1", port))],
        ),
    )

    routes._PENDING.clear()
    started = build_authorization_url(
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
        scopes=REQUIRED_SCOPES,
        pending=routes._PENDING,
    )

    with pytest.raises(routes.OAuthFailed) as raised:
        await routes.google_callback(
            settings=settings,
            code="auth-code",
            state=started.state,
            # Explicit. Called directly rather than through FastAPI, an
            # omitted `error` is the `Query(...)` object, which is truthy --
            # the decline branch would fire and this test would pass without
            # ever reaching the scope check it exists for.
            error=None,
        )

    # The scope check refused it, not some earlier guard.
    assert "calendar events" in str(raised.value)

    # Nothing was written.
    store = FileTokenStore(settings.MAI_CREDENTIAL_DIR)
    assert store.load("google") is None


async def test_a_covering_grant_is_stored(monkeypatch, tmp_path) -> None:
    """The complement, so the test above cannot pass by nothing ever storing."""
    import app.api.routes.integrations as routes
    from app.integrations.http_client import SecureHttpClient

    settings = _settings(MAI_CREDENTIAL_DIR=str(tmp_path / "creds2"))
    # The route reads the registered integration, which resolves the
    # application's settings rather than a caller's -- deliberately, so
    # `/status` and the chat path cannot answer from different objects. A test
    # that wants different settings has to install them, as the app does.
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    transport = TokenTransport()
    monkeypatch.setattr(
        "app.integrations.http_client.SecureHttpClient",
        lambda **kwargs: SecureHttpClient(
            policy=kwargs.get("policy") or token_policy(),
            transport=transport,
            resolve=lambda host, port: [(2, 1, 6, "", ("142.250.72.1", port))],
        ),
    )

    routes._PENDING.clear()
    started = build_authorization_url(
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
        scopes=REQUIRED_SCOPES,
        pending=routes._PENDING,
    )

    result = await routes.google_callback(
        settings=settings, code="auth-code", state=started.state, error=None
    )

    assert result.connected is True
    assert tuple(result.granted_scopes) == (CALENDAR_READONLY_SCOPE,)
    # And the response carries no token.
    assert ACCESS not in result.model_dump_json()
    assert REFRESH not in result.model_dump_json()


async def test_status_and_chat_report_the_same_state(
    monkeypatch, tmp_path, db_session
) -> None:
    """Live verification found these disagreeing. Nothing in the suite did.

    `/status` built its own `GoogleCalendarIntegration`; the chat path looked
    the name up in the integration registry, where it was never registered.
    So a configured-but-unconnected instance told the API
    `authentication_required` and told the user "I don't have a Google
    Calendar integration configured for this instance" -- sending them to the
    operator for something one click would have fixed.

    Asserting the two agree is the assertion that has to hold however the
    objects are resolved.
    """
    import app.api.routes.integrations as routes
    from app.calendar.service import CalendarService
    from app.schemas.calendar import CalendarOutcome

    settings = _settings(
        MAI_CREDENTIAL_DIR=str(tmp_path / "creds3"),
        EXECUTION_ENABLED=True,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    status = await routes.google_status(settings)
    assert status.state == "authentication_required"
    assert status.connected is False

    import uuid as _uuid

    result = await CalendarService(session=db_session, settings=settings).handle(
        _uuid.uuid4(), "What's on my calendar today?"
    )

    # Not NOT_CONFIGURED: a client id is configured, the account simply is not
    # connected. The two answers must name the same situation.
    assert result.outcome is CalendarOutcome.NOT_CONNECTED
    assert "not configured" not in result.reply.lower()


def test_the_calendar_integration_is_registered() -> None:
    """The registry is how every other consumer finds it."""
    from app.integrations.registry import get_integration_registry

    integration = get_integration_registry().get("google_calendar")

    assert integration is not None
    assert integration.name == "google_calendar"


async def test_an_unwritable_credential_directory_does_not_half_connect(
    monkeypatch, tmp_path
) -> None:
    """Found in deployment, not in tests.

    The container's credential volume was created root-owned while the process
    runs unprivileged, so `save` raised `PermissionError` -- as a 500, at the
    very end of the flow, after the user had already granted access at Google
    and with nothing said about what to fix.

    The grant must not be treated as a connection when it was never stored.
    """
    import app.api.routes.integrations as routes
    from app.integrations.http_client import SecureHttpClient
    from app.integrations.token_store import TokenStoreError

    unwritable = tmp_path / "readonly"
    unwritable.mkdir()
    settings = _settings(MAI_CREDENTIAL_DIR=str(unwritable))
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    transport = TokenTransport()
    monkeypatch.setattr(
        "app.integrations.http_client.SecureHttpClient",
        lambda **kwargs: SecureHttpClient(
            policy=kwargs.get("policy") or token_policy(),
            transport=transport,
            resolve=lambda host, port: [(2, 1, 6, "", ("142.250.72.1", port))],
        ),
    )

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.integrations.token_store.os.open", refuse)

    routes._PENDING.clear()
    started = build_authorization_url(
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
        scopes=REQUIRED_SCOPES,
        pending=routes._PENDING,
    )

    with pytest.raises(routes.OAuthFailed) as raised:
        await routes.google_callback(
            settings=settings, code="auth-code", state=started.state, error=None
        )

    # Says what happened, and names no credential.
    message = str(raised.value)
    assert "could not be stored" in message
    assert ACCESS not in message and REFRESH not in message


def test_the_store_reports_an_unwritable_directory_as_its_own_error(
    tmp_path, monkeypatch
) -> None:
    """A raw OSError here becomes a 500 with nothing actionable in it."""
    from app.integrations.token_store import FileTokenStore, TokenStoreError

    store = FileTokenStore(str(tmp_path / "creds"))

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("app.integrations.token_store.os.open", refuse)

    with pytest.raises(TokenStoreError) as raised:
        store.save(
            "google", StoredToken(ACCESS, REFRESH, None, (CALENDAR_READONLY_SCOPE,))
        )

    assert raised.value.args[0] == "token_directory_not_writable"
