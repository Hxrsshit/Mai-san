"""Provider-agnostic LLM interface.

Everything above this module (services, routes) talks to `LLMProvider` and the
plain dataclasses below. No provider SDK type is allowed to leak upward, which
is what makes the active provider swappable for Claude/GPT/Gemini/a
local model later.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class LLMMessage:
    """One turn of conversation, as sent to a model."""

    role: str  # "user" | "assistant" | "system"
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class LLMResponse:
    """A model's reply, normalised across providers."""

    content: str
    model: str
    finish_reason: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)

    # No `raw` field. It used to carry the provider's entire response into
    # application state, and nothing ever read it -- so it was pure exposure:
    # whatever a provider chose to return, held in memory and reachable by any
    # future logger or serialiser. Everything Mai needs is named above.


@dataclass(frozen=True)
class ProviderOutcome:
    """How the provider's most recent real request ended.

    Observed from requests the application made anyway, so reporting it costs
    no model call. Holds an error *code* (`app.core.errors`), never a message,
    a prompt, a response or a credential: it is safe to show on `/health`.
    """

    ok: bool
    #: None on success; otherwise the `LLMError.code`, e.g. "llm_timeout".
    error_code: Optional[str]
    at: datetime


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a provider health probe."""

    healthy: bool
    provider: str
    model: str
    detail: Optional[str] = None


class LLMProvider(ABC):
    """The contract every model backend must satisfy."""

    #: Short identifier used in logs and the health endpoint, e.g. "groq".
    name: str = "base"

    #: The most recent real request's outcome, or None before the first.
    #: Written by `_record_success` / `_record_failure`, which every
    #: implementation calls around its request; read by `/health`.
    last_outcome: Optional[ProviderOutcome] = None

    @property
    @abstractmethod
    def model(self) -> str:
        """The model identifier this provider is configured to call."""

    @abstractmethod
    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Generate a single, non-streaming completion.

        `json_mode` asks the provider to emit a JSON object. It is a
        capability request, not a vendor format: providers that support a
        native JSON mode should use it, and providers that do not may ignore
        it and rely on the prompt. Callers must parse and validate the result
        either way, so ignoring it is always safe.

        Implementations must raise the errors in `app.core.errors`
        (LLMTimeoutError, LLMAuthError, LLMRateLimitError, LLMResponseError,
        LLMError) rather than provider-specific exceptions.
        """

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Report whether the provider is reachable and configured."""

    async def close(self) -> None:
        """Release any held resources. Default is a no-op."""
        return None

    # --- Observed reachability ---------------------------------------------
    # One assignment each, so concurrent requests cannot interleave a partial
    # record: the last request to finish is what `/health` reports.

    def _record_success(self) -> None:
        self.last_outcome = ProviderOutcome(
            ok=True, error_code=None, at=datetime.now(timezone.utc)
        )

    def _record_failure(self, error: Exception) -> None:
        self.last_outcome = ProviderOutcome(
            ok=False,
            error_code=str(getattr(error, "code", "llm_error")),
            at=datetime.now(timezone.utc),
        )
