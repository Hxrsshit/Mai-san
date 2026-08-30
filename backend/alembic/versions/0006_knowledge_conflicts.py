"""Stage 3C: knowledge conflict links.

Adds one table. **No existing table is altered**, no column is dropped, and no
row is modified -- the memory and relationship status enums already carried
`active` / `superseded` / `archived` from Stages 2A and 2C, so lifecycle
states needed no schema change.

That is deliberate. Extending a PostgreSQL enum requires ALTER TYPE ... ADD
VALUE, which carries transactional restrictions, and the PostgreSQL runtime is
not verified on this machine. A purely additive migration is the safer trade.

Existing rows need no backfill: absence of a conflict link means "no conflict
detected", which is the correct state for every memory and relationship
written before this migration.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONFLICT_RESOLUTIONS = ("superseded", "unresolved")
CONFLICT_REASONS = (
    "explicit_replacement",
    "explicit_abandonment",
    "exclusive_replacement",
    "exclusive_ambiguous",
)


def upgrade() -> None:
    op.create_table(
        "knowledge_conflicts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("older_memory_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("newer_memory_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("older_relationship_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("newer_relationship_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "resolution",
            sa.Enum(*CONFLICT_RESOLUTIONS, name="conflict_resolution"),
            nullable=False,
        ),
        sa.Column(
            "reason",
            sa.Enum(*CONFLICT_REASONS, name="conflict_reason"),
            nullable=False,
        ),
        sa.Column("triggering_memory_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_conflicts"),
        # The populated `older_*` column decides the kind; the other kind's
        # columns must be NULL. `newer_*` stays nullable so knowledge can be
        # retired without a named successor.
        sa.CheckConstraint(
            "("
            " (older_memory_id IS NOT NULL"
            "  AND older_relationship_id IS NULL AND newer_relationship_id IS NULL)"
            " OR "
            " (older_relationship_id IS NOT NULL"
            "  AND older_memory_id IS NULL AND newer_memory_id IS NULL)"
            ")",
            name="ck_knowledge_conflicts_exactly_one_conflict_kind",
        ),
        sa.CheckConstraint(
            "older_memory_id IS NULL OR older_memory_id <> newer_memory_id",
            name="ck_knowledge_conflicts_no_self_memory_conflict",
        ),
        sa.CheckConstraint(
            "older_relationship_id IS NULL"
            " OR older_relationship_id <> newer_relationship_id",
            name="ck_knowledge_conflicts_no_self_relationship_conflict",
        ),
        # CASCADE: deleting knowledge removes the links describing it, so no
        # lifecycle reference is ever orphaned.
        sa.ForeignKeyConstraint(
            ["older_memory_id"], ["memories.id"], ondelete="CASCADE",
            name="fk_knowledge_conflicts_older_memory_id_memories",
        ),
        sa.ForeignKeyConstraint(
            ["newer_memory_id"], ["memories.id"], ondelete="CASCADE",
            name="fk_knowledge_conflicts_newer_memory_id_memories",
        ),
        sa.ForeignKeyConstraint(
            ["older_relationship_id"], ["relationships.id"], ondelete="CASCADE",
            name="fk_knowledge_conflicts_older_relationship_id_relationships",
        ),
        sa.ForeignKeyConstraint(
            ["newer_relationship_id"], ["relationships.id"], ondelete="CASCADE",
            name="fk_knowledge_conflicts_newer_relationship_id_relationships",
        ),
        # SET NULL rather than CASCADE: losing the memory that triggered a
        # decision must not erase the decision itself.
        sa.ForeignKeyConstraint(
            ["triggering_memory_id"], ["memories.id"], ondelete="SET NULL",
            name="fk_knowledge_conflicts_triggering_memory_id_memories",
        ),
    )

    # UNIQUE on each ordered pair. This is the only race-safe guard: two
    # concurrent background evaluations cannot see each other's uncommitted
    # insert, so an application-level check would lose the race.
    op.create_index(
        "uq_knowledge_conflicts_memory_pair",
        "knowledge_conflicts",
        ["older_memory_id", "newer_memory_id"],
        unique=True,
    )
    op.create_index(
        "uq_knowledge_conflicts_relationship_pair",
        "knowledge_conflicts",
        ["older_relationship_id", "newer_relationship_id"],
        unique=True,
    )
    for column in (
        "older_memory_id",
        "newer_memory_id",
        "older_relationship_id",
        "newer_relationship_id",
        "resolution",
    ):
        op.create_index(
            f"ix_knowledge_conflicts_{column}", "knowledge_conflicts", [column]
        )


def downgrade() -> None:
    """Drop the table. No knowledge is lost.

    Conflict links are derived metadata: memories and relationships keep their
    content and their status, so downgrading removes only the explanation of
    why something is historical, never the history itself.
    """
    for column in (
        "older_memory_id",
        "newer_memory_id",
        "older_relationship_id",
        "newer_relationship_id",
        "resolution",
    ):
        op.drop_index(f"ix_knowledge_conflicts_{column}", table_name="knowledge_conflicts")
    op.drop_index(
        "uq_knowledge_conflicts_relationship_pair", table_name="knowledge_conflicts"
    )
    op.drop_index(
        "uq_knowledge_conflicts_memory_pair", table_name="knowledge_conflicts"
    )
    op.drop_table("knowledge_conflicts")

    # PostgreSQL keeps enum types after the table using them is dropped.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="conflict_reason").drop(bind, checkfirst=True)
        sa.Enum(name="conflict_resolution").drop(bind, checkfirst=True)
