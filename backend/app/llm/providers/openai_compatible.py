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

import httpx

from app.core.config import Settings
from app.core.errors import (
    LLMAuthError,
    LLMError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.core.logging import get_logger
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
        extra_headers: Optional[Dict[str, str]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._extra_headers = extra_headers or {}
        self._client = client
        self._owns_client = client is None

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

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily build a keep-alive client shared across requests."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    **self._extra_headers,
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    # --- Public API ---------------------------------------------------------

    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
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
        client = self._get_client()
        last_error: Optional[LLMError] = None
        # Honoured in place of exponential backoff when the server sends it.
        retry_after: Optional[float] = None

        for attempt in range(self._max_retries + 1):
            retry_after = None
            try:
                http_response = await client.post(path, json=payload)
            except httpx.TimeoutException as exc:
                last_error = LLMTimeoutError(
                    f"{self.name} request timed out after {self._timeout:.0f}s."
                )
                self._log_attempt_failure(attempt, "timeout", str(exc))
            except httpx.HTTPError as exc:
                last_error = LLMError(f"Could not reach {self.name}: {exc}")
                self._log_attempt_failure(attempt, "transport_error", str(exc))
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

    @staticmethod
    def _retry_after_seconds(http_response: httpx.Response) -> Optional[float]:
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
                # `detail` never contains the API key: it is either an httpx
                # message or the provider's own error text.
                "detail": detail,
                "will_retry": not final and attempt < self._max_retries,
            },
        )

    @staticmethod
    def _decode_json(http_response: httpx.Response) -> Dict[str, Any]:
        try:
            data = http_response.json()
        except ValueError as exc:
            raise LLMResponseError("The model API returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("The model API returned an unexpected response shape.")
        return data

    @staticmethod
    def _provider_message(http_response: httpx.Response) -> str:
        """Best-effort extraction of the provider's error text."""
        try:
            body = http_response.json()
        except ValueError:
            return http_response.text[:200]
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if body.get("message"):
                return str(body["message"])
        return http_response.text[:200]

    def _error_for_status(self, http_response: httpx.Response) -> LLMError:
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
