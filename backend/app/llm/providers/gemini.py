"""Gemini provider.

Google exposes Gemini through an OpenAI-compatible endpoint, so this is a
subclass with different defaults rather than a second implementation -- the
same shape as `groq.py`.

    POST https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
    Authorization: Bearer <GEMINI_API_KEY>

Deliberately **not** the Google SDK. The SDK opens its own connections, which
would bypass `SecureHttpClient` and the single-host `NetworkPolicy` every
provider request goes through. Inheriting `OpenAICompatibleProvider` keeps
all of it: the host allow-list derived from the base URL, refused redirects,
the response-size cap, the timeouts, the key inserted per request rather
than stored on the client, and the redaction that scrubs the key from any
error text a provider echoes back.

Selected only by `LLM_PROVIDER=gemini`. Nothing routes to it automatically.
"""

from app.llm.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-3.8-flash"


class GeminiProvider(OpenAICompatibleProvider):
    name = "gemini"
