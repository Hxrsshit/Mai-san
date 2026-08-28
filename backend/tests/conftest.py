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
from app.database.models import Base
from app.database.session import get_db_session
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, ProviderHealth
from app.llm.factory import get_llm_provider
from app.main import create_app


# --- Fake provider ----------------------------------------------------------


class FakeLLMProvider(LLMProvider):
    """Records what it was asked and returns a canned reply.

    Set `raise_error` to make the next call fail, which is how the error-path
    tests exercise the API without touching the network.
    """

    name = "fake"

    def __init__(self, reply: str = "Hello from Mai.") -> None:
        self.reply = reply
        self.calls: List[List[LLMMessage]] = []
        self.raise_error: Optional[Exception] = None

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate_response(
        self,
        messages: List[LLMMessage],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if self.raise_error is not None:
            raise self.raise_error
        return LLMResponse(
            content=self.reply,
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


# --- Settings ---------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        LLM_PROVIDER="groq",
        GROQ_API_KEY="test-key",
        GLM_API_KEY="test-key",
        LOG_LEVEL="WARNING",
        MAI_SYSTEM_PROMPT="You are Mai.",
        MAX_CONTEXT_MESSAGES=40,
    )


# --- Database ---------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator["async_sessionmaker[AsyncSession]"]:
    """A fresh in-memory schema per test.

    StaticPool keeps every connection pointed at the same in-memory database.
    """
    from sqlalchemy import event
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite ignores foreign keys unless this is switched on per connection.
    # Without it, ON DELETE CASCADE would silently not happen and the cascade
    # test would pass without testing anything.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_llm_provider] = lambda: fake_provider
    app.dependency_overrides[get_settings] = lambda: settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def conversation_id(client: AsyncClient) -> uuid.UUID:
    response = await client.post("/api/conversations", json={})
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])
