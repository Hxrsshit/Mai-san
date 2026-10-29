"""Stage 7B units 5-6: uniqueness per knowledge scope instead of globally.

The global unique indexes from 0003 and 0004 made one member's knowledge
collide with another's: member B storing the same normalized fact (or entity
name, or alias) as member A hit A's row, and deduplication would have found
it. Each is replaced by two partial unique indexes:

    uq_<table>_scoped_<...>   (scope_id, <columns>)  WHERE scope_id IS NOT NULL
    uq_<table>_legacy_<...>   (<columns>)            WHERE scope_id IS NULL

so attributed rows are unique per scope, and unattributed (legacy,
quarantined) rows keep exactly their old guarantee among themselves. Partial
indexes need no NOT NULL and work on SQLite and PostgreSQL.

    memories        uq_memories_type_normalized_content  (memory_type, normalized_content)
    entities        uq_entities_normalized_name          (normalized_name)
    entity_aliases  uq_entity_aliases_normalized_alias   (normalized_alias)

No data changes. The upgrade cannot fail on existing data: every row that
satisfied the global index satisfies both partial ones.

**Downgrade** restores the global indexes. It refuses -- changing nothing --
if two rows in different scopes (or a scoped and a legacy row) share the
same key, because the global index could not be created and one of them
would otherwise have to be deleted.

**Offline mode** (`--sql`) is refused for the downgrade, whose check reads
the database; the upgrade renders normally.
"""

from typing import Sequence, Tuple, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (table, old global index, new name stem, key columns)
INDEXES: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    ("memories", "uq_memories_type_normalized_content", "type_normalized_content",
     ("memory_type", "normalized_content")),
    ("entities", "uq_entities_normalized_name", "normalized_name", ("normalized_name",)),
    ("entity_aliases", "uq_entity_aliases_normalized_alias", "normalized_alias",
     ("normalized_alias",)),
)


def scoped_name(table: str, stem: str) -> str:
    return f"uq_{table}_scoped_{stem}"


def legacy_name(table: str, stem: str) -> str:
    return f"uq_{table}_legacy_{stem}"


def _where(clause: str) -> dict:
    return {"sqlite_where": sa.text(clause), "postgresql_where": sa.text(clause)}


def upgrade() -> None:
    for table, old, stem, columns in INDEXES:
        op.create_index(
            scoped_name(table, stem), table, ["scope_id", *columns], unique=True,
            **_where("scope_id IS NOT NULL"),
        )
        op.create_index(
            legacy_name(table, stem), table, list(columns), unique=True,
            **_where("scope_id IS NULL"),
        )
        op.drop_index(old, table_name=table)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "0022 refused: offline (--sql) downgrade is not supported. Its check for "
            "keys shared across scopes must read the database; run it online. "
            "Nothing was generated."
        )
    bind = op.get_bind()
    blocked = []
    for table, old, stem, columns in INDEXES:
        key = ", ".join(columns)
        clashes = bind.execute(sa.text(
            f"SELECT COUNT(*) FROM (SELECT {key} FROM {table} GROUP BY {key} "
            f"HAVING COUNT(*) > 1) AS shared"
        )).scalar_one()
        if clashes:
            blocked.append(f"{table}: {clashes}")
    if blocked:
        raise RuntimeError(
            "0022 downgrade refused: the global unique indexes cannot be restored, "
            "because these keys are shared across scopes: " + ", ".join(blocked)
            + ". Nothing was changed."
        )
    for table, old, stem, columns in reversed(INDEXES):
        op.create_index(old, table, list(columns), unique=True)
        op.drop_index(legacy_name(table, stem), table_name=table)
        op.drop_index(scoped_name(table, stem), table_name=table)
