"""Connecting and disconnecting an external account.

Four endpoints, and none of them returns a token. The most important property
of this module is what it does **not** hand back: `/status` reports whether a
connection exists and which scopes were granted, never the values; `/connect`
returns a URL the user's browser follows, and the PKCE verifier that secures
it stays in this process.

The callback is the one endpoint a browser reaches with an authorization code
attached, and it is where CSRF is decided: the `state` must match an
in-flight authorization this process started, and consuming it removes it, so
a replayed callback finds nothing.
"""

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AppSettings
from app.core.errors import MaiError
from app.core.logging import get_logger
from app.integrations.credentials import CredentialStatus
from app.integrations.google_calendar import REQUIRED_SCOPES
from app.integrations.google_gmail import (
    REQUIRED_SCOPES as GMAIL_REQUIRED_SCOPES,
    TOKEN_PROVIDER as GMAIL_TOKEN_PROVIDER,
)
from app.integrations.oauth import (
    OAuthError,
    PendingAuthorizations,
    build_authorization_url,
    exchange_code,
    revoke,
    token_policy,
)
from app.integrations.token_store import FileTokenStore, TokenStoreError, StoredToken

logger = get_logger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

#: In-flight authorizations for this process.
#:
#: Deliberately in memory. A `state` and its verifier are meaningful for a few
#: minutes and are worthless afterwards; writing them to a database would make
#: a short-lived secret durable, and a restart losing them is the correct
#: behaviour rather than a bug.
_PENDING = PendingAuthorizations()


class ConnectionRead(BaseModel):
    """What a client learns about a connection. Never a credential.

    No token, no refresh token, no client secret, no account identifier, no
    expiry value that could be used to time anything. A test pins the field
    set so an addition has to be deliberate.
    """

    model_config = ConfigDict(frozen=True)

    integration: str
    #: `not_configured`, `authentication_required`, `available`, `disabled`.
    state: str
    connected: bool = False
    #: The scopes Google actually granted, so a user can see that read-only
    #: is what was asked for and what was given. Scope *names* are public
    #: URLs, not secrets.
    granted_scopes: tuple = ()
    #: What Mai asks for, so the two can be compared.
    required_scopes: tuple = ()


class AuthorizationStart(BaseModel):
    """Where to send the browser, and what Mai will be able to do."""

    model_config = ConfigDict(frozen=True)

    authorization_url: str
    #: Shown before the user leaves, so consent is informed by more than
    #: Google's own screen.
    disclosure: str


class DisconnectResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    disconnected: bool
    #: Whether Google confirmed the grant was withdrawn. A local delete
    #: happens either way -- leaving a token because a remote call failed
    #: would be the wrong direction.
    revoked_remotely: bool = False


class IntegrationNotConfigured(MaiError):
    status_code = 503
    code = "integration_not_configured"


class OAuthFailed(MaiError):
    status_code = 400
    code = "oauth_failed"


#: Shown before the user leaves for Google's consent screen.
#:
#: Google's own screen says what *Google* is granting. This says what *Mai*
#: will do with it, which is the part Google cannot tell them -- in
#: particular that calendar contents reach the configured model provider when
#: answering a question. That disclosure is what makes per-query confirmation
#: unnecessary later, so it belongs here in plain words.
DISCLOSURE = (
    "Mai is asking Google for read-only access to your calendar events. "
    "It will be able to read event titles, times, locations and organiser "
    "names, and it cannot create, change or delete anything. "
    "When you ask a calendar question, the matching events are sent to the "
    "configured AI model provider so it can answer — attendee lists, meeting "
    "links and email addresses are not. Access can be withdrawn at any time."
)


#: Shown before the user leaves for Google, and it says the parts that are
#: easy to leave out.
#:
#: `gmail.readonly` is a Google *restricted* scope: it grants read access to
#: the entire mailbox, not to a folder or a search. A disclosure that said
#: "read-only" and stopped there would be technically accurate and materially
#: misleading, because the two things people actually want to know are how
#: much is reachable and where it goes.
GMAIL_DISCLOSURE = (
    "Mai is asking Google for read-only access to your Gmail. This is a "
    "broad permission: it covers your whole mailbox, not a single folder or "
    "search. Mai cannot send, reply, forward, delete, archive, label or "
    "change anything.\n\n"
    "Mai reads mail only when you ask it to, never in the background, and it "
    "asks you to confirm each time before reading. When you ask a question "
    "about your mail, the senders, subjects and — if the question needs them "
    "— the message bodies of the matching messages are sent to the "
    "configured AI model provider so it can answer. Attachments are never "
    "read.\n\n"
    "This is separate from Calendar: connecting one does not connect the "
    "other. Access can be withdrawn at any time."
)


def _store(settings) -> FileTokenStore:
    return FileTokenStore(settings.MAI_CREDENTIAL_DIR)


def _integration(settings):
    """The registered integration, not a fresh one.

    This used to construct its own, which meant `/status` and the chat path
    consulted two different objects and could disagree -- and did: chat
    reported "not configured" for an integration this route reported as
    `authentication_required`. Both now read the same registration.
    """
    from app.integrations.registry import get_integration_registry

    return get_integration_registry().get("google_calendar")


@router.get(
    "/google/status",
    response_model=ConnectionRead,
    summary="Whether a Google account is connected. Returns no credential.",
)
async def google_status(settings: AppSettings) -> ConnectionRead:
    integration = _integration(settings)
    credential = integration.credential_state()

    return ConnectionRead(
        integration="google_calendar",
        state=integration.state().value,
        connected=credential.status is CredentialStatus.AVAILABLE,
        granted_scopes=tuple(credential.granted_scopes),
        required_scopes=tuple(sorted(REQUIRED_SCOPES)),
    )


@router.post(
    "/google/connect",
    response_model=AuthorizationStart,
    summary="Begin authorization. Returns a URL; starts nothing itself.",
)
async def google_connect(settings: AppSettings) -> AuthorizationStart:
    """Build the consent URL. Sends nothing to Google.

    Nothing is initiated silently: this returns a URL and a disclosure, and
    the user chooses whether to follow it. Mai does not open a browser, and
    no request reaches Google until the user's own browser makes one.
    """
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise IntegrationNotConfigured(
            "No Google OAuth client is configured for this instance."
        )

    try:
        started = build_authorization_url(
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
            scopes=REQUIRED_SCOPES,
            pending=_PENDING,
        )
    except OAuthError as failure:
        raise OAuthFailed(f"Could not begin authorization ({failure.reason}).")

    logger.info(
        "Google authorization started",
        # The scope names, which are public URLs. Never the state, which is
        # this flow's CSRF token.
        extra={"scopes": len(REQUIRED_SCOPES)},
    )
    return AuthorizationStart(
        authorization_url=started.authorization_url, disclosure=DISCLOSURE
    )


@router.get(
    "/google/callback",
    response_model=ConnectionRead,
    summary="Receive the authorization code. Verifies state, then exchanges.",
)
async def google_callback(
    settings: AppSettings,
    code: Optional[str] = Query(default=None, max_length=2048),
    state: Optional[str] = Query(default=None, max_length=512),
    error: Optional[str] = Query(default=None, max_length=200),
) -> ConnectionRead:
    """Complete the flow.

    `state` is checked first and consumed on use, so a replayed callback finds
    nothing and a forged one finds nothing either -- the value was generated
    here and never left this process except inside a URL the user followed.
    """
    if error:
        # Google reports a user decline this way. Not an error to shout about.
        logger.info("Google authorization was not completed")
        raise OAuthFailed("Authorization was not completed.")

    pending = _PENDING.consume(state or "")
    if pending is None:
        # Unknown, already used, or expired. All three mean the same thing to
        # a caller, and distinguishing them would confirm which states exist.
        logger.warning("Rejected a Google callback with an unrecognised state")
        raise OAuthFailed("That authorization request is no longer valid.")

    if not code:
        raise OAuthFailed("The authorization response carried no code.")

    client = None
    try:
        from app.integrations.http_client import SecureHttpClient

        client = SecureHttpClient(policy=token_policy())
        token = await exchange_code(
            client,
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            code=code,
            pending=pending,
        )
    except OAuthError as failure:
        logger.warning(
            "Google token exchange failed", extra={"reason": failure.reason}
        )
        raise OAuthFailed("Could not complete authorization with Google.")
    finally:
        if client is not None:
            await client.aclose()

    if not token.covers(REQUIRED_SCOPES):
        # Google lets a user deselect scopes. A grant that does not cover the
        # read is not stored: keeping it would leave the integration looking
        # connected and failing on every use.
        logger.warning("Google granted fewer scopes than were requested")
        raise OAuthFailed(
            "The granted permissions do not include reading calendar events."
        )

    try:
        _store(settings).save("google", token)
    except TokenStoreError as failure:
        # The grant is real but unusable: nothing was persisted, so the
        # integration stays disconnected rather than half-connected.
        logger.error(
            "Could not store the Google token", extra={"reason": failure.args[0]}
        )
        raise OAuthFailed(
            "Authorization succeeded but the credential could not be stored."
        )
    logger.info("Google account connected", extra={"scopes": len(token.scopes)})

    return await google_status(settings)


@router.post(
    "/google/disconnect",
    response_model=DisconnectResult,
    summary="Withdraw access and delete the stored token.",
)
async def google_disconnect(settings: AppSettings) -> DisconnectResult:
    """Revoke at Google, then delete locally. Deletes even if revocation fails."""
    store = _store(settings)
    try:
        token = store.load("google")
    except Exception:  # noqa: BLE001
        token = None

    revoked = False
    if token is not None:
        from app.integrations.http_client import SecureHttpClient

        client = SecureHttpClient(policy=token_policy())
        try:
            revoked = await revoke(client, token)
        finally:
            await client.aclose()

    deleted = store.delete("google")
    _PENDING.clear()
    logger.info("Google account disconnected", extra={"revoked": revoked})
    return DisconnectResult(disconnected=deleted, revoked_remotely=revoked)


# --- Gmail (Stage 5B) --------------------------------------------------------
#
# A separate integration end to end: its own consent, its own scope check, its
# own token file. The two flows share the OAuth machinery -- `_PENDING`, PKCE,
# one-shot state, redirect validation -- and share nothing else, which is what
# makes "Calendar is connected" and "Gmail is connected" independent facts.


def _gmail(settings):
    """The registered Gmail integration, not a fresh one.

    Reading the registry rather than constructing an instance is the fix
    Stage 4F-G had to make for Calendar after `/status` and the chat path
    disagreed. The same mistake is available here, so the same answer is
    used.
    """
    from app.integrations.registry import get_integration_registry

    return get_integration_registry().get("google_gmail")


@router.get(
    "/gmail/status",
    response_model=ConnectionRead,
    summary="Whether Gmail is connected. Returns no credential.",
)
async def gmail_status(settings: AppSettings) -> ConnectionRead:
    integration = _gmail(settings)
    credential = integration.credential_state()

    return ConnectionRead(
        integration="google_gmail",
        state=integration.state().value,
        connected=credential.status is CredentialStatus.AVAILABLE,
        granted_scopes=tuple(credential.granted_scopes),
        required_scopes=tuple(sorted(GMAIL_REQUIRED_SCOPES)),
    )


@router.post(
    "/gmail/connect",
    response_model=AuthorizationStart,
    summary="Begin Gmail authorization. Returns a URL; starts nothing itself.",
)
async def gmail_connect(settings: AppSettings) -> AuthorizationStart:
    """Build the consent URL for Gmail alone.

    `GMAIL_REQUIRED_SCOPES` and nothing else: the Calendar scope is not added,
    so a user connecting Gmail is not quietly asked for their calendar too.
    """
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise IntegrationNotConfigured(
            "No Google OAuth client is configured for this instance."
        )

    try:
        started = build_authorization_url(
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            redirect_uri=settings.GOOGLE_GMAIL_REDIRECT_URI,
            scopes=GMAIL_REQUIRED_SCOPES,
            pending=_PENDING,
        )
    except OAuthError as failure:
        raise OAuthFailed(f"Could not begin authorization ({failure.reason}).")

    logger.info(
        "Gmail authorization started",
        # A count of scope names, which are public URLs. Never the state,
        # which is this flow's CSRF token.
        extra={"scopes": len(GMAIL_REQUIRED_SCOPES)},
    )
    return AuthorizationStart(
        authorization_url=started.authorization_url, disclosure=GMAIL_DISCLOSURE
    )


@router.get(
    "/gmail/callback",
    response_model=ConnectionRead,
    summary="Receive the Gmail authorization code. Verifies state and scope.",
)
async def gmail_callback(
    settings: AppSettings,
    code: Optional[str] = Query(default=None, max_length=2048),
    state: Optional[str] = Query(default=None, max_length=512),
    error: Optional[str] = Query(default=None, max_length=200),
) -> ConnectionRead:
    """Complete the Gmail flow.

    Identical in shape to the Calendar callback and different in exactly two
    places: the scope it demands, and the provider key it stores under.
    """
    if error:
        logger.info("Gmail authorization was not completed")
        raise OAuthFailed("Authorization was not completed.")

    pending = _PENDING.consume(state or "")
    if pending is None:
        logger.warning("Rejected a Gmail callback with an unrecognised state")
        raise OAuthFailed("That authorization request is no longer valid.")

    if not code:
        raise OAuthFailed("The authorization response carried no code.")

    client = None
    try:
        from app.integrations.http_client import SecureHttpClient

        client = SecureHttpClient(policy=token_policy())
        token = await exchange_code(
            client,
            client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
            code=code,
            pending=pending,
        )
    except OAuthError as failure:
        logger.warning("Gmail token exchange failed", extra={"reason": failure.reason})
        raise OAuthFailed("Could not complete authorization with Google.")
    finally:
        if client is not None:
            await client.aclose()

    if not token.covers(GMAIL_REQUIRED_SCOPES):
        # Exact required-scope validation. Google lets a user deselect scopes
        # at the consent screen, and a *Calendar* grant arriving here would
        # also fail this check -- which is the point. Neither is stored:
        # keeping a grant that does not cover the read would leave Gmail
        # looking connected and failing on every use.
        logger.warning("Google granted fewer scopes than Gmail requires")
        raise OAuthFailed(
            "The granted permissions do not include reading Gmail."
        )

    try:
        _store(settings).save(GMAIL_TOKEN_PROVIDER, token)
    except TokenStoreError as failure:
        logger.error(
            "Could not store the Gmail token", extra={"reason": failure.args[0]}
        )
        raise OAuthFailed(
            "Authorization succeeded but the credential could not be stored."
        )

    logger.info("Gmail connected", extra={"scopes": len(token.scopes)})
    return await gmail_status(settings)


@router.post(
    "/gmail/disconnect",
    response_model=DisconnectResult,
    summary="Withdraw Gmail access and delete the stored token.",
)
async def gmail_disconnect(settings: AppSettings) -> DisconnectResult:
    """Revoke at Google, then delete locally. Deletes even if revocation fails."""
    store = _store(settings)
    revoked = False
    try:
        token = store.load(GMAIL_TOKEN_PROVIDER, "default")
    except Exception:  # noqa: BLE001
        token = None

    if token is not None:
        client = None
        try:
            from app.integrations.http_client import SecureHttpClient

            client = SecureHttpClient(policy=token_policy())
            revoked = await revoke(client, token)
        except Exception:  # noqa: BLE001
            # A revocation that fails must not stop the local delete. Leaving
            # the token on disk because Google was unreachable is the worse
            # of the two failures.
            logger.warning("Gmail token revocation failed")
        finally:
            if client is not None:
                await client.aclose()

    deleted = store.delete(GMAIL_TOKEN_PROVIDER, "default")
    logger.info("Gmail disconnected", extra={"revoked": revoked, "deleted": deleted})
    return DisconnectResult(disconnected=deleted, revoked_remotely=revoked)
