"""The Anthropic Messages API, behind Mai's own network boundary.

A first-class provider, not a special case. It uses the same
`SecureHttpClient` the Groq provider uses, with the same single-host
allow-list, the same POST-only policy, the same bounded body and timeouts,
and the same refusal to follow redirects -- the only differences are the host,
the header the credential travels in, and the shape of the request and reply.

What this is not
----------------

Not the Agent SDK. This talks to `POST /v1/messages` over HTTPS and nothing
else: no subprocess, no tool runner, no filesystem access, no server-side
tools. Anthropic's own tool-use and web-search features are deliberately not
requested -- Mai's tool registry and its Tavily research path stay
authoritative, and a provider-native tool would be a second execution route
around Stage 4C and 4E.
"""

from typing import Any, Dict, List, Optional

from app.core.config import Settings
from app.core.errors import (
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.core.logging import get_logger, redact
from app.integrations.errors import (
    IntegrationError,
    NetworkPolicyViolation,
    ProviderTimeout,
    ResponseTooLarge,
)
from app.integrations.http_client import SecureHttpClient
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, ProviderHealth
from app.llm.transport import build_provider_client

logger = get_logger(__name__)

#: The API version header Anthropic requires. A pinned constant: the wire
#: format is versioned, and letting it float would mean the parser below and
#: the response could disagree after a change nobody made.
ANTHROPIC_VERSION = "2023-06-01"

#: The header the credential travels in. Anthropic uses `x-api-key` rather
#: than `Authorization: Bearer`.
AUTH_HEADER = "x-api-key"

#: Statuses worth trying again. Identical reasoning to the Groq provider:
#: a caller error cannot be fixed by repeating it.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class AnthropicProvider(LLMProvider):
    """Anthropic's Messages API, normalised into Mai's own response type."""

    name = "anthropic_api"

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
        self._transport = transport
        self._client: Optional[SecureHttpClient] = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "AnthropicProvider":
        return cls(
            api_key=settings.ANTHROPIC_API_KEY,
            base_url=settings.ANTHROPIC_BASE_URL,
            model=settings.ANTHROPIC_MODEL,
            timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

    @property
    def model(self) -> str:
        return self._model

    # --- The request --------------------------------------------------------

    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMAuthError("No API key configured for the anthropic_api provider.")

        system, turns = self._split_system(messages)

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": turns,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": (
                self._temperature if temperature is None else temperature
            ),
        }
        if system:
            # Anthropic carries system instructions in their own top-level
            # field rather than as a message. Mai's formatter emits them as
            # system-role messages, so they are lifted here -- one place,
            # rather than a second prompt shape for one provider.
            payload["system"] = system

        if json_mode:
            # No native JSON mode on this API. `json_mode` is a capability
            # *request*: a provider that cannot honour it may ignore it, and
            # callers parse and validate regardless. Nothing is faked here.
            logger.debug("json_mode requested; anthropic_api has no native mode")

        data = await self._post_with_retries("/v1/messages", payload)
        return self._parse_response(data)

    async def health_check(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                healthy=False,
                provider=self.name,
                model=self._model,
                detail="No API key configured for the anthropic_api provider.",
            )
        try:
            await self.generate_response(
                [LLMMessage(role="user", content="ping")], max_tokens=1
            )
            return ProviderHealth(healthy=True, provider=self.name, model=self._model)
        except LLMError as exc:
            return ProviderHealth(
                healthy=False, provider=self.name, model=self._model,
                detail=exc.message,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    # --- Internals ----------------------------------------------------------

    def _get_client(self) -> SecureHttpClient:
        """The policed client. Same class, same policy shape, as Groq's.

        The API key is not stored on it -- it is passed per request, so it
        exists on no long-lived object an error handler or debug dump could
        reach.
        """
        if self._client is None:
            self._client = build_provider_client(
                base_url=self._base_url,
                timeout_seconds=self._timeout,
                transport=self._transport,
                # The one protocol header this API requires. Named here, so
                # it is permitted for this provider's client and for nothing
                # else -- the research client cannot set it.
                extra_request_headers=frozenset({"anthropic-version"}),
            )
        return self._client

    async def _post_with_retries(
        self, path: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST with bounded backoff. The provider's own loop, not the transport's."""
        import asyncio

        try:
            client = self._get_client()
        except IntegrationError as exc:
            logger.warning(
                "Provider endpoint refused by network policy",
                extra={"provider": self.name, "reason": exc.reason},
            )
            raise LLMError(
                f"The configured {self.name} endpoint is not permitted."
            ) from exc

        last_error: Optional[LLMError] = None

        for attempt in range(self._max_retries + 1):
            retry_after: Optional[float] = None
            try:
                response = await client.post_json(
                    f"{self._base_url}/{path.lstrip('/')}",
                    json_body=payload,
                    # Supplied per request and applied by the transport. Never
                    # placed on the client, never in the URL, never in the body.
                    auth_header=(AUTH_HEADER, self._api_key),
                    headers={"anthropic-version": ANTHROPIC_VERSION},
                )
            except ProviderTimeout:
                last_error = LLMTimeoutError(
                    f"{self.name} request timed out after {self._timeout:.0f}s."
                )
            except NetworkPolicyViolation as exc:
                raise LLMError(
                    f"The configured {self.name} endpoint is not permitted."
                ) from exc
            except ResponseTooLarge as exc:
                raise LLMError(
                    f"{self.name} returned an unexpectedly large response."
                ) from exc
            except IntegrationError:
                last_error = LLMError(f"Could not reach {self.name}.")
            else:
                if response.status_code == 200:
                    return self._decode(response)

                error = self._error_for_status(response)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise error
                last_error = error
                retry_after = self._retry_after_seconds(response)

            if attempt < self._max_retries:
                await asyncio.sleep(
                    retry_after if retry_after is not None
                    else min(2.0 ** attempt, 8.0)
                )

        raise last_error or LLMError(f"Could not reach {self.name}.")

    @staticmethod
    def _retry_after_seconds(response) -> Optional[float]:
        raw = response.headers.get("Retry-After")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if 0 < value <= 60 else None

    def _decode(self, response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(
                f"{self.name} returned a response that was not JSON."
            ) from exc
        if not isinstance(data, dict):
            raise LLMResponseError(f"{self.name} returned an unexpected response.")
        return data

    def _error_for_status(self, response) -> LLMError:
        status = response.status_code
        detail = self._scrub(self._provider_message(response))

        if status in (401, 403):
            return LLMAuthError(f"{self.name} rejected the API key (HTTP {status}).")
        if status == 429:
            return LLMRateLimitError(f"{self.name} is rate limiting requests.")
        if status >= 500:
            return LLMError(f"{self.name} is unavailable (HTTP {status}).")
        return LLMError(f"{self.name} rejected the request (HTTP {status}): {detail}")

    def _provider_message(self, response) -> str:
        """The provider's own error text, bounded.

        Scrubbed by the caller. A provider's error body is text Mai did not
        write and it reaches the user; providers do echo requests back for
        debugging, and a careless one could include the header it received.
        """
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:200]
            if body.get("message"):
                return str(body["message"])[:200]
        return response.text[:200]

    def _scrub(self, text: str) -> str:
        """Remove the API key from provider-supplied text.

        `redact` handles the general secret-shaped patterns Stage 3D defined;
        this handles the one secret this object holds, which a generic pattern
        could miss.
        """
        cleaned = redact(text or "")
        if self._api_key:
            cleaned = cleaned.replace(self._api_key, "[redacted]")
        return cleaned

    @staticmethod
    def _split_system(messages: List[LLMMessage]):
        """Separate system instructions from the conversation.

        Anthropic takes system text in its own field and rejects a `system`
        role inside `messages`. Consecutive system messages are joined in
        order, which is exactly how the formatter emits them.
        """
        system_parts: List[str] = []
        turns: List[Dict[str, str]] = []

        for message in messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
            else:
                turns.append({"role": message.role, "content": message.content})

        return "\n\n".join(system_parts), turns

    def _parse_response(self, data: Dict[str, Any]) -> LLMResponse:
        """Normalise into Mai's own response type, field by field.

        Never `LLMResponse(**data)`. Each field is read by name so a provider
        that adds one, renames one, or returns the wrong type produces a
        poorer answer or a clear error rather than an injection.
        """
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise LLMResponseError("The model response contained no content.")

        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

        if not text.strip():
            if data.get("stop_reason") == "refusal":
                raise LLMResponseError(
                    "The model declined to answer this message."
                )
            raise LLMResponseError("The model returned an empty message.")

        usage = data.get("usage")
        normalised: Dict[str, Any] = {}
        if isinstance(usage, dict):
            # Renamed into the shape the rest of Mai already logs, so a
            # provider swap does not change what a log line means.
            prompt_tokens = usage.get("input_tokens")
            completion_tokens = usage.get("output_tokens")
            normalised = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": (
                    (prompt_tokens or 0) + (completion_tokens or 0)
                    if prompt_tokens is not None or completion_tokens is not None
                    else None
                ),
            }

        return LLMResponse(
            content=text,
            model=str(data.get("model") or self._model),
            finish_reason=data.get("stop_reason"),
            usage=normalised,
        )


__all__ = ["ANTHROPIC_VERSION", "AUTH_HEADER", "AnthropicProvider"]
