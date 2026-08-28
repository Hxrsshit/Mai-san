"""Async database engine and session management."""

from typing import AsyncIterator, Optional

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseError
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _engine_kwargs(settings: Settings) -> dict:
    """Pool options that apply to real servers but not to SQLite."""
    kwargs = {"echo": settings.DB_ECHO, "pool_pre_ping": True}
    if not settings.DATABASE_URL.startswith("sqlite"):
        kwargs["pool_size"] = settings.DB_POOL_SIZE
        kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW
    return kwargs


def _enforce_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Turn on foreign key enforcement for SQLite connections.

    PostgreSQL (the deployment target) enforces ON DELETE CASCADE natively, but
    SQLite ignores foreign keys unless this pragma is set per connection --
    which would silently orphan a deleted conversation's messages. Setting it
    keeps behaviour identical across both engines.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_engine(settings: Optional[Settings] = None) -> AsyncEngine:
    """Create the process-wide engine and session factory."""
    global _engine, _session_factory

    settings = settings or get_settings()
    if _engine is None:
        _engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs(settings))
        _enforce_sqlite_foreign_keys(_engine)
        _session_factory = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info(
            "Database engine initialised",
            extra={"dialect": _engine.dialect.name},
        )
    return _engine


def get_engine() -> AsyncEngine:
    return init_engine()


def get_session_factory() -> "async_sessionmaker[AsyncSession]":
    init_engine()
    assert _session_factory is not None  # set by init_engine
    return _session_factory


async def dispose_engine() -> None:
    """Close all pooled connections. Called on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _session_factory = None


async def check_database_connection() -> bool:
    """Cheap liveness probe used by /health and at startup."""
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health checks must not raise
        logger.error("Database connection check failed", extra={"error": str(exc)})
        return False


async def check_session_connection(session: AsyncSession) -> bool:
    """Liveness probe against an injected session.

    Used by /health so the endpoint tests the same connection path that serves
    real requests (and so it is overridable in tests).
    """
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health checks must not raise
        logger.error("Database session check failed", extra={"error": str(exc)})
        return False


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session with commit/rollback handling.

    The session is committed when the request handler returns normally and
    rolled back if it raises, so routes never manage transactions themselves.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

        try:
            await session.commit()
        except (SQLAlchemyError, OSError) as exc:
            # A commit can still fail after a clean handler (e.g. the database
            # went away mid-request). Surface it as a 503 rather than a 500.
            await session.rollback()
            logger.error("Commit failed", extra={"error": str(exc)})
            raise DatabaseError("Could not commit the transaction.") from exc
