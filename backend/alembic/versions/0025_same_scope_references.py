"""Stage 7B unit 10: same-scope references, made relational.

Units 4-9 keep every reference between scoped rows inside one scope in
code (the referenced row is loaded through the scope filter before the
reference is written). This makes it a database guarantee (design 6a):

- `UNIQUE (id, organisation_id, scope_id)` on each scoped parent
  (`uq_<table>_id_scope`);
- beside each existing single-column foreign key, a composite foreign key
  `(ref, organisation_id, scope_id) -> parent (id, organisation_id,
  scope_id)` with no action (`fk_<table>_<ref>_same_scope`). The existing
  key keeps its CASCADE / SET NULL; this one is checked at the end of the
  statement, so a cascade removes the child first and a SET NULL nulls only
  the reference -- a composite key with a NULL component, like a legacy row
  with no scope, is not checked (MATCH SIMPLE).

**Precondition.** The upgrade first counts, per reference, scoped rows that
point at a row of another scope or at an unattributed row, and refuses --
changing nothing -- if there is any. It names the reference and the count,
never a row.

**Dialects.** PostgreSQL (the production dialect) gets the constraints.
SQLite is a test-only dialect whose test schemas are built from the models
(which carry the same constraints); SQLite cannot add a constraint to an
existing table without rebuilding it, so here the upgrade runs the
precondition only.

**Offline mode** (`--sql`) is refused for the upgrade, whose precondition
reads the database. The downgrade only drops constraints: it loses no data
and renders offline.

Not done here, by decision (design 6a): a foreign key from the scope to the
membership, and NOT NULL on the scope columns.
"""

from typing import Sequence, Tuple, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Scoped parents that same-scope references point at.
PARENTS: Tuple[str, ...] = ("conversations", "entities", "memories", "relationships", "workflows")

#: (table, reference column, parent table) -- every reference from a scoped
#: table to a scoped parent.
REFERENCES: Tuple[Tuple[str, str, str], ...] = (
    ("memories", "source_conversation_id", "conversations"),
    ("entity_aliases", "entity_id", "entities"),
    ("relationships", "source_entity_id", "entities"),
    ("relationships", "target_entity_id", "entities"),
    ("knowledge_conflicts", "older_memory_id", "memories"),
    ("knowledge_conflicts", "newer_memory_id", "memories"),
    ("knowledge_conflicts", "triggering_memory_id", "memories"),
    ("knowledge_conflicts", "older_relationship_id", "relationships"),
    ("knowledge_conflicts", "newer_relationship_id", "relationships"),
    ("reminders", "conversation_id", "conversations"),
    ("workflows", "conversation_id", "conversations"),
    ("executions", "conversation_id", "conversations"),
    ("executions", "workflow_id", "workflows"),
)


def parent_key(table: str) -> str:
    return f"uq_{table}_id_scope"


def reference_key(table: str, column: str) -> str:
    return f"fk_{table}_{column}_same_scope"


def violations(bind) -> list:
    """`(table.column, count)` for every reference with scoped rows that
    point outside their own scope. Reads only."""
    found = []
    for table, column, parent in REFERENCES:
        count = bind.execute(sa.text(
            f"SELECT COUNT(*) FROM {table} c WHERE c.scope_id IS NOT NULL "
            f"AND c.{column} IS NOT NULL AND NOT EXISTS ("
            f"SELECT 1 FROM {parent} p WHERE p.id = c.{column} "
            f"AND p.organisation_id = c.organisation_id AND p.scope_id = c.scope_id)"
        )).scalar_one()
        if count:
            found.append((f"{table}.{column}", int(count)))
    return found


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "0025 refused: offline (--sql) upgrade is not supported. Its check for "
            "references across scopes must read the database; run it online. "
            "Nothing was generated."
        )
    bind = op.get_bind()
    found = violations(bind)
    if found:
        raise RuntimeError(
            "0025 refused: these references point outside their own scope: "
            + ", ".join(f"{name}: {count}" for name, count in found)
            + ". Nothing was changed."
        )
    if bind.dialect.name != "postgresql":
        return
    for table in PARENTS:
        op.create_unique_constraint(parent_key(table), table, ["id", "organisation_id", "scope_id"])
    for table, column, parent in REFERENCES:
        op.create_foreign_key(
            reference_key(table, column), table, parent,
            [column, "organisation_id", "scope_id"], ["id", "organisation_id", "scope_id"],
        )


def downgrade() -> None:
    if not context.is_offline_mode() and op.get_bind().dialect.name != "postgresql":
        return
    for table, column, _parent in reversed(REFERENCES):
        op.drop_constraint(reference_key(table, column), table, type_="foreignkey")
    for table in reversed(PARENTS):
        op.drop_constraint(parent_key(table), table, type_="unique")
