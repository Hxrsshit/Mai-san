"""OAuth 2.0 authorization code flow with PKCE, over Mai's own network boundary.

Google's documented flow for an installed application: authorization code,
PKCE with S256, a loopback redirect, and `state` for CSRF. Two endpoints
matter, and they are on different hosts with different roles:

    accounts.google.com     the consent screen. The *user's browser* goes
                            there. Mai never connects to it.
    oauth2.googleapis.com   the token endpoint. Mai POSTs here, through
                            `SecureHttpClient`, under a policy that permits
                            that one host and that one verb.

Why no OAuth library
--------------------

Every OAuth client library ships its own HTTP transport. Adding one would
create exactly the unpoliced outbound path Stage 4F-C spent a stage removing,
and Mai's repository-wide invariant -- one module may import an HTTP client --
would have to grow an exception.

What is left once the transport is Mai's own is not cryptography to invent. It
is `secrets.token_urlsafe` for the state and verifier, one `hashlib.sha256`
for the challenge, and `base64.urlsafe_b64encode` -- standard-library
primitives used as documented, not primitives implemented. The dependency
review in the stage brief asks for exactly that trade to be made explicitly,
so it is made here and recorded.

What never leaves this module
-----------------------------

Access and refresh tokens. They go from the token endpoint into the token
store and from the store into an `Authorization` header. There is no path from
either into a prompt, a log line, an audit record, an API response or an
exception message, and tests assert each.
"""

import base64
import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, FrozenSet, NamedTuple, Optional, Tuple
from urllib.parse import urlencode, urlparse

from app.core.logging import get_logger
from app.integrations.errors import IntegrationError
from app.integrations.http_client import SecureHttpClient
from app.integrations.policy import NetworkPolicy, RetryPolicy, TimeoutPolicy
from app.integrations.token_store import StoredToken

logger = get_logger(__name__)

#: Where the user's browser goes. Mai never connects to this host, and it is
#: deliberately absent from every network policy below.
AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

#: Where Mai exchanges and refreshes. The one host the token client may reach.
TOKEN_HOST = "oauth2.googleapis.com"
TOKEN_ENDPOINT = f"https://{TOKEN_HOST}/token"

#: Where a grant is withdrawn. Same host, so the same policy covers it.
REVOKE_ENDPOINT = f"https://{TOKEN_HOST}/revoke"

#: How long an in-flight authorization may take before its state expires.
#:
#: Ten minutes is long enough to read a consent screen and short enough that a
#: state value left in memory is not a standing invitation.
STATE_TTL_SECONDS = 600

#: A verifier must be 43-128 characters per RFC 7636. 64 random bytes,
#: base64url-encoded, lands comfortably inside that.
_VERIFIER_BYTES = 64


class OAuthError(IntegrationError):
    """An OAuth failure. Carries a reason code and never token material."""

    reason = "oauth_failed"


def token_policy(timeout_seconds: float = 20.0) -> NetworkPolicy:
    """The policy for the token endpoint. One host, POST only, no redirects.

    A token endpoint that redirected would be sending a client credential and
    an authorization code somewhere the origin chose, so redirects are refused
    rather than re-checked.
    """
    return NetworkPolicy(
        allowed_hosts=frozenset({TOKEN_HOST}),
        allowed_methods=frozenset({"POST"}),
        follow_redirects=False,
        max_response_bytes=64_000,
        timeouts=TimeoutPolicy(
            connect_seconds=5.0,
            read_seconds=timeout_seconds,
            total_seconds=timeout_seconds + 5.0,
        ),
        retries=RetryPolicy(max_attempts=1),
        # The token endpoint takes form-encoded input, which the client sends
        # as JSON by default -- so the content type is named here rather than
        # opened for every caller.
        extra_request_headers=frozenset({"content-type"}),
    )


class PendingAuthorization(NamedTuple):
    """One in-flight authorization. Held in memory, never persisted.

    The verifier is a secret for the duration of the exchange: whoever holds
    it and the code can complete the flow. It lives in this process and is
    discarded the moment it is used or expires.
    """

    state: str
    code_verifier: str
    redirect_uri: str
    scopes: Tuple[str, ...]
    created_at: float

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > STATE_TTL_SECONDS


class AuthorizationStarts(NamedTuple):
    """What a caller needs to begin. Carries no secret.

    The `code_verifier` is deliberately absent: the URL is shown to a user and
    may be logged by a browser, and the verifier must not travel with it.
    """

    authorization_url: str
    state: str


class PendingAuthorizations:
    """In-memory store for in-flight `state` values.

    One-shot by construction: `consume` removes the entry, so a replayed
    callback finds nothing. That is the CSRF and replay guard, and it is a
    property of the data structure rather than a check someone must remember.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, PendingAuthorization] = {}

    def add(self, pending: PendingAuthorization) -> None:
        self._sweep()
        self._entries[pending.state] = pending

    def consume(self, state: str) -> Optional[PendingAuthorization]:
        """Take the entry for this state, once. None if absent or expired.

        The order matters, and mutation testing is why it is written this way.
        Sweeping *first* made the expiry check below unreachable: a lapsed
        entry was already gone, so removing the check changed nothing and no
        test could tell. The guard that decides is now the explicit one, and
        the sweep afterwards is memory hygiene rather than correctness.
        """
        pending = self._entries.pop(state or "", None)
        self._sweep()
        if pending is None or pending.expired:
            return None
        return pending

    def clear(self) -> None:
        self._entries.clear()

    def _sweep(self) -> None:
        for state in [s for s, p in self._entries.items() if p.expired]:
            self._entries.pop(state, None)

    def __len__(self) -> int:
        return len(self._entries)


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    scopes: FrozenSet[str],
    pending: PendingAuthorizations,
) -> AuthorizationStarts:
    """Build the consent URL and remember what it must come back with.

    `access_type=offline` and `prompt=consent` are requested so a refresh
    token is issued. Without them Google returns one only on the very first
    grant, and an integration that silently stops working after an hour is
    worse than one that asks once more.
    """
    if not client_id:
        raise OAuthError(reason="oauth_not_configured")

    validate_redirect_uri(redirect_uri)

    verifier = _new_verifier()
    state = secrets.token_urlsafe(32)

    pending.add(
        PendingAuthorization(
            state=state,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            scopes=tuple(sorted(scopes)),
            created_at=time.monotonic(),
        )
    )

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            # Space-delimited, sorted, so the requested set is deterministic
            # and a test can assert it exactly.
            "scope": " ".join(sorted(scopes)),
            "state": state,
            "code_challenge": _challenge_for(verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
        }
    )

    return AuthorizationStarts(
        authorization_url=f"{AUTHORIZE_ENDPOINT}?{query}", state=state
    )


def validate_redirect_uri(redirect_uri: str) -> None:
    """Refuse anything but a loopback redirect on an ordinary port.

    Google's installed-app flow permits `http://127.0.0.1:port/path`, and
    loopback is the point: the authorization code is delivered to a listener
    on the user's own machine and never crosses a network. A redirect URI
    pointing anywhere else would hand the code to whoever owns that host.

    Checked when the URL is built *and* when the callback is handled, so a
    tampered `redirect_uri` cannot be introduced between the two.
    """
    try:
        parsed = urlparse((redirect_uri or "").strip())
    except ValueError as exc:
        raise OAuthError(reason="invalid_redirect_uri") from exc

    if parsed.scheme != "http":
        # Deliberately http, not https: a loopback listener has no
        # certificate, and requiring https here would push people to a
        # non-loopback redirect, which is the thing worth preventing.
        raise OAuthError(reason="invalid_redirect_uri")

    if parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise OAuthError(reason="invalid_redirect_uri")

    if parsed.port is None or not (1024 <= parsed.port <= 65535):
        raise OAuthError(reason="invalid_redirect_uri")

    if parsed.query or parsed.fragment:
        # A redirect URI carrying its own parameters is a smuggling surface,
        # and Google requires an exact registered match anyway.
        raise OAuthError(reason="invalid_redirect_uri")


async def exchange_code(
    client: SecureHttpClient,
    client_id: str,
    client_secret: str,
    code: str,
    pending: PendingAuthorization,
) -> StoredToken:
    """Trade an authorization code for tokens. Verifies the redirect again."""
    validate_redirect_uri(pending.redirect_uri)

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": pending.code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": pending.redirect_uri,
    }
    data = await _post_form(client, TOKEN_ENDPOINT, payload)
    return _token_from(data, fallback_refresh="")


async def refresh_token(
    client: SecureHttpClient,
    client_id: str,
    client_secret: str,
    token: StoredToken,
) -> StoredToken:
    """Exchange a refresh token for a fresh access token.

    Google usually omits the refresh token from a refresh response, so the
    existing one is carried forward rather than lost -- dropping it would
    disconnect the integration on the first refresh.
    """
    if not token.refresh_token:
        raise OAuthError(reason="no_refresh_token")

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": token.refresh_token,
        "grant_type": "refresh_token",
    }
    data = await _post_form(client, TOKEN_ENDPOINT, payload)
    refreshed = _token_from(data, fallback_refresh=token.refresh_token)

    # A refresh response may omit `scope`; the grant did not change.
    if not refreshed.scopes:
        refreshed = StoredToken(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            expires_at=refreshed.expires_at,
            scopes=token.scopes,
            account=token.account,
        )
    return refreshed


async def revoke(client: SecureHttpClient, token: StoredToken) -> bool:
    """Ask Google to invalidate the grant. Best effort, never raises.

    A revocation that fails still leads to the local token being deleted --
    leaving it on disk because the remote call failed would be the wrong
    direction. The return value says whether the remote side confirmed.
    """
    target = token.refresh_token or token.access_token
    if not target:
        return False
    try:
        await _post_form(client, REVOKE_ENDPOINT, {"token": target})
        return True
    except IntegrationError as exc:
        logger.info("Token revocation was not confirmed", extra={"reason": exc.reason})
        return False


# --- Internals --------------------------------------------------------------


def _new_verifier() -> str:
    return base64.urlsafe_b64encode(
        secrets.token_bytes(_VERIFIER_BYTES)
    ).decode("ascii").rstrip("=")


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def _post_form(
    client: SecureHttpClient, url: str, payload: Dict[str, str]
) -> Dict[str, object]:
    """POST form-encoded data and decode the JSON reply.

    Errors are converted to a reason code here. A token endpoint's error body
    can echo the request, and the request contains the client secret and the
    code -- so the body is never carried into an exception.
    """
    try:
        response = await client.post_form(url, form=payload)
    except IntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(reason="oauth_transport_failed") from exc

    if response.status_code != 200:
        logger.warning(
            "OAuth token request failed",
            # The status only. Never the body, never the payload.
            extra={"status": response.status_code},
        )
        raise OAuthError(
            reason="oauth_invalid_grant" if response.status_code in (400, 401)
            else "oauth_upstream_error"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise OAuthError(reason="oauth_malformed_response") from exc

    if not isinstance(data, dict):
        raise OAuthError(reason="oauth_malformed_response")
    return data


def _token_from(data: Dict[str, object], fallback_refresh: str) -> StoredToken:
    """Read a token response field by field. Never `StoredToken(**data)`."""
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise OAuthError(reason="oauth_no_access_token")

    expires_in = data.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    raw_scope = data.get("scope")
    scopes: Tuple[str, ...] = ()
    if isinstance(raw_scope, str) and raw_scope.strip():
        scopes = tuple(sorted(raw_scope.split()))

    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = fallback_refresh

    return StoredToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        scopes=scopes,
    )


__all__ = [
    "AUTHORIZE_ENDPOINT",
    "REVOKE_ENDPOINT",
    "STATE_TTL_SECONDS",
    "TOKEN_ENDPOINT",
    "TOKEN_HOST",
    "AuthorizationStarts",
    "OAuthError",
    "PendingAuthorization",
    "PendingAuthorizations",
    "build_authorization_url",
    "exchange_code",
    "refresh_token",
    "revoke",
    "validate_redirect_uri",
]
