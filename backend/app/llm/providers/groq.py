"""Groq provider.

Groq exposes an OpenAI-compatible endpoint, so this is a subclass with
different defaults rather than a second implementation.

    POST https://api.groq.com/openai/v1/chat/completions
    Authorization: Bearer <GROQ_API_KEY>

Free tier: 30 requests/minute, 14,400/day, no card required. Available chat
models include openai/gpt-oss-120b (default), openai/gpt-oss-20b and
qwen/qwen3.8-27b; set GROQ_MODEL to change it.
"""

from app.llm.providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
