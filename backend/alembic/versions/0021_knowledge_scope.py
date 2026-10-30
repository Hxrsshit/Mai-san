"""Stage 7B (L0): knowledge-scope columns on the ten scoped tables.

Adds three nullable columns -- `organisation_id`, `scope_id`,
`scope_visibility` -- one CHECK (`ck_<table>_knowledge_scope_valid`) and one
index (`ix_<table>_scope_id`) to:

    conversations, memories, entities, entity_aliases, relationships,
    knowledge_conflicts, reminders, executions, workflows, imported_archives

No existing column, constraint or index changes. The global unique indexes
(for example `uq_memories_type_normalized_content`) are left exactly as they
are: replacing them with scoped ones needs `scope_id` to be NOT NULL first,
which is a later, separately approved migration.

**Backfill.** Every existing row's `organisation_id` is set to the default
organisation (`...b1b1`, seeded by 0020) -- the only organisation there has
ever been. `scope_id` and `scope_visibility` are **not** backfilled: legacy
rows stay unattributed. Before Stage 7A the Telegram webhook did not check
the sender, so legacy conversations and everything derived from them may
hold statements that are not the Admin's; nothing here can tell which
conversation was the Telegram one (that is configuration, which a migration
must not read). Attribution, or quarantine, is a separate owner decision.
If the default organisation is missing the upgrade refuses rather than
attributing rows to nothing.

There is no default and no server default on any of the columns: rows
written after this migration stay unattributed until the code that writes
them sets a scope from an authenticated principal.

**SQLite** cannot add a table-level constraint to an existing table, so the
CHECK is attached to `scope_visibility` as a column constraint, as 0017 does
-- no table rebuild, so no existing constraint can be lost.

**Downgrade** refuses -- and changes nothing -- if dropping the columns would
lose information: any row with a scope, or any row attributed to an
organisation other than the default. Otherwise the only data dropped is the
default-organisation backfill, which this upgrade recreates exactly.

**Offline mode** (`alembic upgrade --sql`) is refused in both directions.
Both safety checks above read the database, and offline mode cannot:
generating a script without them would skip the refusals. Run 0021 online
against the target database.
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORGANISATION_ID = uuid.UUID("00000000-0000-0000-0000-00000000b1b1")

SCOPED_TABLES = (
    "conversations",
    "memories",
    "entities",
    "entity_aliases",
    "relationships",
    "knowledge_conflicts",
    "reminders",
    "executions",
    "workflows",
    "imported_archives",
)

# Must equal app.database.models.scope.KNOWLEDGE_SCOPE_CHECK_SQL (a test checks).
CHECK_SQL = (
    "(scope_id IS NULL AND scope_visibility IS NULL)"
    " OR (scope_id IS NOT NULL AND organisation_id IS NOT NULL"
    " AND scope_visibility IS NOT NULL"
    " AND ((scope_visibility = 'personal' AND scope_id <> organisation_id)"
    " OR (scope_visibility = 'organisation' AND scope_id = organisation_id)))"
)


def _check_name(table: str) -> str:
    return f"ck_{table}_knowledge_scope_valid"


def _index_name(table: str) -> str:
    return f"ix_{table}_scope_id"


def _scope_table(table: str) -> sa.TableClause:
    return sa.table(
        table,
        sa.column("organisation_id", sa.Uuid(as_uuid=True)),
        sa.column("scope_id", sa.Uuid(as_uuid=True)),
        sa.column("scope_visibility", sa.String),
    )


def _refuse_offline(direction: str) -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            f"0021 refused: offline (--sql) {direction} is not supported. Its safety "
            "checks must read the database, which offline mode cannot; run it online "
            "against the target database. Nothing was generated."
        )


def upgrade() -> None:
    _refuse_offline("upgrade")
    bind = op.get_bind()
    organisations = sa.table("organisations", sa.column("id", sa.Uuid(as_uuid=True)))
    present = bind.execute(
        sa.select(sa.func.count()).select_from(organisations)
        .where(organisations.c.id == DEFAULT_ORGANISATION_ID)
    ).scalar_one()
    if present != 1:
        raise RuntimeError(
            "0021 refused: the default organisation (seeded by 0020) is missing, "
            "so legacy rows cannot be attributed to it. Nothing was changed."
        )

    postgresql = bind.dialect.name == "postgresql"
    for table in SCOPED_TABLES:
        op.add_column(table, sa.Column("organisation_id", sa.Uuid(as_uuid=True), nullable=True))
        op.add_column(table, sa.Column("scope_id", sa.Uuid(as_uuid=True), nullable=True))
        if postgresql:
            op.add_column(table, sa.Column("scope_visibility", sa.String(length=16), nullable=True))
            op.create_check_constraint(op.f(_check_name(table)), table, CHECK_SQL)
        else:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN scope_visibility VARCHAR(16)"
                f" CONSTRAINT {_check_name(table)} CHECK ({CHECK_SQL})"
            )
        op.create_index(_index_name(table), table, ["scope_id"])

        scoped = _scope_table(table)
        op.execute(
            scoped.update()
            .where(scoped.c.organisation_id.is_(None))
            .values(organisation_id=DEFAULT_ORGANISATION_ID)
        )


def downgrade() -> None:
    _refuse_offline("downgrade")
    bind = op.get_bind()
    blocked = []
    for table in SCOPED_TABLES:
        scoped = _scope_table(table)
        count = bind.execute(
            sa.select(sa.func.count()).select_from(scoped).where(
                sa.or_(
                    scoped.c.scope_id.is_not(None),
                    scoped.c.scope_visibility.is_not(None),
                    sa.and_(
                        scoped.c.organisation_id.is_not(None),
                        scoped.c.organisation_id != DEFAULT_ORGANISATION_ID,
                    ),
                )
            )
        ).scalar_one()
        if count:
            blocked.append(f"{table}: {count}")
    if blocked:
        raise RuntimeError(
            "0021 downgrade refused: dropping the scope columns would lose ownership "
            "(rows with a scope, or attributed to an organisation other than the "
            "default): " + ", ".join(blocked) + ". Nothing was changed."
        )

    postgresql = bind.dialect.name == "postgresql"
    for table in reversed(SCOPED_TABLES):
        op.drop_index(_index_name(table), table_name=table)
        if postgresql:
            op.drop_constraint(op.f(_check_name(table)), table, type_="check")
            for column in ("scope_visibility", "scope_id", "organisation_id"):
                op.drop_column(table, column)
        else:
            # SQLite >= 3.35 drops a column in place. scope_visibility goes
            # first: the CHECK is its own column constraint and names the
            # other two columns.
            for column in ("scope_visibility", "scope_id", "organisation_id"):
                op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
