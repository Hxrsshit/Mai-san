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


def test_a_provider_cannot_be_added_at_runtime(
    registered_custom_provider,
) -> None:
    """Stage 4F-F closed the provider set, and that is a tightening.

    Registering a builder used to be enough for the factory to construct a
    provider under any name. Now the set is an enum, and a name outside it is
    refused however many registry entries exist -- so a provider cannot appear
    from configuration, from a fixture, or from anything a request could
    reach.

    Adding a real provider is now a deliberate three-place change: an enum
    member, a table entry with its host and auth mode, and a builder.
    """
    settings = Settings(_env_file=None, LLM_PROVIDER="claude-shaped")

    with pytest.raises(UnknownProviderError):
        build_provider(settings)


def test_the_provider_set_is_exactly_the_declared_modes() -> None:
    """Widened to four when Gemini was added. Literal, so a fifth has to be
    argued for too."""
    from app.llm.gateway import PROVIDERS, ProviderMode

    assert {mode.value for mode in ProviderMode} == {
        "groq", "gemini", "anthropic_api", "claude_subscription",
    }
    assert set(PROVIDERS) == set(ProviderMode)


def test_an_unlisted_provider_name_has_no_settings_prefix() -> None:
    """Settings resolution goes through the same closed table.

    Previously a hyphenated name was mapped to an env var by convention, so
    any name resolved to *some* setting. Now an unlisted name cannot resolve
    at all.
    """
    settings = Settings(_env_file=None, LLM_PROVIDER="claude-shaped")

    with pytest.raises(UnknownProviderError):
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
        research,
        workflow,
        calendar,
        mail,
    ) = await chat.send_message(
        conversation.id, "hello from the test"
    )

    assert assistant_message.content == "reply from a different vendor"
    assert user_message.content == "hello from the test"
    # The provider received the system prompt, the Stage 5D.1 execution-state
    # block, and the user turn. The point of the test is unchanged: a foreign
    # provider sees exactly the same normalised messages as any other, and the
    # execution block is application text like the rest of the instructions.
    assert [m.role for m in provider.received] == ["system", "system", "user"]

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
    # The transport is injected, not a finished client. Stage 4F-C made the
    # provider build a `SecureHttpClient` around whatever transport it is
    # given, so the network policy runs here exactly as it does in
    # production -- including the destination check on the URL asserted below.
    provider._transport = httpx.MockTransport(handler)

    await provider.generate_response([LLMMessage(role="user", content="Hi")])

    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["auth"] == "Bearer groq-key"


def test_unknown_provider_setting_gives_an_actionable_error() -> None:
    """The message must name the valid options.

    Stage 4F-F changed what "actionable" means here. Previously an unknown
    name produced `MYSTERY_API_KEY is missing`, which sent the reader off to
    add a setting for a provider that does not exist. Now the provider set is
    closed, so the useful message is the list of names that work.
    """
    settings = Settings(_env_file=None, LLM_PROVIDER="mystery")

    with pytest.raises(UnknownProviderError) as caught:
        _ = settings.active_api_key

    message = str(caught.value)
    assert "mystery" in message
    for valid in ("groq", "anthropic_api", "claude_subscription"):
        assert valid in message
