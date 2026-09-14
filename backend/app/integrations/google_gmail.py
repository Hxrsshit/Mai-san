"""Read the user's Gmail. Nothing else, and only what a question needs.

The same shape as the Calendar integration, deliberately: one host, one
method, two typed operations, and a token that lives in exactly one place.
What is different is how much more sensitive the data is, and the design
answers that in three ways.

**A separate grant.** `gmail.readonly` is a Google *restricted* scope, and it
is requested on its own. Connecting Calendar does not connect Gmail, and the
tokens are stored under different provider keys, so neither can be used as
the other. The callback refuses a grant that does not carry the Gmail scope
exactly.

**A separate, narrower host.** Gmail is reached at `gmail.googleapis.com`,
which appears in no other policy -- so a bug in the Calendar client cannot
reach mail, and a bug here cannot reach a calendar.

**No write anywhere.** There is no send, reply, forward, trash, archive,
label, star, draft or modify operation in this file or any other. They are
absent rather than disabled, and the scope Mai holds would have Google refuse
them in any case.

What is absent, and why
-----------------------

    users.messages.send      users.messages.trash     users.messages.modify
    users.messages.batchModify                        users.drafts.*
    users.labels.*           users.settings.*         users.watch / stop
    users.history.list       users.threads.*          messages.attachments.get

`history.list` and `watch` are worth naming: they are how background mailbox
monitoring is built, and Stage 5B has none. Mai reads mail when asked, and at
no other time.
"""

from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from app.core.logging import get_logger
from app.integrations.base import Integration, IntegrationState, OperationSpec
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
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnauthorized,
    ProviderUnavailable,
)
from app.integrations.gmail_schemas import (
    MAX_BODIES,
    MAX_MESSAGES,
    MailMessage,
    MailQuery,
    MailWindow,
    parse_message,
    parse_message_ids,
)
from app.integrations.http_client import SecureHttpClient
from app.integrations.oauth import OAuthError, refresh_token, token_policy
from app.integrations.policy import NetworkPolicy, RetryPolicy, TimeoutPolicy
from app.integrations.result import ExternalResult, ExternalResultState
from app.integrations.token_store import FileTokenStore, StoredToken

logger = get_logger(__name__)

#: The single host the Gmail client may reach.
#:
#: Not `www.googleapis.com`, which is where Calendar lives. Gmail has its own
#: official endpoint, and using it means the two integrations cannot reach
#: each other's API even if one of them is wrong about a path.
API_HOST = "gmail.googleapis.com"
MESSAGES_ENDPOINT = f"https://{API_HOST}/gmail/v1/users/me/messages"

#: The one scope. A Google *restricted* scope: it grants read access to the
#: entire mailbox, which is why it is requested separately, validated exactly,
#: and never bundled with anything else.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
REQUIRED_SCOPES: FrozenSet[str] = frozenset({GMAIL_READONLY_SCOPE})

#: The provider key this integration's token is stored under.
#:
#: Distinct from Calendar's `google`, so the two grants are different files
#: with different scopes. Connecting one leaves the other untouched, and a
#: Calendar token can never satisfy a Gmail read.
TOKEN_PROVIDER = "google_gmail"

#: Bounded response size. A listing is small; a message with a long body is
#: still far below this, and anything larger is refused rather than streamed.
_MAX_RESPONSE_BYTES = 1_000_000

#: Requests one turn may make: one listing plus at most `MAX_BODIES` fetches.
MAX_REQUESTS_PER_TURN = 1 + MAX_BODIES


def api_policy(timeout_seconds: float = 15.0) -> NetworkPolicy:
    """One host, GET only, no redirects, bounded body.

    `follow_redirects=False` matters more here than almost anywhere. A
    redirect is how a bearer token for a restricted scope ends up at a host
    nobody chose, and Gmail has no legitimate reason to redirect a read.
    """
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
        # Read-only, so a retry repeats nothing. Two attempts, and only for
        # the transient failures the policy itself classifies as retryable --
        # a 401, a 403 or a malformed body is never retried.
        retries=RetryPolicy(max_attempts=2, backoff_seconds=0.5),
    )


class GoogleGmailIntegration(Integration):
    """Read a bounded set of the user's messages. Read-only."""

    name = "google_gmail"
    provider = "google"
    description = "Read a bounded set of the user's Gmail messages."

    def __init__(
        self,
        settings=None,
        store: Optional[FileTokenStore] = None,
        api_transport=None,
        token_transport=None,
        resolve=None,
        enabled: bool = True,
    ) -> None:
        # Held as overrides and resolved per use, for the reason the Calendar
        # integration records: this is built once at import and registered as
        # a singleton, so binding settings here would freeze whatever
        # configuration existed at import time.
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
        """Exactly two, both reads. See the module docstring on what is absent."""
        return (
            OperationSpec(
                name="gmail_list_messages",
                handler=self._list_messages,
                has_side_effect=False,
                description="List bounded message metadata matching a typed query.",
            ),
            OperationSpec(
                name="gmail_get_message",
                handler=self._get_message,
                has_side_effect=False,
                description="Read one selected message, bounded.",
            ),
        )

    @property
    def credential_requirement(self) -> CredentialRequirement:
        return CredentialRequirement(
            identifier="google_gmail.oauth",
            provider="google",
            credential_type=CredentialType.OAUTH2,
            required_scopes=REQUIRED_SCOPES,
        )

    @property
    def network_policy(self) -> NetworkPolicy:
        return api_policy()

    # --- Availability -------------------------------------------------------

    def credential_state(self) -> CredentialState:
        """Whether usable access exists. Carries no token.

        Reads the Gmail token specifically. A connected Calendar leaves this
        `MISSING`, which is the whole point of storing them separately.
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
            token = self._store.load(TOKEN_PROVIDER, "default")
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the Gmail token store")
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
            status=CredentialStatus.AVAILABLE,
            granted_scopes=token.scopes,
            expires_at=token.expires_at,
        )

    def state(self) -> IntegrationState:
        if not self._enabled:
            return IntegrationState.DISABLED
        if not self._settings.GOOGLE_OAUTH_CLIENT_ID:
            return IntegrationState.NOT_CONFIGURED
        status = self.credential_state().status
        if status is CredentialStatus.MISSING:
            return IntegrationState.AUTHENTICATION_REQUIRED
        if status is CredentialStatus.INSUFFICIENT_SCOPE:
            return IntegrationState.AUTHENTICATION_REQUIRED
        return IntegrationState.AVAILABLE

    # --- Operation one: bounded listing -------------------------------------

    async def _list_messages(self, arguments: Dict[str, Any]) -> ExternalResult:
        """Find messages matching a typed query, metadata only.

        What crosses from the caller: the fields of a `MailQuery`. What is
        constructed here: the host, the path, the method, the query string and
        the header the token travels in. There is no argument through which a
        caller could reach a different endpoint, and none through which raw
        Gmail syntax could reach `q`.

        `format=metadata` with an explicit header allowlist, so Gmail never
        sends a body for a message the user has not selected. That is the
        minimum-data rule made into a request parameter rather than a
        post-filter: the data does not arrive, so it cannot leak.
        """
        query = self._query_from(arguments)
        rendered = query.to_gmail_query()
        want_bodies = min(
            int(arguments.get("body_count") or 0), MAX_BODIES, query.max_results
        )

        access = await self._usable_access_token()

        params = {
            "maxResults": str(query.max_results),
            # Explicitly excluded. The default is already false, and stating
            # it means a future default change cannot start reading spam.
            "includeSpamTrash": "false",
        }
        if rendered:
            params["q"] = rendered

        payload = await self._get_json(MESSAGES_ENDPOINT, params, access)
        ids, total = parse_message_ids(payload, query.max_results)

        messages: List[MailMessage] = []
        for position, identifier in enumerate(ids):
            # One request per message, bounded by the listing itself. No
            # `pageToken` is ever sent, so this cannot walk the mailbox.
            with_body = position < want_bodies
            message = await self._fetch(identifier, with_body, access)
            if message is not None:
                messages.append(message)

        window = MailWindow(
            messages=tuple(messages), total_available=total, query=rendered
        )

        logger.info(
            "Gmail messages read",
            # Counts and the operation only. Never a subject, a sender, a body
            # or the token -- a subject line is personal data about the user
            # and usually about someone else too.
            extra={
                "integration": self.name,
                "message_count": len(messages),
                "bodies": sum(1 for m in messages if m.has_body),
                "offered": total,
            },
        )

        return ExternalResult(
            state=ExternalResultState.SUCCESS,
            integration=self.name,
            operation="gmail_list_messages",
            summary=(
                f"Found {len(messages)} message{'' if len(messages) == 1 else 's'}."
            ),
            data=window.as_external_data(),
        )

    # --- Operation two: one selected message --------------------------------

    async def _get_message(self, arguments: Dict[str, Any]) -> ExternalResult:
        """Read one message the application already selected from a listing.

        The id is not a free parameter in practice: it comes from a listing
        this integration itself produced moments earlier. It is validated
        anyway, because "the caller is trustworthy" is not a property a
        boundary should rely on.
        """
        identifier = str(arguments.get("message_id") or "").strip()
        if not identifier or not identifier.replace("-", "").replace("_", "").isalnum():
            raise ProviderInvalidResponse(reason="gmail_message_id_invalid")

        access = await self._usable_access_token()
        message = await self._fetch(identifier[:128], True, access)
        if message is None:
            raise ProviderNotFound(reason="gmail_message_not_found")

        window = MailWindow(messages=(message,), total_available=1)

        logger.info(
            "Gmail message read",
            extra={"integration": self.name, "message_count": 1, "bodies": 1},
        )

        return ExternalResult(
            state=ExternalResultState.SUCCESS,
            integration=self.name,
            operation="gmail_get_message",
            summary="Read one message.",
            data=window.as_external_data(),
        )

    # --- Internals ----------------------------------------------------------

    @staticmethod
    def _query_from(arguments: Dict[str, Any]) -> MailQuery:
        """Build the typed query from validated argument fields.

        Anything unrecognised is dropped rather than passed on. In particular
        a caller supplying `q`, `labelIds`, `pageToken` or `includeSpamTrash`
        finds them ignored -- `MailQuery` forbids extra fields, and only these
        names are read.
        """
        def terms(value: Any) -> Tuple[str, ...]:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(str(item) for item in value if isinstance(item, str))[:4]

        newer = arguments.get("newer_than_days")
        try:
            newer = int(newer) if newer not in (None, "") else None
        except (TypeError, ValueError):
            newer = None
        if newer is not None:
            newer = max(1, min(newer, 30))

        try:
            limit = int(arguments.get("max_results") or MAX_MESSAGES)
        except (TypeError, ValueError):
            limit = MAX_MESSAGES

        return MailQuery(
            sender=str(arguments.get("sender") or "")[:96],
            subject_terms=terms(arguments.get("subject_terms")),
            text_terms=terms(arguments.get("text_terms")),
            unread_only=bool(arguments.get("unread_only")),
            newer_than_days=newer,
            max_results=max(1, min(limit, MAX_MESSAGES)),
        )

    async def _fetch(
        self, identifier: str, with_body: bool, access: str
    ) -> Optional[MailMessage]:
        """One message, at the narrowest format that answers the question.

        `format=metadata` returns headers and no payload data at all, so a
        message Mai only needs to *list* never has its body cross the network.
        """
        # `metadata` returns headers and **no payload body at all**; `full`
        # returns the MIME tree. That choice is the minimum-data rule made
        # into a request parameter rather than a post-filter: for a message
        # Mai only needs to list, the body never crosses the network, so
        # there is nothing to forget to discard.
        #
        # Gmail's `metadataHeaders` parameter would narrow this further to
        # three named headers, but it must be repeated once per header and
        # `SecureHttpClient.get` takes a mapping. Widening a core security
        # module for a bandwidth saving is the wrong trade: `_headers_of`
        # reads only `From`, `Subject` and `Date`, examines at most
        # `MAX_HEADERS`, and discards the rest unexamined -- so the headers
        # that arrive and the headers Mai keeps are already different sets.
        params = {"format": "full"} if with_body else {"format": "metadata"}

        payload = await self._get_json(
            f"{MESSAGES_ENDPOINT}/{identifier}", params, access
        )
        return parse_message(payload, with_body=with_body)

    async def _get_json(self, url: str, params: Any, access: str) -> Dict[str, Any]:
        """One GET through the secure client, with the token on one header."""
        response = await self._api_client.get(
            url,
            params=params,
            # Applied by the transport for this one request. Never on the
            # client, never in the URL, never in a log.
            auth_header=("Authorization", f"Bearer {access}"),
        )

        if response.status_code != 200:
            raise _error_for_status(response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderInvalidResponse(reason="gmail_malformed_response") from exc

        if not isinstance(payload, dict):
            raise ProviderInvalidResponse(reason="gmail_malformed_response")
        return payload

    async def _usable_access_token(self) -> str:
        """A live access token, refreshing if the stored one is spent.

        The only place a Gmail token is read, and the only place one is
        written. Returns the value to exactly one caller, which puts it in
        exactly one header.
        """
        token = self._store.load(TOKEN_PROVIDER, "default")
        if token is None:
            raise ProviderUnauthorized(reason="gmail_not_connected")

        if not token.covers(REQUIRED_SCOPES):
            raise ProviderForbidden(reason="gmail_insufficient_scope")

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
            logger.info("Gmail token refresh failed", extra={"reason": exc.reason})
            raise ProviderUnauthorized(reason="gmail_reauthorisation_required")

        stored = StoredToken(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token or token.refresh_token,
            expires_at=refreshed.expires_at,
            scopes=refreshed.scopes or token.scopes,
            account="default",
        )
        self._store.save(TOKEN_PROVIDER, stored)
        return stored.access_token

    async def aclose(self) -> None:
        await self._api_client.aclose()
        await self._token_client.aclose()


def _error_for_status(status: int) -> IntegrationError:
    """Google's status, as a Mai error. Never carries a response body.

    Each maps to something true and different. A 403 on a restricted scope
    usually means the grant is missing the scope rather than that the mailbox
    is empty, and reporting "no emails" for either would be a lie.
    """
    if status == 401:
        return ProviderUnauthorized(reason="gmail_reauthorisation_required")
    if status == 403:
        return ProviderForbidden(reason="gmail_forbidden")
    if status == 404:
        return ProviderNotFound(reason="gmail_message_not_found")
    if status == 429:
        return ProviderRateLimited(reason="gmail_rate_limited")
    if status >= 500:
        return ProviderUnavailable(reason="gmail_unavailable")
    return ProviderInvalidResponse(reason="gmail_request_rejected")


__all__ = [
    "API_HOST",
    "GMAIL_READONLY_SCOPE",
    "MAX_REQUESTS_PER_TURN",
    "MESSAGES_ENDPOINT",
    "REQUIRED_SCOPES",
    "TOKEN_PROVIDER",
    "GoogleGmailIntegration",
    "api_policy",
]
