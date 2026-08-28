"""Conversation ORM model."""

import uuid
from typing import List, TYPE_CHECKING

from sqlalchemy import String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, TimestampMixin

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers only
    from app.database.models.message import Message

DEFAULT_CONVERSATION_TITLE = "New conversation"


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    # `Uuid` is SQLAlchemy's portable UUID type: native UUID on PostgreSQL,
    # CHAR(32) elsewhere. Keeps the schema portable for a later cloud move.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default=DEFAULT_CONVERSATION_TITLE
    )

    messages: Mapped[List["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation id={self.id} title={self.title!r}>"
