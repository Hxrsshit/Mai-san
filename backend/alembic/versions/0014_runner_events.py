"""Stage 6D: the runner's three event types.

Three values added to the `task_event_type` enum, and nothing else. No table
is created, altered or dropped, and no column changes.

`execution_created`, `runner_blocked` and `runner_refused` describe things a
runner does, so Stage 6A declared the vocabulary without them -- a journal
entry for something that cannot happen is worse than a missing one. A runner
exists now, so they do.

Portability: PostgreSQL stores this as a real enum type and needs
`ALTER TYPE ... ADD VALUE`; SQLite stores it as `VARCHAR` with a CHECK
constraint that SQLAlchemy does not emit for this column, so there is nothing
to change there and the upgrade is a no-op.

**Downgrade removes nothing.** PostgreSQL cannot drop a value from an enum
without rewriting the type and every column using it, and a downgrade that
silently rewrote the audit journal would be far worse than one that leaves
three unused labels behind. Rows written with a new value are left intact;
the labels simply become unused.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_VALUES = ("execution_created", "runner_blocked", "runner_refused")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for value in NEW_VALUES:
        # IF NOT EXISTS so a re-run is safe, and one statement per value
        # because PostgreSQL takes them one at a time.
        op.execute(
            f"ALTER TYPE task_event_type ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # Deliberately empty. See the module docstring: removing an enum value
    # means rewriting the type and every column that uses it, and the column
    # in question is an append-only audit journal.
    pass
