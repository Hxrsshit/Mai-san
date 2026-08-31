"""Provider registry and the extensibility the abstraction promises.

Stage 1 ships one provider (Groq). The requirement is not that several exist,
but that adding Claude/OpenAI/Gemini/a local model later needs no change to
ChatService. These tests verify that directly, by registering a brand-new
provider at runtime and driving a full chat turn through it.
"""

from typing import List, Optional

import httpx
import pytest

from app.core.config import Settings
from app.llm import factory
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, ProviderHealth
from app.llm.factory import UnknownProviderError, build_provider
from app.llm.providers import GroqProvider, OpenAICompatibleProvider
from app.services.chat_service import ChatService


# --- Registry ---------------------------------------------------------------


def test_factory_builds_groq_by_default() -> None:
    provider = build_provider(Settings(_env_file=None, GROQ_API_KEY="k"))
    assert isinstance(provider, GroqProvider)
    assert provider.name == "groq"
    assert provider.model == "openai/gpt-oss-120b"


def test_provider_name_is_case_and_space_insensitive() -> None:
    provider = build_provider(
        Settings(_env_file=None, LLM_PROVIDER="  GROQ ", GROQ_API_KEY="k")
    )
    assert isinstance(provider, GroqProvider)


def test_unknown_provider_names_the_valid_options() -> None:
    with pytest.raises(UnknownProviderError) as caught:
        build_provider(Settings(_env_file=None, LLM_PROVIDER="nope"))
    assert "groq" in str(caught.value)


def test_groq_specialises_the_shared_transport() -> None:
    """Provider-specific code must stay defaults-only, not a forked copy."""
    assert issubclass(GroqProvider, OpenAICompatibleProvider)
    assert issubclass(OpenAICompatibleProvider, LLMProvider)


# --- Extensibility: the actual Stage 1 requirement --------------------------


class ClaudeShapedProvider(LLMProvider):
    """A provider with a completely different wire format.

    Stands in for Claude/Gemini/a local model: it does not inherit the
    OpenAI-compatible transport at all.
    """

    name = "claude-shaped"

    def __init__(self, api_key: str = "", model: str = "fake-claude-1") -> None:
        self._model = model
        self.received: List[LLMMessage] = []

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClaudeShapedProvider":
        # A real provider would read CLAUDE_SHAPED_API_KEY from Settings; this
        # stand-in keeps the fixture free of a Settings change.
        return cls()

    @property
    def model(self) -> str:
        return self._model

    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Accepts `json_mode` and ignores it, which the contract permits.

        A provider may have no native JSON mode; it may not refuse the
        parameter. Stage 4A's classifier is the first request-path caller to
        pass it, so a provider that omitted it used to work by accident.
        """
        if json_mode:
            # No structured mode of its own: answer the shape the caller needs
            # from the prompt alone, exactly as the contract describes.
            self.received = list(messages)
            return LLMResponse(
                content=(
                    '{"intent_type": "conversation", "confidence": 0.5,'
                    ' "ambiguity": "none", "secondary_intents": []}'
                ),
                model=self._model,
            )
        self.received = list(messages)
        return LLMResponse(content="reply from a different vendor", model=self._model)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, provider=self.name, model=self._model)


@pytest.fixture
def registered_custom_provider():
    """Register a new provider, then restore the registry."""
    original = dict(factory._REGISTRY)
    factory._REGISTRY["claude-shaped"] = ClaudeShapedProvider.from_settings
    yield
    factory._REGISTRY.clear()
    factory._REGISTRY.update(original)


def test_a_new_provider_can_be_registered_at_runtime(
    registered_custom_provider,
) -> None:
    """One registry entry is enough for the factory to build it."""
    settings = Settings(_env_file=None, LLM_PROVIDER="claude-shaped")
    provider = build_provider(settings)
    assert isinstance(provider, ClaudeShapedProvider)
    assert provider.name == "claude-shaped"


def test_provider_setting_lookup_normalises_hyphens() -> None:
    """A hyphenated provider name maps to an underscored env var."""
    settings = Settings(_env_file=None, LLM_PROVIDER="claude-shaped")
    with pytest.raises(ValueError, match="CLAUDE_SHAPED_API_KEY"):
        _ = settings.active_api_key


async def test_chat_service_works_unchanged_with_a_foreign_provider(
    db_session, settings
) -> None:
    """The core guarantee: ChatService has no provider-specific code."""
    from app.services.conversation_service import ConversationService

    provider = ClaudeShapedProvider()
    conversations = ConversationService(db_session)
    conversation = await conversations.create_conversation()

    chat = ChatService(session=db_session, provider=provider, settings=settings)
    (
        user_message,
        assistant_message,
        intent,
        planning,
        orchestration,
    ) = await chat.send_message(
        conversation.id, "hello from the test"
    )

    assert assistant_message.content == "reply from a different vendor"
    assert user_message.content == "hello from the test"
    # The provider received the system prompt plus the user turn.
    assert [m.role for m in provider.received] == ["system", "user"]

    # Stage 4A rides the same abstraction. A provider with a different wire
    # format, no native JSON mode and no OpenAI-compatible transport still
    # produces a usable classification, because the classifier only ever
    # speaks `LLMMessage` and validates whatever comes back.
    assert intent.intent_type.value == "conversation"
    assert intent.classified is True
    assert intent.requires_execution is False
    # Conversation warrants no plan, so Stage 4B made no call at all.
    assert planning.status.value == "not_eligible"
    assert planning.model_calls == 0
    # Stage 4D: a conversational turn is not action-capable, so nothing
    # was examined and nothing could have run.
    assert orchestration.outcome.value == "not_eligible"
    assert orchestration.model_calls == 0
    assert orchestration.acted is False


async def test_switching_providers_changes_the_endpoint_called() -> None:
    """Provider selection must change real behaviour, not just a label."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}
                ],
            },
        )

    settings = Settings(_env_file=None, LLM_PROVIDER="groq", GROQ_API_KEY="groq-key")
    provider = build_provider(settings)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=settings.active_base_url,
        headers={"Authorization": f"Bearer {settings.active_api_key}"},
    )

    await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["auth"] == "Bearer groq-key"


def test_unknown_provider_setting_gives_an_actionable_error() -> None:
    """The message must say exactly which setting is missing."""
    settings = Settings(_env_file=None, LLM_PROVIDER="mystery")
    with pytest.raises(ValueError, match="MYSTERY_API_KEY"):
        _ = settings.active_api_key
