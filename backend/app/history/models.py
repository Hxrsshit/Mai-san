"""Raw archive ORM models for imported conversation history.

These tables are an **archive**, not memory. They exist so an imported
statement can always be traced back to the exact message that produced it, and
so the user's history survives independently of whatever Mai derived from it.

Three rules shape the schema:

1. **Separate from live conversations.** Imported conversations are not rows in
   `conversations`. Putting them there would make historical text continuable,
   listable beside real chats, and eligible for the live context window --
   which is precisely what "do not treat raw history as active memory" forbids.
2. **Idempotent by content.** `ImportedArchive.source_sha256` is unique, so
   importing the same export twice is a no-op rather than a duplicate corpus.
3. **Redacted before persistence.** Message text is scrubbed of credential
   shapes on the way in. An export can contain an API key the user pasted into
   a chat years ago, and copying that into a new table would be creating a
   fresh secret-at-rest liability, not preserving history.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow


def _enum_values(enum_cls) -> list:
    """Store the lowercase values, not the Python member names."""
    return [member.value for member in enum_cls]


class ImportFormat(str, enum.Enum):
    """Export shapes this system accepts. Closed: anything else is refused.

    Detection happens before parsing, so an unrecognised file is rejected
    without its contents ever being interpreted.
    """

    #: A ChatGPT export `.zip` containing `conversations.json`.
    CHATGPT_ZIP = "chatgpt_zip"
    #: A bare `conversations.json` lifted out of such an export.
    CHATGPT_JSON = "chatgpt_json"


class ImportStatus(str, enum.Enum):
    """Where an import run got to.

    `PARTIAL` is a real outcome, not a failure dressed up: a bounded parser
    that stops at a limit has still imported everything before the limit, and
    saying so is more useful than discarding the work.
    """

    PENDING = "pending"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ImportedRole(str, enum.Enum):
    """Who authored an archived message.

    Closed, and mapped from the export's free-text role. An unrecognised role
    becomes `UNKNOWN` rather than being trusted or dropped -- the message is
    still archived, it simply never qualifies as a statement by the user.

    The distinction carries authority: only `USER` messages are first-person
    evidence about the user. `ASSISTANT` is model-generated content, and
    `SYSTEM` is instruction text that must never be mined for memories at all.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    UNKNOWN = "unknown"


#: Roles whose content may be mined for derived memories.
#:
#: A single-member frozenset rather than "everything except SYSTEM", so a role
#: added later is excluded by default. Stage 4C's tool registry uses the same
#: shape for the same reason.
EXTRACTABLE_ROLES = frozenset({ImportedRole.USER})


class ImportedArchive(Base):
    """One import run over one export file."""

    __tablename__ = "imported_archives"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: The file's basename. Never a path: the directory is server
    #: configuration, and echoing a caller-supplied path back would turn this
    #: column into a traversal oracle.
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    #: SHA-256 of the file's bytes. The idempotency key -- see the unique
    #: index below. Content-addressed rather than name-addressed because a
    #: renamed export is the same export.
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    source_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    import_format: Mapped[ImportFormat] = mapped_column(
        Enum(ImportFormat, name="import_format", values_callable=_enum_values),
        nullable=False,
    )
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, name="import_status", values_callable=_enum_values),
        nullable=False,
        default=ImportStatus.PENDING,
    )

    conversations_imported: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    messages_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Conversations and messages the bounds stopped us from taking.
    conversations_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    messages_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: How many credential shapes were masked on the way in. A count, never
    #: the values, and never where they were.
    redactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memories_derived: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: An application reason code on failure. Never a raw exception string: a
    #: driver exception can embed a DSN, and a parser exception can embed the
    #: document fragment that broke it.
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("source_bytes >= 0", name="source_bytes_non_negative"),
        CheckConstraint(
            "conversations_imported >= 0 AND messages_imported >= 0 "
            "AND conversations_skipped >= 0 AND messages_skipped >= 0 "
            "AND redactions >= 0 AND memories_derived >= 0",
            name="counters_non_negative",
        ),
        # Idempotency, enforced by the database rather than by a check-then-act
        # in the service: two concurrent imports of the same file cannot both
        # win, whatever the application believes.
        Index("uq_imported_archives_source_sha256", "source_sha256", unique=True),
        Index("ix_imported_archives_status_created_at", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ImportedArchive {self.source_filename!r} {self.status.value}>"


class ImportedConversation(Base):
    """One conversation inside an imported archive."""

    __tablename__ = "imported_conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    archive_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("imported_archives.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: The export's own conversation id. Untrusted text, length-capped, and
    #: used only for idempotency within an archive.
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Titles are user-authored and can say anything. Stored as data.
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    #: When the conversation happened, per the export. Nullable because an
    #: export may omit it, and inventing a timestamp would be a lie that the
    #: temporal conflict rules would then act on.
    source_created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("message_count >= 0", name="message_count_non_negative"),
        Index(
            "uq_imported_conversations_archive_id_external_id",
            "archive_id",
            "external_id",
            unique=True,
        ),
        Index("ix_imported_conversations_archive_id", "archive_id"),
        Index("ix_imported_conversations_source_created_at", "source_created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ImportedConversation {self.external_id!r}>"


class ImportedMessage(Base):
    """One archived message. Redacted, role-attributed, ordered."""

    __tablename__ = "imported_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("imported_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    role: Mapped[ImportedRole] = mapped_column(
        Enum(ImportedRole, name="imported_role", values_callable=_enum_values),
        nullable=False,
    )

    #: Redacted message text, truncated to the configured per-message cap.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: The export's `content_type` (`text`, `code`, `multimodal_text`, ...),
    #: kept so a later reader knows what it is looking at. Untrusted, capped.
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    #: Position within the conversation, after the export's node graph has been
    #: walked into a linear order.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    source_created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Credential shapes masked in this message. A count only.
    redactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: True when the text hit the per-message cap and was cut.
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("redactions >= 0", name="redactions_non_negative"),
        Index(
            "uq_imported_messages_conversation_id_external_id",
            "conversation_id",
            "external_id",
            unique=True,
        ),
        Index("ix_imported_messages_conversation_id_sequence",
              "conversation_id", "sequence"),
        Index("ix_imported_messages_role", "role"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ImportedMessage {self.role.value} seq={self.sequence}>"


__all__ = [
    "EXTRACTABLE_ROLES",
    "ImportFormat",
    "ImportStatus",
    "ImportedArchive",
    "ImportedConversation",
    "ImportedMessage",
    "ImportedRole",
]
