"""Test fixtures.

Tests run against an in-process SQLite database and a fake LLM provider, so
the suite needs neither PostgreSQL nor network access or an API key.
"""

import uuid
from typing import AsyncIterator, List, Optional

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.database.metadata import Base
from app.database.session import get_db_session, get_session_factory
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, ProviderHealth
from app.llm.factory import get_llm_provider
from app.main import create_app


# --- Fake provider ----------------------------------------------------------


class FakeLLMProvider(LLMProvider):
    """Records what it was asked and returns a canned reply.

    Set `raise_error` to make the next call fail, which is how the error-path
    tests exercise the API without touching the network.

    Calls made with `json_mode=True` are structured calls. They are recorded
    separately and answered with a canned payload, so one fake serves the chat
    turn, the Stage 4A intent classification and Stage 4B planning that precede
    it, and the memory, entity and relationship extraction that follow it. The
    kinds are told apart by their system prompt.

    `calls` therefore holds *generation* calls only -- the single synchronous
    response call per turn. Classification and extraction never appear there,
    which is what keeps the Stage 3B one-generation-call assertion meaningful
    after Stage 4A added a second request-path call of a different kind.
    """

    name = "fake"

    #: Returned for json_mode calls when nothing else is configured.
    NO_MEMORIES = '{"should_store_memory": false, "memories": []}'
    NO_ENTITIES = '{"entities": []}'
    NO_RELATIONSHIPS = '{"relationships": []}'
    #: A minimal valid Stage 4B plan, for turns that reach the planner.
    SIMPLE_PLAN = (
        '{"goal_summary": "A goal", "desired_outcome": null, "scope": null,'
        ' "tasks": [{"id": "step-one", "title": "First step",'
        ' "description": null, "priority": "medium", "dependencies": [],'
        ' "expected_outcome": null, "completion_criteria": []}],'
        ' "assumptions": [], "risks": [], "success_criteria": []}'
    )
    #: A neutral Stage 4A answer: ordinary conversation, nothing required.
    CONVERSATION_INTENT = (
        '{"intent_type": "conversation", "confidence": 0.9, "goal": null,'
        ' "requested_outcome": null, "ambiguity": "none",'
        ' "ambiguity_reason": null, "suggests_planning": false,'
        ' "suggests_research": false, "secondary_intents": []}'
    )

    def __init__(self, reply: str = "Hello from Mai.") -> None:
        self.reply = reply
        self.calls: List[List[LLMMessage]] = []
        self.extraction_calls: List[List[LLMMessage]] = []
        self.entity_calls: List[List[LLMMessage]] = []
        self.relationship_calls: List[List[LLMMessage]] = []
        self.intent_calls: List[List[LLMMessage]] = []
        self.planning_calls: List[List[LLMMessage]] = []
        self.extraction_reply: str = self.NO_MEMORIES
        self.entity_reply: str = self.NO_ENTITIES
        self.relationship_reply: str = self.NO_RELATIONSHIPS
        self.intent_reply: str = self.CONVERSATION_INTENT
        self.planning_reply: str = self.SIMPLE_PLAN
        self.raise_error: Optional[Exception] = None
        self.extraction_error: Optional[Exception] = None
        self.entity_error: Optional[Exception] = None
        self.relationship_error: Optional[Exception] = None
        self.intent_error: Optional[Exception] = None
        self.planning_error: Optional[Exception] = None

    @staticmethod
    def _is_planning_call(messages: List[LLMMessage]) -> bool:
        system = messages[0].content if messages else ""
        return "You turn a user's goal into a structured plan" in system

    @staticmethod
    def _is_intent_call(messages: List[LLMMessage]) -> bool:
        system = messages[0].content if messages else ""
        return "You classify what a user is asking for" in system

    @staticmethod
    def _is_entity_call(messages: List[LLMMessage]) -> bool:
        system = messages[0].content if messages else ""
        return "identifiable entities" in system

    @staticmethod
    def _is_relationship_call(messages: List[LLMMessage]) -> bool:
        system = messages[0].content if messages else ""
        return "directional relationships" in system

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        if json_mode and self._is_planning_call(messages):
            self.planning_calls.append(list(messages))
            if self.planning_error is not None:
                raise self.planning_error
            content = self.planning_reply
        elif json_mode and self._is_intent_call(messages):
            self.intent_calls.append(list(messages))
            if self.intent_error is not None:
                raise self.intent_error
            content = self.intent_reply
        elif json_mode and self._is_relationship_call(messages):
            self.relationship_calls.append(list(messages))
            if self.relationship_error is not None:
                raise self.relationship_error
            content = self.relationship_reply
        elif json_mode and self._is_entity_call(messages):
            self.entity_calls.append(list(messages))
            if self.entity_error is not None:
                raise self.entity_error
            content = self.entity_reply
        elif json_mode:
            self.extraction_calls.append(list(messages))
            if self.extraction_error is not None:
                raise self.extraction_error
            content = self.extraction_reply
        else:
            self.calls.append(list(messages))
            if self.raise_error is not None:
                raise self.raise_error
            content = self.reply

        return LLMResponse(
            content=content,
            model=self.model,
            finish_reason="stop",
            usage={"total_tokens": 42},
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, provider=self.name, model=self.model)

    @property
    def last_call(self) -> List[LLMMessage]:
        assert self.calls, "provider was never called"
        return self.calls[-1]

    @property
    def last_extraction_call(self) -> List[LLMMessage]:
        assert self.extraction_calls, "extraction was never called"
        return self.extraction_calls[-1]

    @property
    def last_entity_call(self) -> List[LLMMessage]:
        assert self.entity_calls, "entity extraction was never called"
        return self.entity_calls[-1]

    @property
    def last_relationship_call(self) -> List[LLMMessage]:
        assert self.relationship_calls, "relationship extraction was never called"
        return self.relationship_calls[-1]

    @property
    def last_intent_call(self) -> List[LLMMessage]:
        assert self.intent_calls, "intent classification was never called"
        return self.intent_calls[-1]

    @property
    def last_planning_call(self) -> List[LLMMessage]:
        assert self.planning_calls, "planning was never called"
        return self.planning_calls[-1]


# --- Settings ---------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LLM_PROVIDER="groq",
        GROQ_API_KEY="test-key",
        GLM_API_KEY="test-key",
        # INFO, not WARNING: logging paths must actually execute in tests.
        # A reserved-attribute collision in `extra` only raises when the
        # level is enabled, so WARNING hid a real bug.
        LOG_LEVEL="INFO",
        MAI_SYSTEM_PROMPT="You are Mai.",
        MAX_CONTEXT_MESSAGES=40,
    )


# --- Database ---------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator["async_sessionmaker[AsyncSession]"]:
    """A fresh in-memory schema per test.

    StaticPool keeps every connection pointed at the same in-memory database.
    """
    from sqlalchemy.pool import StaticPool

    from app.database.session import configure_sqlite

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Apply the *same* configuration production uses -- foreign key
    # enforcement, busy timeout, and the driver settings SAVEPOINT depends on.
    # A fixture that configures its connection differently from the real engine
    # hides exactly the bugs these tests exist to catch.
    configure_sqlite(engine)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
        await session.commit()


# --- Application ------------------------------------------------------------


@pytest.fixture
def fake_provider() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest_asyncio.fixture
async def client(
    session_factory, fake_provider: FakeLLMProvider, settings: Settings
) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app, with DB and LLM dependencies overridden.

    `lifespan` is not run, so no real engine or provider is ever constructed.
    """
    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        # Mirrors get_db_session, including its "already committed" guard.
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: settings
    # Post-turn memory extraction opens its own session; point it at the
    # in-memory test database rather than the process-wide engine.
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def conversation_id(client: AsyncClient) -> uuid.UUID:
    response = await client.post("/api/conversations", json={})
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])

# --- Stage 4E: controlled execution -----------------------------------------


@pytest.fixture
def workspace(tmp_path):
    """A real, empty workspace directory, per test.

    A real one rather than a mock. Every guarantee in Stage 4E is about the
    filesystem -- symlinks, traversal, exclusive creation -- and a fake
    filesystem would test the fake.
    """
    root = tmp_path / "mai_workspace"
    root.mkdir()
    return root


@pytest.fixture
def execution_settings(settings: Settings, workspace) -> Settings:
    """Settings with execution switched on and confined to `workspace`.

    Switched on *only here*. The default fixture leaves it off, so every other
    test in the suite runs against a deployment that cannot execute -- which is
    the configuration Mai ships in, and the one most tests should exercise.
    """
    settings.EXECUTION_ENABLED = True
    settings.MAI_WORKSPACE_ROOT = str(workspace)
    return settings


@pytest_asyncio.fixture
async def execution_client(
    session_factory, fake_provider, execution_settings: Settings, monkeypatch
) -> AsyncIterator[AsyncClient]:
    """A client whose app was *built* with execution enabled.

    `create_app` reads settings while registering routers, and a dependency
    override cannot reach back in time to change that -- so the switch is
    patched before the app is constructed. Overriding `get_settings` as well
    keeps request-time reads consistent with build-time ones.
    """
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: execution_settings)
    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: execution_settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def executions(db_session, execution_settings: Settings):
    """An `ExecutionService` on the test session, for tests below the API."""
    from app.execution.service import ExecutionService

    return ExecutionService(db_session, settings=execution_settings)


@pytest_asyncio.fixture
async def concurrent_session_factory(tmp_path) -> AsyncIterator[
    "async_sessionmaker[AsyncSession]"
]:
    """A file-backed database where sessions really are independent.

    The main `session_factory` uses StaticPool over `:memory:`, which points
    every session at one connection -- so two "concurrent" transactions are
    actually the same transaction, and a race test against it would prove
    nothing (it raises "cannot start a transaction within a transaction"
    instead).

    A file gives each session its own connection and its own transaction,
    which is what a race needs. SQLite still serialises writers, so this
    demonstrates the *logic* -- the loser sees no matching row and refuses --
    rather than true parallelism. PostgreSQL covers the rest.
    """
    from app.database.session import configure_sqlite

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    configure_sqlite(engine)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    await engine.dispose()


@pytest_asyncio.fixture
async def research_client(
    session_factory, fake_provider, execution_settings, workspace, monkeypatch
) -> AsyncIterator[AsyncClient]:
    """A chat client with research enabled and a stubbed search provider.

    The search integration is real -- real `SecureHttpClient`, real
    `NetworkPolicy` -- with only the socket replaced by a stub transport. So
    what these tests exercise is the code that would run against a live
    provider, which is what makes them worth more than mocking the service.
    """
    import app.main as main_module
    from app.execution.dispatcher import Dispatcher
    from app.execution.service import ExecutionService
    from app.execution.tools import ExecutableRegistry
    from app.execution.web_search_tool import WebSearchTool
    from app.integrations.credentials import EnvironmentCredentialResolver
    from app.integrations.registry import IntegrationRegistry
    from app.integrations.web_search import WebSearchIntegration
    from app.prompt.formatter import PromptFormatter
    from app.research.service import ResearchService
    from app.runtime.facts import build as build_runtime_facts
    from app.services.chat_service import ChatService
    from app.tools.authorization import AuthorizationService
    from app.tools.catalog import build_catalog
    from app.tools.registry import ToolRegistry
    from tests.support.stub_transport import StubTransport, brave_payload

    execution_settings.SEARCH_API_KEY = "SEARCH_SECRET_123"
    monkeypatch.setattr(main_module, "get_settings", lambda: execution_settings)

    transport = StubTransport(payload=brave_payload(count=2))

    def resolve(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    integration = WebSearchIntegration(
        credentials=EnvironmentCredentialResolver(
            environ={"SEARCH_API_KEY": "SEARCH_SECRET_123"}
        ),
        transport=transport,
        resolve=resolve,
    )
    integrations = IntegrationRegistry()
    integrations.register(integration)
    integrations.seal()

    tools = build_catalog(ToolRegistry())
    executable = ExecutableRegistry()
    executable.register(WebSearchTool())

    app = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            if session.in_transaction():
                await session.commit()

    from fastapi import Depends

    def override_chat(session: AsyncSession = Depends(get_db_session)):
        """Built on the *request's* session, not a second one.

        This matters more than it looks: the route commits the request
        session, so a chat service holding a different one would write a
        proposal that the next turn could never see. Sharing the session is
        also what production does -- the dependency graph gives every service
        in a request the same one.
        """
        authorization = AuthorizationService(registry=tools)
        executions = ExecutionService(
            session,
            settings=execution_settings,
            authorization=authorization,
            dispatcher=Dispatcher(
                session, settings=execution_settings,
                authorization=authorization, registry=executable,
                integrations=integrations,
            ),
            executable=executable,
        )
        return ChatService(
            session=session,
            provider=fake_provider,
            settings=execution_settings,
            # The same formatter production builds -- with runtime facts.
            # Constructing a bare one here would quietly drop the authoritative
            # facts section and make every ordering assertion below test a
            # prompt shape that does not exist outside this fixture.
            prompt_formatter=PromptFormatter(
                system_prompt=execution_settings.MAI_SYSTEM_PROMPT,
                runtime_facts=build_runtime_facts(
                    settings=execution_settings, provider=fake_provider
                ),
            ),
            research_service=ResearchService(
                session,
                settings=execution_settings,
                executions=executions,
                integrations=integrations,
            ),
        )

    from app.api.deps import get_chat_service

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: execution_settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_chat_service] = override_chat

    transport_holder = transport
    transport_client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )
    transport_client.search_transport = transport_holder
    transport_client.search_integration = integration

    async with transport_client as http_client:
        yield http_client

    app.dependency_overrides.clear()
