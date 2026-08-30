"""Application error types and the HTTP responses they map to.

Services raise these; the API layer never has to build error payloads by hand.
Every handled error is returned to the client in a single, stable shape:

    {"error": {"code": "...", "message": "...", "request_id": "..."}}
"""

from typing import Optional


class MaiError(Exception):
    """Base class for all errors Mai raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

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
