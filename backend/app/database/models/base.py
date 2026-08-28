"""Declarative base and shared column conventions."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming conventions so Alembic autogenerates stable constraint names
# instead of leaving them to the database.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Timezone-aware current time, used as the Python-side column default.

    Timestamps are populated in Python rather than left to the server default:
    a server-side default leaves the attribute expired after flush, and reading
    it back would trigger a lazy refresh -- which raises MissingGreenlet under
    the async session. `server_default` is kept as well so rows inserted
    outside the ORM (migrations, manual SQL) still get a value.
    """
    return datetime.now(timezone.utc)


class TimestampMixin:
    """created_at / updated_at maintained on every write."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        # Conversations are always listed most-recently-active first.
        index=True,
    )
