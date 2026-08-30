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

    Calls made with `json_mode=True` are extraction calls. They are recorded
    separately and answered with a canned payload, so one fake serves the chat
    turn, the memory extraction that follows it, the entity extraction after
    that, and the relationship extraction after that. The three extraction
    kinds are told apart by their system prompt.
    """

    name = "fake"

    #: Returned for json_mode calls when nothing else is configured.
    NO_MEMORIES = '{"should_store_memory": false, "memories": []}'
    NO_ENTITIES = '{"entities": []}'
    NO_RELATIONSHIPS = '{"relationships": []}'

    def __init__(self, reply: str = "Hello from Mai.") -> None:
        self.reply = reply
        self.calls: List[List[LLMMessage]] = []
        self.extraction_calls: List[List[LLMMessage]] = []
        self.entity_calls: List[List[LLMMessage]] = []
        self.relationship_calls: List[List[LLMMessage]] = []
        self.extraction_reply: str = self.NO_MEMORIES
        self.entity_reply: str = self.NO_ENTITIES
        self.relationship_reply: str = self.NO_RELATIONSHIPS
        self.raise_error: Optional[Exception] = None
        self.extraction_error: Optional[Exception] = None
        self.entity_error: Optional[Exception] = None
        self.relationship_error: Optional[Exception] = None

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
        if json_mode and self._is_relationship_call(messages):
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
