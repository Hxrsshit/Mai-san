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

# How long a SQLite connection waits for a competing writer before failing.
SQLITE_BUSY_TIMEOUT_MS = 5000

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _engine_kwargs(settings: Settings) -> dict:
    """Pool options that apply to real servers but not to SQLite."""
    kwargs = {"echo": settings.DB_ECHO, "pool_pre_ping": True}
    if not settings.DATABASE_URL.startswith("sqlite"):
        kwargs["pool_size"] = settings.DB_POOL_SIZE
        kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW
    return kwargs


def configure_sqlite(engine: AsyncEngine) -> None:
    """Apply the pragmas SQLite needs to behave like the PostgreSQL target.

    Three settings, each fixing a real defect observed in this application:

    - ``foreign_keys``: SQLite ignores foreign keys unless asked, which would
      silently orphan a deleted conversation's messages and memories.
    - ``busy_timeout``: SQLite permits a single writer. Post-turn memory
      extraction runs on its own connection, so it can collide with a chat
      request writing a message. Without a timeout the loser fails instantly
      with "database is locked"; with it, the loser waits. (The main cause of
      that collision was fixed separately -- the chat route now commits before
      queueing extraction -- but concurrent requests can still overlap.)
    - ``journal_mode=WAL``: lets readers proceed during a write, which removes
      most of the remaining contention. Not available for in-memory databases,
      which is harmless -- they have a single connection anyway.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:  # noqa: BLE001 - unsupported for :memory:, not fatal
            pass
        cursor.close()
        # Required for SAVEPOINT to work. The pysqlite driver emits its own
        # implicit BEGIN at the wrong moments, which silently breaks nested
        # transactions -- a savepoint that should have been discarded ends up
        # persisted. Memory storage uses savepoints so one rejected candidate
        # cannot abort the rest of the batch, so this is not optional.
        # See SQLAlchemy's "Serializable isolation / Savepoints" note.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin_immediate(connection):  # pragma: no cover - driver hook
        """Emit the BEGIN the driver is no longer emitting -- as a writer.

        A plain deferred BEGIN starts the transaction as a reader that must
        later upgrade to a writer, and SQLite refuses to wait on that upgrade:
        it returns SQLITE_BUSY immediately rather than risk deadlock, ignoring
        busy_timeout. Taking the write lock up front makes busy_timeout apply,
        so a competing writer waits instead of failing.

        This serialises SQLite transactions, which is an acceptable trade for a
        single-user development database. PostgreSQL uses MVCC and never
        reaches this hook.
        """
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def init_engine(settings: Optional[Settings] = None) -> AsyncEngine:
    """Create the process-wide engine and session factory."""
    global _engine, _session_factory

    settings = settings or get_settings()
    if _engine is None:
        _engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs(settings))
        configure_sqlite(_engine)
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

        if not session.in_transaction():
            # The handler already committed (the chat route does, so that the
            # turn is durable before background work starts). Committing again
            # would autobegin an empty transaction, and on SQLite -- where
            # pooled sessions can share one connection -- that collides with
            # whatever else is using it.
            return

        try:
            await session.commit()
        except (SQLAlchemyError, OSError) as exc:
            # A commit can still fail after a clean handler (e.g. the database
            # went away mid-request). Surface it as a 503 rather than a 500.
            await session.rollback()
            logger.error("Commit failed", extra={"error": str(exc)})
            raise DatabaseError("Could not commit the transaction.") from exc
