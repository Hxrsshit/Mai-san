"""Shared implementation for OpenAI-compatible chat completions APIs.

Groq (and OpenAI, Together, Fireworks, vLLM, ...) all expose the same schema:

    POST {base_url}/chat/completions
    Authorization: Bearer <api_key>

so one implementation serves them all, and adding another gateway is a
subclass with different defaults rather than a second copy of the retry,
error-mapping and parsing logic.

Concrete providers live alongside this module (see `groq.py`).
"""

import asyncio
import random
from typing import Any, Dict, List, Optional


from app.core.config import Settings
from app.integrations.errors import (
    IntegrationError,
    NetworkPolicyViolation,
    ProviderTimeout,
    ResponseTooLarge,
)
from app.integrations.http_client import SecureHttpClient
from app.llm.transport import build_provider_client
from app.core.errors import (
    LLMAuthError,
    LLMError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.core.logging import get_logger, redact
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, ProviderHealth

logger = get_logger(__name__)

# Transient conditions worth a retry. 4xx other than 429 are caller errors.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Upper bound on how long a server-supplied Retry-After may stall a request.
_MAX_RETRY_AFTER_SECONDS = 30.0


class OpenAICompatibleProvider(LLMProvider):
    """Base provider. Subclasses set `name` and supply defaults."""

    name = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        transport: Optional[object] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._temperature = temperature
        self._max_tokens = max_tokens
        #: Injectable for tests. It reaches `SecureHttpClient` unchanged, so
        #: the policy runs against a stub exactly as it runs against the
        #: network -- which is what makes a provider SSRF test meaningful.
        self._transport = transport
        self._client: Optional[SecureHttpClient] = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAICompatibleProvider":
        return cls(
            api_key=settings.active_api_key,
            base_url=settings.active_base_url,
            model=settings.active_model,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

    @property
    def model(self) -> str:
        return self._model

    # --- HTTP plumbing ------------------------------------------------------

    def _get_client(self) -> SecureHttpClient:
        """Lazily build the policed client shared across requests.

        A `SecureHttpClient`, the same class web research uses, carrying a
        policy whose host allow-list is derived from the configured base URL.
        Stage 4F-C removed the direct `httpx.AsyncClient` that used to live
        here: it was the last outbound path in the application that did not
        consult `NetworkPolicy`.

        The API key is **not** baked into the client's headers as it once was.
        It is passed per request instead -- see `_post_with_retries` -- so it
        exists on no long-lived object that could be logged, serialised or
        inspected. Late insertion, at the transport boundary.
        """
        if self._client is None:
            # A malformed or forbidden base URL raises here, not at request
            # time. Converted rather than allowed to escape: the API layer
            # maps `LLMError` subclasses to HTTP responses, and an unmapped
            # `NetworkPolicyViolation` would surface as an unhandled 500 of a
            # different shape.
            self._client = build_provider_client(
                base_url=self._base_url,
                timeout_seconds=self._timeout,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    # --- Public API ---------------------------------------------------------

    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMNotConfiguredError(
                f"No API key configured for provider {self.name!r}. "
                "Set it in your .env file."
            )
        if not messages:
            raise LLMResponseError("Cannot call the model with no messages.")

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [message.to_dict() for message in messages],
            "temperature": (
                self._temperature if temperature is None else temperature
            ),
            "max_tokens": self._max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            # Supported by every OpenAI-compatible gateway used so far. The
            # caller still parses and validates, so a provider that silently
            # ignores this loses nothing.
            payload["response_format"] = {"type": "json_object"}

        logger.info(
            "LLM request started",
            extra={
                "provider": self.name,
                "model": self._model,
                "message_count": len(messages),
            },
        )

        data = await self._post_with_retries("/chat/completions", payload)
        response = self._parse_response(data)

        logger.info(
            "LLM request succeeded",
            extra={
                "provider": self.name,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "total_tokens": response.usage.get("total_tokens"),
            },
        )
        return response

    async def health_check(self) -> ProviderHealth:
        """Configuration + reachability probe using a one-token completion."""
        if not self._api_key:
            return ProviderHealth(
                healthy=False,
                provider=self.name,
                model=self._model,
                detail=f"No API key configured for the {self.name} provider.",
            )
        try:
            await self.generate_response(
                [LLMMessage(role="user", content="ping")], max_tokens=1
            )
            return ProviderHealth(healthy=True, provider=self.name, model=self._model)
        except LLMError as exc:
            return ProviderHealth(
                healthy=False,
                provider=self.name,
                model=self._model,
                detail=exc.message,
            )

    # --- Internals ----------------------------------------------------------

    async def _post_with_retries(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST with bounded exponential backoff on transient failures."""
        try:
            client = self._get_client()
        except IntegrationError as exc:
            # Building the client validates the configured endpoint. A
            # refusal here means the deployment is misconfigured, and the
            # message says so without naming the destination -- a refusal
            # that described it would be a way to probe the network through
            # a provider setting.
            logger.warning(
                "Provider endpoint refused by network policy",
                extra={"provider": self.name, "reason": exc.reason},
            )
            raise LLMError(
                f"The configured {self.name} endpoint is not permitted."
            ) from exc

        last_error: Optional[LLMError] = None
        # Honoured in place of exponential backoff when the server sends it.
        retry_after: Optional[float] = None

        for attempt in range(self._max_retries + 1):
            retry_after = None
            try:
                http_response = await client.post_json(
                    self._url_for(path),
                    json_body=payload,
                    # The key is supplied here, per request, rather than
                    # living on the client. It is applied by the transport
                    # and never returned on the response object.
                    auth_header=("Authorization", f"Bearer {self._api_key}"),
                )
            except ProviderTimeout as exc:
                last_error = LLMTimeoutError(
                    f"{self.name} request timed out after {self._timeout:.0f}s."
                )
                self._log_attempt_failure(attempt, "timeout", exc.reason)
            except NetworkPolicyViolation as exc:
                # The destination was refused. Not retryable, and not
                # described: a refusal that named the host would be a way to
                # probe the network through a misconfigured provider setting.
                self._log_attempt_failure(
                    attempt, "policy_refused", exc.reason, final=True
                )
                raise LLMError(
                    f"The configured {self.name} endpoint is not permitted."
                ) from exc
            except ResponseTooLarge as exc:
                self._log_attempt_failure(
                    attempt, "oversized_response", exc.reason, final=True
                )
                raise LLMError(
                    f"{self.name} returned an unexpectedly large response."
                ) from exc
            except IntegrationError as exc:
                # Every remaining transport failure. `exc.reason` is an
                # application constant; the underlying exception text is not
                # carried, because it can name an internal host.
                last_error = LLMError(f"Could not reach {self.name}.")
                self._log_attempt_failure(attempt, "transport_error", exc.reason)
            else:
                if http_response.status_code == 200:
                    return self._decode_json(http_response)

                error = self._error_for_status(http_response)
                retry_after = self._retry_after_seconds(http_response)
                if http_response.status_code not in _RETRYABLE_STATUS:
                    # Caller error (bad key, bad request) -- retrying cannot help.
                    self._log_attempt_failure(
                        attempt, "http_error", error.message, final=True
                    )
                    raise error
                last_error = error
                self._log_attempt_failure(
                    attempt, "http_error", error.message, retry_after=retry_after
                )

            if attempt < self._max_retries:
                # A server-supplied Retry-After is authoritative -- shared free
                # tiers routinely ask for longer than our own backoff.
                delay = (
                    retry_after
                    if retry_after is not None
                    else self._backoff_seconds(attempt)
                )
                await asyncio.sleep(delay)

        raise last_error or LLMError()

    def _url_for(self, path: str) -> str:
        """Absolute URL from the configured base and a code-chosen path.

        `path` is a literal in this module (`/chat/completions`), never a
        value from a request, a plan or a model. The base is operator
        configuration. Neither is user-influenced, and the policy checks the
        result regardless.
        """
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _retry_after_seconds(http_response) -> Optional[float]:
        """Read the Retry-After header, if the server sent a usable one.

        Only the delta-seconds form is handled; the HTTP-date form is rare here
        and falling back to normal backoff is safe.
        """
        raw = http_response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            seconds = float(raw.strip())
        except ValueError:
            return None
        if seconds < 0:
            return None
        return min(seconds, _MAX_RETRY_AFTER_SECONDS)

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Exponential backoff with jitter: ~0.5s, ~1s, ~2s."""
        return (0.5 * (2**attempt)) + random.uniform(0, 0.25)

    def _log_attempt_failure(
        self,
        attempt: int,
        reason: str,
        detail: str,
        final: bool = False,
        retry_after: Optional[float] = None,
    ) -> None:
        logger.warning(
            "LLM request failed",
            extra={
                "provider": self.name,
                "model": self._model,
                "attempt": attempt + 1,
                "max_attempts": self._max_retries + 1,
                "reason": reason,
                "retry_after": retry_after,
                # `detail` never contains the API key: it is either a transport
                # message or the provider's own error text.
                "detail": detail,
                "will_retry": not final and attempt < self._max_retries,
            },
        )

    @staticmethod
    def _decode_json(http_response) -> Dict[str, Any]:
        try:
            data = http_response.json()
        except ValueError as exc:
            raise LLMResponseError("The model API returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("The model API returned an unexpected response shape.")
        return data

    def _provider_message(self, http_response) -> str:
        """The provider's error text, bounded and stripped of the credential.

        A provider's error body is text Mai did not write, and it reaches the
        user through `LLMError.message`. Providers do sometimes echo the
        request back for debugging, and a compromised or merely careless one
        could include the `Authorization` header it received -- so the key is
        removed here rather than trusted not to appear.

        Cheap, and it closes the one path by which the credential could reach
        a user-visible string.
        """
        try:
            body = http_response.json()
        except ValueError:
            return self._scrub(http_response.text[:200])

        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return self._scrub(str(error["message"]))
            if body.get("message"):
                return self._scrub(str(body["message"]))
        return self._scrub(http_response.text[:200])

    def _scrub(self, text: str) -> str:
        """Remove the API key from provider-supplied text.

        Both bare and `Bearer`-prefixed. `redact` handles the general
        secret-shaped patterns Stage 3D defined; this handles the one secret
        this object actually holds, which a generic pattern could miss.
        """
        cleaned = redact(text or "")
        if self._api_key:
            cleaned = cleaned.replace(f"Bearer {self._api_key}", "[redacted]")
            cleaned = cleaned.replace(self._api_key, "[redacted]")
        return cleaned

    def _error_for_status(self, http_response) -> LLMError:
        status = http_response.status_code
        detail = self._provider_message(http_response)

        if status in (401, 403):
            return LLMAuthError(f"{self.name} rejected the API key (HTTP {status}).")
        if status == 429:
            return LLMRateLimitError(f"{self.name} is rate limiting requests.")
        if status >= 500:
            return LLMError(f"{self.name} is unavailable (HTTP {status}).")
        return LLMError(f"{self.name} rejected the request (HTTP {status}): {detail}")

    def _parse_response(self, data: Dict[str, Any]) -> LLMResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("The model response contained no choices.")

        first = choices[0] or {}
        message = first.get("message") or {}
        content = message.get("content")

        if not isinstance(content, str) or not content.strip():
            finish_reason = first.get("finish_reason")
            if finish_reason == "sensitive":
                raise LLMResponseError(
                    "The model declined to answer this message (content filter)."
                )
            raise LLMResponseError("The model returned an empty message.")

        usage = data.get("usage")
        return LLMResponse(
            content=content,
            model=str(data.get("model") or self._model),
            finish_reason=first.get("finish_reason"),
            usage=usage if isinstance(usage, dict) else {},
            raw=data,
        )
