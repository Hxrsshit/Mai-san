"""Stage 5C: imported history archive, and memory provenance for it.

Three new tables plus four changes to `memories`.

The archive is deliberately *not* `conversations` / `messages`. Imported
history must not be continuable, must not appear beside live chats, and must
never be eligible for the live context window; giving it its own tables makes
that a schema property instead of a rule someone has to remember.

The `memories` changes are the delicate part, because existing rows must keep
behaving exactly as they did:

- `origin` arrives with a server default of `'live'`, so every existing row is
  live without a data migration;
- `stated_at` is added nullable, backfilled from `created_at`, and only then
  made NOT NULL. For every pre-existing row the two are identical, which is
  what makes the conflict-detector's switch to `stated_at` a no-op for live
  knowledge;
- `source_conversation_id` becomes nullable, because an imported memory's
  provenance root is an archived message instead. A CHECK keeps that from
  becoming "no provenance at all": exactly one root, matching the origin.

Portability notes, unchanged from earlier migrations:

- `batch_alter_table` so one migration serves both dialects. SQLite cannot
  ALTER a column's nullability and rebuilds the table, preserving only the
  constraints it is told about, so the existing CHECKs are passed through
  `table_args`. PostgreSQL performs no rebuild and ignores them.
- Enum types are created natively on PostgreSQL and dropped on downgrade,
  since PostgreSQL keeps a type after its table is gone.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IMPORT_FORMATS = ("chatgpt_zip", "chatgpt_json")
IMPORT_STATUSES = (
    "pending", "parsing", "extracting", "completed", "partial", "failed",
)
IMPORTED_ROLES = ("user", "assistant", "system", "tool", "unknown")
MEMORY_ORIGINS = ("live", "imported")

#: The CHECKs already on `memories`. Described for the SQLite rebuild and
#: ignored by PostgreSQL -- see the docstring. Re-creating them on PostgreSQL
#: would fail outright, which is how the equivalent list in 0009 was found.
def _memory_checks():
    return (
        sa.CheckConstraint(
            "importance_score >= 1 AND importance_score <= 10",
            name="ck_memories_importance_score_range",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name="ck_memories_confidence_score_range",
        ),
    )


PROVENANCE_CHECK = (
    "(origin = 'live'"
    " AND source_conversation_id IS NOT NULL"
    " AND source_imported_message_id IS NULL)"
    " OR (origin = 'imported'"
    " AND source_imported_message_id IS NOT NULL"
    " AND source_conversation_id IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "imported_archives",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "import_format",
            sa.Enum(*IMPORT_FORMATS, name="import_format"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*IMPORT_STATUSES, name="import_status"),
            nullable=False,
        ),
        sa.Column("conversations_imported", sa.Integer(), nullable=False),
        sa.Column("messages_imported", sa.Integer(), nullable=False),
        sa.Column("conversations_skipped", sa.Integer(), nullable=False),
        sa.Column("messages_skipped", sa.Integer(), nullable=False),
        sa.Column("redactions", sa.Integer(), nullable=False),
        sa.Column("memories_derived", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_imported_archives")),
        sa.CheckConstraint(
            "source_bytes >= 0",
            name=op.f("ck_imported_archives_source_bytes_non_negative"),
        ),
        sa.CheckConstraint(
            "conversations_imported >= 0 AND messages_imported >= 0 "
            "AND conversations_skipped >= 0 AND messages_skipped >= 0 "
            "AND redactions >= 0 AND memories_derived >= 0",
            name=op.f("ck_imported_archives_counters_non_negative"),
        ),
    )
    op.create_index(
        "uq_imported_archives_source_sha256",
        "imported_archives", ["source_sha256"], unique=True,
    )
    op.create_index(
        "ix_imported_archives_status_created_at",
        "imported_archives", ["status", "created_at"],
    )

    op.create_table(
        "imported_conversations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("archive_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_imported_conversations")),
        sa.ForeignKeyConstraint(
            ["archive_id"], ["imported_archives.id"], ondelete="CASCADE",
            name=op.f("fk_imported_conversations_archive_id_imported_archives"),
        ),
        sa.CheckConstraint(
            "message_count >= 0",
            name=op.f("ck_imported_conversations_message_count_non_negative"),
        ),
    )
    op.create_index(
        "uq_imported_conversations_archive_id_external_id",
        "imported_conversations", ["archive_id", "external_id"], unique=True,
    )
    op.create_index(
        "ix_imported_conversations_archive_id",
        "imported_conversations", ["archive_id"],
    )
    op.create_index(
        "ix_imported_conversations_source_created_at",
        "imported_conversations", ["source_created_at"],
    )

    op.create_table(
        "imported_messages",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column(
            "role", sa.Enum(*IMPORTED_ROLES, name="imported_role"), nullable=False
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redactions", sa.Integer(), nullable=False),
        sa.Column(
            "truncated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_imported_messages")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["imported_conversations.id"], ondelete="CASCADE",
            name=op.f(
                "fk_imported_messages_conversation_id_imported_conversations"
            ),
        ),
        sa.CheckConstraint(
            "sequence >= 0", name=op.f("ck_imported_messages_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "redactions >= 0",
            name=op.f("ck_imported_messages_redactions_non_negative"),
        ),
    )
    op.create_index(
        "uq_imported_messages_conversation_id_external_id",
        "imported_messages", ["conversation_id", "external_id"], unique=True,
    )
    op.create_index(
        "ix_imported_messages_conversation_id_sequence",
        "imported_messages", ["conversation_id", "sequence"],
    )
    op.create_index("ix_imported_messages_role", "imported_messages", ["role"])

    # --- memories -----------------------------------------------------------
    #
    # Two passes. The additive columns and the backfill come first, so that by
    # the time the rebuild runs every row already satisfies the new CHECK.
    origin_enum = sa.Enum(*MEMORY_ORIGINS, name="memory_origin")
    origin_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "memories",
        sa.Column(
            "origin", origin_enum, nullable=False, server_default="live"
        ),
    )
    op.add_column(
        "memories",
        sa.Column("stated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column(
            "source_imported_message_id", sa.Uuid(as_uuid=True), nullable=True
        ),
    )

    # Every pre-existing memory was stated when it was recorded. This equality
    # is what makes the conflict detector's move to `stated_at` invisible to
    # live knowledge.
    op.execute("UPDATE memories SET stated_at = created_at WHERE stated_at IS NULL")

    with op.batch_alter_table(
        "memories", schema=None, table_args=_memory_checks()
    ) as batch:
        batch.alter_column(
            "stated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
        batch.alter_column(
            "source_conversation_id",
            existing_type=sa.Uuid(as_uuid=True),
            nullable=True,
        )
        batch.create_foreign_key(
            op.f("fk_memories_source_imported_message_id_imported_messages"),
            "imported_messages", ["source_imported_message_id"], ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "provenance_matches_origin", PROVENANCE_CHECK
        )

    op.create_index(
        "ix_memories_source_imported_message_id",
        "memories", ["source_imported_message_id"],
    )
    op.create_index("ix_memories_stated_at", "memories", ["stated_at"])
    op.create_index("ix_memories_origin", "memories", ["origin"])


def downgrade() -> None:
    """Reverse order: memory changes, then the archive tables they reference."""
    op.drop_index("ix_memories_origin", table_name="memories")
    op.drop_index("ix_memories_stated_at", table_name="memories")
    op.drop_index("ix_memories_source_imported_message_id", table_name="memories")

    # Imported memories cannot survive the removal of their provenance root,
    # and leaving them behind would violate the NOT NULL restored below.
    op.execute("DELETE FROM memories WHERE origin = 'imported'")

    with op.batch_alter_table(
        "memories", schema=None, table_args=_memory_checks()
    ) as batch:
        # The bare name: the naming convention adds the `ck_memories_`
        # prefix, and passing the already-prefixed name gets it prefixed
        # twice.
        batch.drop_constraint("provenance_matches_origin", type_="check")
        batch.drop_constraint(
            op.f("fk_memories_source_imported_message_id_imported_messages"),
            type_="foreignkey",
        )
        batch.alter_column(
            "source_conversation_id",
            existing_type=sa.Uuid(as_uuid=True),
            nullable=False,
        )

    op.drop_column("memories", "source_imported_message_id")
    op.drop_column("memories", "stated_at")
    op.drop_column("memories", "origin")

    op.drop_index("ix_imported_messages_role", table_name="imported_messages")
    op.drop_index(
        "ix_imported_messages_conversation_id_sequence",
        table_name="imported_messages",
    )
    op.drop_index(
        "uq_imported_messages_conversation_id_external_id",
        table_name="imported_messages",
    )
    op.drop_table("imported_messages")

    op.drop_index(
        "ix_imported_conversations_source_created_at",
        table_name="imported_conversations",
    )
    op.drop_index(
        "ix_imported_conversations_archive_id", table_name="imported_conversations"
    )
    op.drop_index(
        "uq_imported_conversations_archive_id_external_id",
        table_name="imported_conversations",
    )
    op.drop_table("imported_conversations")

    op.drop_index(
        "ix_imported_archives_status_created_at", table_name="imported_archives"
    )
    op.drop_index(
        "uq_imported_archives_source_sha256", table_name="imported_archives"
    )
    op.drop_table("imported_archives")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="memory_origin").drop(bind, checkfirst=True)
        sa.Enum(name="imported_role").drop(bind, checkfirst=True)
        sa.Enum(name="import_status").drop(bind, checkfirst=True)
        sa.Enum(name="import_format").drop(bind, checkfirst=True)
