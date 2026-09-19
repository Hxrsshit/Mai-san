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
        #: A scripted sequence of chat replies, consumed one per chat call.
        #:
        #: Stage 5A.2 needs a model that answers badly and then well, which a
        #: single fixed reply cannot express. `None` keeps the old behaviour
        #: exactly, and the script falls back to `self.reply` once exhausted
        #: -- so a test scripting one bad turn does not have to predict how
        #: many calls the recovery path will make.
        self.replies: Optional[List[str]] = None
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
            if self.replies:
                content = self.replies.pop(0)
            else:
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

# --- Stage 5C: history import -----------------------------------------------


def chatgpt_export(conversations) -> list:
    """A ChatGPT export document from a compact description.

    `conversations` is a list of dicts with `id`, `title`, `created` (epoch
    seconds) and `turns` -- a list of `(role, text)` pairs. The function builds
    the real export shape around them: a `mapping` of parent-linked nodes and a
    `current_node` naming the leaf, because that graph *is* the thing the
    parser has to get right, and a fixture that flattened it would test a
    format ChatGPT does not produce.
    """
    document = []
    for spec in conversations:
        mapping = {
            "root": {"id": "root", "parent": None, "children": [], "message": None}
        }
        parent = "root"
        for index, (role, text) in enumerate(spec["turns"]):
            node_id = f"{spec['id']}-n{index}"
            mapping[parent]["children"].append(node_id)
            mapping[node_id] = {
                "id": node_id,
                "parent": parent,
                "children": [],
                "message": {
                    "id": f"{spec['id']}-m{index}",
                    "author": {"role": role},
                    "create_time": spec.get("created", 1690000000.0) + index,
                    "content": {"content_type": "text", "parts": [text]},
                },
            }
            parent = node_id
        document.append(
            {
                "conversation_id": spec["id"],
                "title": spec.get("title", ""),
                "create_time": spec.get("created", 1690000000.0),
                "update_time": spec.get("created", 1690000000.0) + 100,
                "current_node": parent,
                "mapping": mapping,
            }
        )
    return document


@pytest.fixture
def import_dir(tmp_path):
    """A real directory the importer reads from.

    Real, because every guarantee in `app.history.sources` is about the
    filesystem -- symlink resolution and path identity cannot be mocked
    without mocking away the thing under test.
    """
    directory = tmp_path / "imports"
    directory.mkdir()
    return directory


@pytest.fixture
def import_settings(settings: Settings, import_dir) -> Settings:
    return settings.model_copy(
        update={
            "MAI_IMPORT_DIR": str(import_dir),
            "HISTORY_IMPORT_ENABLED": True,
            "IMPORT_MEMORY_EXTRACTION_ENABLED": True,
        }
    )


@pytest.fixture
def write_export(import_dir):
    """Write an export into the import directory; returns its filename."""
    import json as _json
    import zipfile as _zipfile

    def write(conversations, name="export.zip", as_zip=True):
        document = (
            conversations
            if isinstance(conversations, (dict, str))
            else chatgpt_export(conversations)
        )
        raw = document if isinstance(document, str) else _json.dumps(document)
        path = import_dir / name
        if as_zip:
            with _zipfile.ZipFile(path, "w") as archive:
                archive.writestr("conversations.json", raw)
        else:
            path.write_text(raw)
        return name

    return write


@pytest_asyncio.fixture
async def import_session_factory(tmp_path) -> AsyncIterator[
    "async_sessionmaker[AsyncSession]"
]:
    """A file-backed database, because an import schedules background work.

    The main `session_factory` is StaticPool over `:memory:`, which points
    every session at one connection. The import route hands its derived
    memory ids to `run_entity_extraction`, which opens its own session -- and
    on a shared connection that fails with "cannot start a transaction within
    a transaction", exactly as `concurrent_session_factory` documents.

    A file gives each session its own connection, which is also the shape
    production has. Testing the background chain against a single shared
    connection would not be testing the thing that runs.
    """
    from app.database.session import configure_sqlite

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'import.db'}")
    configure_sqlite(engine)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    await engine.dispose()


@pytest_asyncio.fixture
async def import_client(
    import_session_factory, fake_provider: FakeLLMProvider, import_settings: Settings
) -> AsyncIterator[AsyncClient]:
    """The app with history import switched on and pointed at a temp dir."""
    app = create_app()
    session_factory = import_session_factory

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
    app.dependency_overrides[get_settings] = lambda: import_settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()


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
    from app.workflows.service import WorkflowService
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
    # Stage 4F-E needs the artifact half too. Registered here rather than in
    # a near-duplicate fixture: two fixtures building the same stack would
    # drift, and the research tests are unaffected by a tool they never name.
    from app.execution.tools import CreateTextFileTool

    executable.register(CreateTextFileTool())

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
            workflow_service=WorkflowService(
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


# --- Stage 4F-G: Google Calendar --------------------------------------------


@pytest.fixture
def calendar_tokens(tmp_path):
    """A connected Google account, in a real token store.

    A real `FileTokenStore` on a real directory, so the permission and path
    behaviour under test is the behaviour that ships. Only the socket is
    replaced.
    """
    from datetime import datetime, timedelta, timezone

    from app.integrations.google_calendar import CALENDAR_READONLY_SCOPE
    from app.integrations.token_store import FileTokenStore, StoredToken

    directory = tmp_path / "credentials"
    store = FileTokenStore(str(directory))
    store.save(
        "google",
        StoredToken(
            access_token="ya29.ACCESS-SENTINEL-NEVER-REAL",
            refresh_token="1//REFRESH-SENTINEL-NEVER-REAL",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            scopes=(CALENDAR_READONLY_SCOPE,),
        ),
    )
    return store


@pytest.fixture
def calendar_settings(execution_settings, tmp_path):
    settings = execution_settings
    settings.GOOGLE_OAUTH_CLIENT_ID = "cid.apps.googleusercontent.com"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "GOCSPX-SENTINEL"
    settings.MAI_CREDENTIAL_DIR = str(tmp_path / "credentials")
    return settings


@pytest_asyncio.fixture
async def calendar_client(
    session_factory, fake_provider, calendar_settings, calendar_tokens,
    workspace, monkeypatch,
) -> AsyncIterator[AsyncClient]:
    """A chat client with a connected calendar and a stubbed Google transport.

    The integration, the `SecureHttpClient` and the `NetworkPolicy` are all
    real -- what these tests exercise is the code that would run against
    Google.
    """
    import app.main as main_module
    from app.calendar.service import CalendarService
    from app.execution.calendar_tool import CalendarListEventsTool
    from app.execution.dispatcher import Dispatcher
    from app.execution.service import ExecutionService
    from app.execution.tools import ExecutableRegistry
    from app.integrations.google_calendar import GoogleCalendarIntegration
    from app.integrations.registry import IntegrationRegistry
    from app.prompt.formatter import PromptFormatter
    from app.runtime.facts import build as build_runtime_facts
    from app.services.chat_service import ChatService
    from app.tools.authorization import AuthorizationService
    from app.tools.catalog import build_catalog
    from app.tools.registry import ToolRegistry
    from tests.support.stub_transport import StubTransport, calendar_payload

    monkeypatch.setattr(main_module, "get_settings", lambda: calendar_settings)

    transport = StubTransport(payload=calendar_payload())

    def resolve(host, port):
        return [(2, 1, 6, "", ("142.250.72.1", port))]

    integration = GoogleCalendarIntegration(
        settings=calendar_settings,
        store=calendar_tokens,
        api_transport=transport,
        token_transport=transport,
        resolve=resolve,
    )
    integrations = IntegrationRegistry()
    integrations.register(integration)
    integrations.seal()

    tools = build_catalog(ToolRegistry())
    executable = ExecutableRegistry()
    executable.register(CalendarListEventsTool())

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
        authorization = AuthorizationService(registry=tools)
        executions = ExecutionService(
            session,
            settings=calendar_settings,
            authorization=authorization,
            dispatcher=Dispatcher(
                session, settings=calendar_settings,
                authorization=authorization, registry=executable,
                integrations=integrations,
            ),
            executable=executable,
        )
        return ChatService(
            session=session,
            provider=fake_provider,
            settings=calendar_settings,
            prompt_formatter=PromptFormatter(
                system_prompt=calendar_settings.MAI_SYSTEM_PROMPT,
                runtime_facts=build_runtime_facts(
                    settings=calendar_settings, provider=fake_provider
                ),
            ),
            calendar_service=CalendarService(
                session,
                settings=calendar_settings,
                executions=executions,
                integrations=integrations,
            ),
        )

    from app.api.deps import get_chat_service

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: calendar_settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_chat_service] = override_chat

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.calendar_transport = transport
    client.calendar_integration = integration
    client.token_store = calendar_tokens

    async with client as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def briefing_client(
    session_factory, fake_provider, calendar_settings, calendar_tokens,
    workspace, monkeypatch,
) -> AsyncIterator[AsyncClient]:
    """A chat client with calendar *and* search, for Stage 4H compositions.

    Both integrations are real -- real `SecureHttpClient`, real
    `NetworkPolicy`, real dispatcher, real authorization -- with only the
    socket replaced by a stub transport on each. What these tests exercise is
    the code that would run against Google and Tavily.

    A separate transport per integration, deliberately: sharing one would make
    "which service was called" unanswerable, and several Stage 4H tests turn
    on exactly that -- that a briefing whose search failed still did not touch
    the calendar twice, and that no request reaches a provider before consent.
    """
    import app.main as main_module
    from app.execution.calendar_tool import CalendarListEventsTool
    from app.execution.dispatcher import Dispatcher
    from app.execution.service import ExecutionService
    from app.execution.tools import CreateTextFileTool, ExecutableRegistry
    from app.execution.web_search_tool import WebSearchTool
    from app.integrations.credentials import EnvironmentCredentialResolver
    from app.integrations.google_calendar import GoogleCalendarIntegration
    from app.integrations.registry import IntegrationRegistry
    from app.integrations.web_search import WebSearchIntegration
    from app.prompt.formatter import PromptFormatter
    from app.research.service import ResearchService
    from app.runtime.facts import build as build_runtime_facts
    from app.services.chat_service import ChatService
    from app.tools.authorization import AuthorizationService
    from app.tools.catalog import build_catalog
    from app.tools.registry import ToolRegistry
    from app.calendar.service import CalendarService
    from app.workflows.service import WorkflowService
    from tests.support.stub_transport import (
        StubTransport, brave_payload, calendar_payload,
    )

    calendar_settings.SEARCH_API_KEY = "SEARCH_SECRET_123"
    monkeypatch.setattr(main_module, "get_settings", lambda: calendar_settings)

    calendar_transport = StubTransport(payload=calendar_payload())
    search_transport = StubTransport(payload=brave_payload(count=2))

    def resolve(host, port):
        return [(2, 1, 6, "", ("142.250.72.1", port))]

    calendar_integration = GoogleCalendarIntegration(
        settings=calendar_settings,
        store=calendar_tokens,
        api_transport=calendar_transport,
        token_transport=calendar_transport,
        resolve=resolve,
    )
    search_integration = WebSearchIntegration(
        credentials=EnvironmentCredentialResolver(
            environ={"SEARCH_API_KEY": "SEARCH_SECRET_123"}
        ),
        transport=search_transport,
        resolve=resolve,
    )
    integrations = IntegrationRegistry()
    integrations.register(calendar_integration)
    integrations.register(search_integration)
    integrations.seal()

    tools = build_catalog(ToolRegistry())
    executable = ExecutableRegistry()
    executable.register(CalendarListEventsTool())
    executable.register(WebSearchTool())
    executable.register(CreateTextFileTool())

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
        authorization = AuthorizationService(registry=tools)
        executions = ExecutionService(
            session,
            settings=calendar_settings,
            authorization=authorization,
            dispatcher=Dispatcher(
                session, settings=calendar_settings,
                authorization=authorization, registry=executable,
                integrations=integrations,
            ),
            executable=executable,
        )
        return ChatService(
            session=session,
            provider=fake_provider,
            settings=calendar_settings,
            prompt_formatter=PromptFormatter(
                system_prompt=calendar_settings.MAI_SYSTEM_PROMPT,
                runtime_facts=build_runtime_facts(
                    settings=calendar_settings, provider=fake_provider
                ),
            ),
            calendar_service=CalendarService(
                session, settings=calendar_settings,
                executions=executions, integrations=integrations,
            ),
            research_service=ResearchService(
                session, settings=calendar_settings,
                executions=executions, integrations=integrations,
            ),
            workflow_service=WorkflowService(
                session, settings=calendar_settings,
                executions=executions, integrations=integrations,
            ),
        )

    from app.api.deps import get_chat_service

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: calendar_settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_chat_service] = override_chat

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.calendar_transport = calendar_transport
    client.search_transport = search_transport
    client.calendar_integration = calendar_integration
    client.search_integration = search_integration
    client.token_store = calendar_tokens
    client.settings = calendar_settings

    async with client as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest.fixture
def gmail_tokens(calendar_settings):
    """A token store holding a *Gmail* grant and nothing else.

    Keyed under the Gmail provider, so a fixture that wants Gmail connected
    and Calendar not connected is the default rather than a special case --
    which is the separation Stage 5B exists to enforce.
    """
    import datetime

    from app.integrations.google_gmail import GMAIL_READONLY_SCOPE, TOKEN_PROVIDER
    from app.integrations.token_store import FileTokenStore, StoredToken

    # The settings' own credential directory, not a private one.
    #
    # Both the fixture's integration and the *registered* one the API routes
    # read resolve their store from settings, so a private directory made
    # `/api/integrations/gmail/status` disagree with the chat path -- the same
    # split Stage 4F-G had to fix for Calendar. One directory holding one file
    # per provider is also the real deployment shape.
    store = FileTokenStore(calendar_settings.MAI_CREDENTIAL_DIR)
    store.save(
        TOKEN_PROVIDER,
        StoredToken(
            access_token="ya29.GMAIL-ACCESS-SENTINEL-NEVER-REAL",
            refresh_token="1//GMAIL-REFRESH-SENTINEL-NEVER-REAL",
            expires_at=(
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=1)
            ),
            scopes=frozenset({GMAIL_READONLY_SCOPE}),
            account="default",
        ),
    )
    return store


@pytest_asyncio.fixture
async def gmail_client(
    session_factory, fake_provider, calendar_settings, gmail_tokens,
    workspace, monkeypatch,
) -> AsyncIterator[AsyncClient]:
    """A chat client with Gmail connected and a stubbed Google transport.

    The integration, the `SecureHttpClient` and the `NetworkPolicy` are all
    real -- what these tests exercise is the code that would run against
    Gmail. Only the socket is replaced.

    Calendar is deliberately *not* connected here: the token store holds a
    Gmail grant only, so any test that finds Calendar working has found a
    privilege-isolation bug.
    """
    import app.main as main_module
    from app.execution.dispatcher import Dispatcher
    from app.execution.gmail_tools import GmailGetMessageTool, GmailListMessagesTool
    from app.execution.service import ExecutionService
    from app.execution.tools import ExecutableRegistry
    from app.integrations.google_gmail import GoogleGmailIntegration
    from app.integrations.registry import IntegrationRegistry
    from app.mail.service import MailService
    from app.prompt.formatter import PromptFormatter
    from app.runtime.facts import build as build_runtime_facts
    from app.services.chat_service import ChatService
    from app.tools.authorization import AuthorizationService
    from app.tools.catalog import build_catalog
    from app.tools.registry import ToolRegistry
    from tests.support.stub_transport import GmailTransport

    monkeypatch.setattr(main_module, "get_settings", lambda: calendar_settings)
    # The *registered* integration -- the one the `/api/integrations/gmail/*`
    # routes read -- resolves its settings globally, so a test that only
    # overrides the dependency leaves the route looking in the real credential
    # directory. Stage 4F-G's OAuth tests patch the same function for the same
    # reason.
    monkeypatch.setattr("app.core.config.get_settings", lambda: calendar_settings)

    transport = GmailTransport()

    def resolve(host, port):
        return [(2, 1, 6, "", ("142.250.72.1", port))]

    integration = GoogleGmailIntegration(
        settings=calendar_settings,
        store=gmail_tokens,
        api_transport=transport,
        token_transport=transport,
        resolve=resolve,
    )
    integrations = IntegrationRegistry()
    integrations.register(integration)
    integrations.seal()

    tools = build_catalog(ToolRegistry())
    executable = ExecutableRegistry()
    executable.register(GmailListMessagesTool())
    executable.register(GmailGetMessageTool())

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
        authorization = AuthorizationService(registry=tools)
        executions = ExecutionService(
            session,
            settings=calendar_settings,
            authorization=authorization,
            dispatcher=Dispatcher(
                session, settings=calendar_settings,
                authorization=authorization, registry=executable,
                integrations=integrations,
            ),
            executable=executable,
        )
        return ChatService(
            session=session,
            provider=fake_provider,
            settings=calendar_settings,
            prompt_formatter=PromptFormatter(
                system_prompt=calendar_settings.MAI_SYSTEM_PROMPT,
                runtime_facts=build_runtime_facts(
                    settings=calendar_settings, provider=fake_provider
                ),
            ),
            mail_service=MailService(
                session, settings=calendar_settings,
                executions=executions, integrations=integrations,
            ),
        )

    from app.api.deps import get_chat_service

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: calendar_settings
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_chat_service] = override_chat

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.gmail_transport = transport
    client.gmail_integration = integration
    client.token_store = gmail_tokens
    client.settings = calendar_settings

    async with client as http_client:
        yield http_client

    app.dependency_overrides.clear()
