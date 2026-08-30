"""Provider-agnostic LLM interface.

Everything above this module (services, routes) talks to `LLMProvider` and the
plain dataclasses below. No provider SDK type is allowed to leak upward, which
is what makes the active provider swappable for Claude/GPT/Gemini/a
local model later.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
    raw: Dict[str, Any] = field(default_factory=dict)


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
