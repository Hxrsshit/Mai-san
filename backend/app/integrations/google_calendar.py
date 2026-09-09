"""Google Calendar, read-only, through Mai's own network boundary.

One operation exists: `calendar_list_events`. There is no create, no update,
no delete, no invite -- not disabled, not gated, **absent**. A capability that
does not exist cannot be reached by a bug, a prompt injection, or a mistaken
authorization decision, and the OAuth scope is narrow enough that Google would
refuse a write even if one were somehow attempted.

Two clients, because the work has two shapes
--------------------------------------------

    oauth2.googleapis.com    POST, form-encoded, for tokens
    www.googleapis.com       GET, for events

Each client carries a policy naming one host and one verb. A single policy
permitting both would let the token client reach the API and the API client
reach the token endpoint, and neither needs to. `accounts.google.com` appears
in neither: the consent screen is somewhere the *user's browser* goes, and Mai
never connects to it.

The access token
----------------

Fetched from the store, refreshed if spent, and applied by the transport as an
`Authorization` header for exactly one request. It is never stored on a
client, never placed in a URL or a body, never logged, and never returned in
any object this module hands back.
"""

from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional, Tuple

from app.core.logging import get_logger
from app.integrations.base import Integration, IntegrationState, OperationSpec
from app.integrations.calendar_schemas import (
    MAX_EVENTS,
    CalendarWindow,
    parse_events,
)
from app.integrations.credentials import (
    CredentialRequirement,
    CredentialState,
    CredentialStatus,
    CredentialType,
)
from app.integrations.errors import (
    IntegrationError,
    ProviderForbidden,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnauthorized,
    ProviderUnavailable,
)
from app.integrations.http_client import SecureHttpClient
from app.integrations.oauth import (
    OAuthError,
    refresh_token,
    token_policy,
)
from app.integrations.policy import NetworkPolicy, RetryPolicy, TimeoutPolicy
from app.integrations.result import ExternalResult, ExternalResultState
from app.integrations.token_store import FileTokenStore, StoredToken

logger = get_logger(__name__)

#: The single host the Calendar API client may reach.
API_HOST = "www.googleapis.com"
EVENTS_ENDPOINT = f"https://{API_HOST}/calendar/v3/calendars/primary/events"

#: The narrowest scope that answers "what's on my calendar?".
#:
#: `calendar.events.readonly`, not `calendar.readonly`. The wider one also
#: grants calendar metadata, settings and access-control lists; none is needed
#: to list events, and a scope is issued once and cannot be narrowed
#: afterwards. Google documents nineteen Calendar scopes and this is the
#: smallest that covers the operation.
#:
#: Notably absent: every write scope, Gmail, Drive, Contacts, and any
#: full-account scope.
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
REQUIRED_SCOPES: FrozenSet[str] = frozenset({CALENDAR_READONLY_SCOPE})

#: Bounds on one API call.
MAX_RESULTS = MAX_EVENTS
_MAX_RESPONSE_BYTES = 512_000


def api_policy(timeout_seconds: float = 15.0) -> NetworkPolicy:
    """One host, GET only, no redirects, bounded body."""
    return NetworkPolicy(
        allowed_hosts=frozenset({API_HOST}),
        allowed_methods=frozenset({"GET"}),
        follow_redirects=False,
        max_response_bytes=_MAX_RESPONSE_BYTES,
        timeouts=TimeoutPolicy(
            connect_seconds=5.0,
            read_seconds=timeout_seconds,
            total_seconds=timeout_seconds + 5.0,
        ),
        # Read-only, so a retry repeats nothing. Still bounded.
        retries=RetryPolicy(max_attempts=2, backoff_seconds=0.5),
    )


class GoogleCalendarIntegration(Integration):
    """Read a window of the user's calendar. Nothing else."""

    name = "google_calendar"
    provider = "google"
    description = "Read events from the user's Google Calendar."

    def __init__(
        self,
        settings=None,
        store: Optional[FileTokenStore] = None,
        api_transport=None,
        token_transport=None,
        resolve=None,
        enabled: bool = True,
    ) -> None:
        # Held as overrides, resolved per use. This integration is built once
        # at import and registered as a singleton, so binding a settings object
        # here would freeze whatever configuration existed at import time --
        # and a caller passing different settings (a different credential
        # directory, a client id configured later) would be silently ignored.
        self._settings_override = settings
        self._store_override = store
        self._resolve = resolve

        self._api_client = SecureHttpClient(
            policy=api_policy(), transport=api_transport, resolve=resolve
        )
        self._token_client = SecureHttpClient(
            policy=token_policy(), transport=token_transport, resolve=resolve
        )
        super().__init__(credentials=None, enabled=enabled)

    @property
    def _settings(self):
        if self._settings_override is not None:
            return self._settings_override
        from app.core.config import get_settings

        return get_settings()

    @property
    def _store(self) -> FileTokenStore:
        if self._store_override is not None:
            return self._store_override
        return FileTokenStore(self._settings.MAI_CREDENTIAL_DIR)

    # --- Declaration --------------------------------------------------------

    def declare_operations(self) -> Tuple[OperationSpec, ...]:
        """Exactly one. See the module docstring on what is absent."""
        return (
            OperationSpec(
                name="calendar_list_events",
                handler=self._list_events,
                # Read-only: it changes nothing at Google, which is what makes
                # a retry safe.
                has_side_effect=False,
                description="List events in a bounded time window.",
            ),
        )

    @property
    def credential_requirement(self) -> CredentialRequirement:
        return CredentialRequirement(
            identifier="google_calendar.oauth",
            provider="google",
            credential_type=CredentialType.OAUTH2,
            # No `setting_name`: an OAuth token does not come from a setting.
            # It is issued by Google and held in the token store.
            required_scopes=REQUIRED_SCOPES,
        )

    @property
    def network_policy(self) -> NetworkPolicy:
        return api_policy()

    # --- Availability -------------------------------------------------------

    def credential_state(self) -> CredentialState:
        """Whether usable access exists. Carries no token.

        Four answers, and they are genuinely different things to tell an
        operator: not configured at all, configured but never connected,
        connected but with the wrong scopes, connected and usable.
        """
        requirement = self.credential_requirement

        if not self._settings.GOOGLE_OAUTH_CLIENT_ID:
            return CredentialState(
                identifier=requirement.identifier,
                provider=requirement.provider,
                credential_type=CredentialType.OAUTH2,
                status=CredentialStatus.MISSING,
            )

        try:
            token = self._store.load("google", "default")
        except Exception:  # noqa: BLE001
            # A store that cannot be read is reported as missing rather than
            # raising. The failure direction is "no access".
            logger.warning("Could not read the Google token store")
            token = None

        if token is None:
            return CredentialState(
                identifier=requirement.identifier,
                provider=requirement.provider,
                credential_type=CredentialType.OAUTH2,
                status=CredentialStatus.MISSING,
            )

        if not token.covers(REQUIRED_SCOPES):
            return CredentialState(
                identifier=requirement.identifier,
                provider=requirement.provider,
                credential_type=CredentialType.OAUTH2,
                status=CredentialStatus.INSUFFICIENT_SCOPE,
                granted_scopes=token.scopes,
                expires_at=token.expires_at,
            )

        return CredentialState(
            identifier=requirement.identifier,
            provider=requirement.provider,
            credential_type=CredentialType.OAUTH2,
            # An expired *access* token with a refresh token is still usable
            # access: the refresh happens inside the call. Reporting it as
            # expired would make a working integration look broken.
            status=CredentialStatus.AVAILABLE,
            granted_scopes=token.scopes,
            expires_at=token.expires_at,
        )

    def state(self) -> IntegrationState:
        if not self._enabled:
            return IntegrationState.DISABLED
        if not self._settings.GOOGLE_OAUTH_CLIENT_ID:
            return IntegrationState.NOT_CONFIGURED
        if self.credential_state().status is CredentialStatus.MISSING:
            # Configured, but the user has not connected an account. The
            # state that tells a UI to offer the connect flow.
            return IntegrationState.AUTHENTICATION_REQUIRED
        if self.credential_state().status is CredentialStatus.INSUFFICIENT_SCOPE:
            return IntegrationState.AUTHENTICATION_REQUIRED
        return IntegrationState.AVAILABLE

    # --- The one operation --------------------------------------------------

    async def _list_events(self, arguments: Dict[str, Any]) -> ExternalResult:
        """List events in a window. Builds the request; never receives one.

        What crosses from the caller: two timestamps and a bounded count.
        What is constructed here: the URL, the method, the parameters, and the
        header the token travels in. There is no argument through which a
        caller could reach a different endpoint.
        """
        from app.integrations.calendar_schemas import CalendarWindow

        starts_at = _iso(arguments.get("starts_at"))
        ends_at = _iso(arguments.get("ends_at"))
        limit = max(1, min(int(arguments.get("max_results", 10)), MAX_RESULTS))

        if not starts_at or not ends_at:
            raise ProviderInvalidResponse(reason="calendar_window_invalid")

        access = await self._usable_access_token()

        response = await self._api_client.get(
            EVENTS_ENDPOINT,
            params={
                "timeMin": starts_at,
                "timeMax": ends_at,
                "maxResults": str(limit),
                "singleEvents": "true",
                "orderBy": "startTime",
            },
            # Applied by the transport for this one request. Never on the
            # client, never in the URL, never in a log.
            auth_header=("Authorization", f"Bearer {access}"),
        )

        if response.status_code != 200:
            raise _error_for_status(response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderInvalidResponse(reason="calendar_malformed_response") from exc

        if not isinstance(payload, dict):
            raise ProviderInvalidResponse(reason="calendar_malformed_response")

        events, offered = parse_events(payload, max_events=limit)
        window = CalendarWindow(
            events=events,
            starts_at=starts_at,
            ends_at=ends_at,
            total_available=offered,
        )

        logger.info(
            "Calendar window read",
            # Counts and the window only. Never a title, a location, an
            # attendee or the token -- an event title is personal data about
            # the user and often about someone else.
            extra={
                "integration": self.name,
                "event_count": len(events),
                "offered": offered,
                "latency_ms": response.elapsed_ms,
            },
        )

        return ExternalResult(
            state=ExternalResultState.SUCCESS,
            integration=self.name,
            operation="calendar_list_events",
            summary=(
                f"Found {len(events)} event{'' if len(events) == 1 else 's'}."
            ),
            data=window.as_external_data(),
        )

    # --- Token handling -----------------------------------------------------

    async def _usable_access_token(self) -> str:
        """A live access token, refreshing if the stored one is spent.

        The only place a token is read, and the only place one is written.
        Returns the value to exactly one caller, which puts it in exactly one
        header.
        """
        token = self._store.load("google", "default")
        if token is None:
            raise ProviderUnauthorized(reason="calendar_not_connected")

        if not token.covers(REQUIRED_SCOPES):
            raise ProviderForbidden(reason="calendar_insufficient_scope")

        if not token.expired:
            return token.access_token

        try:
            refreshed = await refresh_token(
                self._token_client,
                client_id=self._settings.GOOGLE_OAUTH_CLIENT_ID,
                client_secret=self._settings.GOOGLE_OAUTH_CLIENT_SECRET,
                token=token,
            )
        except OAuthError as exc:
            # A refresh that fails means the grant is gone -- revoked, or the
            # refresh token rotated away. Reported as "reconnect", which is
            # the action that fixes it.
            logger.info(
                "Google token refresh failed",
                extra={"reason": exc.reason},
            )
            raise ProviderUnauthorized(reason="calendar_reauthorisation_required")

        refreshed = StoredToken(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token or token.refresh_token,
            expires_at=refreshed.expires_at,
            scopes=refreshed.scopes or token.scopes,
            account="default",
        )
        self._store.save("google", refreshed)
        return refreshed.access_token

    async def aclose(self) -> None:
        await self._api_client.aclose()
        await self._token_client.aclose()


def _iso(value: Any) -> str:
    """An RFC-3339 timestamp, or `""`.

    Accepts a `datetime` or an ISO string and emits the string form Google
    expects. Anything else is refused rather than coerced: a window Mai
    cannot express is one it should not guess at.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    if isinstance(value, str) and value.strip():
        cleaned = value.strip()[:40]
        # Must at least look like a date. A free-form string is not a window.
        if len(cleaned) >= 10 and cleaned[4] == "-" and cleaned[7] == "-":
            return cleaned
    return ""


def _error_for_status(status: int) -> IntegrationError:
    """Google's status, as a Mai error. Never carries a response body."""
    if status == 401:
        return ProviderUnauthorized(reason="calendar_reauthorisation_required")
    if status == 403:
        return ProviderForbidden(reason="calendar_forbidden")
    if status == 429:
        return ProviderRateLimited(reason="calendar_rate_limited")
    if status >= 500:
        return ProviderUnavailable(reason="calendar_unavailable")
    return ProviderInvalidResponse(reason="calendar_request_rejected")


__all__ = [
    "API_HOST",
    "CALENDAR_READONLY_SCOPE",
    "EVENTS_ENDPOINT",
    "MAX_RESULTS",
    "REQUIRED_SCOPES",
    "GoogleCalendarIntegration",
    "api_policy",
]
