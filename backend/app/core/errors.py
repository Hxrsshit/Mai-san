"""Application error types and the HTTP responses they map to.

Services raise these; the API layer never has to build error payloads by hand.
Every handled error is returned to the client in a single, stable shape:

    {"error": {"code": "...", "message": "...", "request_id": "..."}}
"""

from typing import Dict, Optional


class MaiError(Exception):
    """Base class for all errors Mai raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."
    #: Response headers the error requires (Stage 6M.4: `WWW-Authenticate`).
    headers: Optional[Dict[str, str]] = None

    def __init__(self, message: Optional[str] = None) -> None:
        if message:
            self.message = message
        super().__init__(self.message)


class NotFoundError(MaiError):
    status_code = 404
    code = "not_found"
    message = "The requested resource was not found."


class ConversationNotFoundError(NotFoundError):
    code = "conversation_not_found"
    message = "Conversation not found."


class MemoryNotFoundError(NotFoundError):
    code = "memory_not_found"
    message = "Memory not found."


# --- Authentication (Stage 6M.4) --------------------------------------------
# Raised only by the request seam, `app.api.deps.get_owner_id`. The messages
# are fixed: none says whether a token was close, configured or malformed.


class AuthenticationRequired(MaiError):
    status_code = 401
    code = "authentication_required"
    message = "An API token is required: send Authorization: Bearer <token>."
    headers = {"WWW-Authenticate": "Bearer"}


class InvalidCredentials(MaiError):
    status_code = 401
    code = "invalid_credentials"
    message = "The API token was not accepted."
    headers = {"WWW-Authenticate": "Bearer"}


class AuthenticationNotConfigured(MaiError):
    status_code = 503
    code = "authentication_not_configured"
    message = "API authentication is not configured on this server."


# --- Membership (Stage 7A) --------------------------------------------------
# Raised by the request seam after the token is accepted: the token names a
# member, and the member must be active in an active organisation. Fixed
# messages; none says which of the two failed.


class MembershipInactive(MaiError):
    status_code = 403
    code = "membership_inactive"
    message = "This account is not an active member of an active organisation."


class MembershipUnavailable(MaiError):
    status_code = 503
    code = "membership_unavailable"
    message = "Membership could not be checked. Try again shortly."


class AdminRequired(MaiError):
    status_code = 403
    code = "admin_required"
    message = "This action requires an organisation Admin."


class SeamInvariantError(MaiError):
    """The seam found writes pending on the request session before it ran.
    Nothing may write before authentication; this is a programming error."""

    status_code = 500
    code = "seam_invariant"
    message = "The request could not be authenticated."


class ValidationError(MaiError):
    status_code = 422
    code = "validation_error"
    message = "The request was invalid."


class DatabaseError(MaiError):
    status_code = 503
    code = "database_error"
    message = "The database is currently unavailable."


# --- LLM errors -------------------------------------------------------------
# These are raised by providers and are intentionally provider-agnostic, so
# swapping one backend for another does not change how the API behaves.


class LLMError(MaiError):
    status_code = 502
    code = "llm_error"
    message = "The language model could not be reached."


class LLMTimeoutError(LLMError):
    status_code = 504
    code = "llm_timeout"
    message = "The language model took too long to respond."


class LLMAuthError(LLMError):
    status_code = 502
    code = "llm_auth_error"
    message = "The language model rejected the configured credentials."


class LLMRateLimitError(LLMError):
    status_code = 429
    code = "llm_rate_limited"
    message = "The language model is rate limiting requests. Try again shortly."


class LLMResponseError(LLMError):
    status_code = 502
    code = "llm_invalid_response"
    message = "The language model returned an unusable response."


class LLMNotConfiguredError(LLMError):
    status_code = 503
    code = "llm_not_configured"
    message = "No language model API key is configured on the server."
