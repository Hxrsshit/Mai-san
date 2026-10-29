"""Stage 7B unit 8b: execution idempotency per knowledge scope.

`uq_executions_idempotency_key` made the payload-derived idempotency key
global: member B proposing the same tool call with the same arguments as
member A would collide with A's row -- and the service's "retry returns the
original" lookup would have handed B A's execution. It is replaced, exactly
as 0022 replaced the knowledge indexes, by two partial unique indexes:

    uq_executions_scoped_idempotency_key   (scope_id, idempotency_key)  WHERE scope_id IS NOT NULL
    uq_executions_legacy_idempotency_key   (idempotency_key)            WHERE scope_id IS NULL

No data changes; the upgrade cannot fail on existing data.

**Downgrade** restores the global index and refuses -- changing nothing --
if a key is shared across scopes (or by a scoped and a legacy row).
**Offline mode** (`--sql`) is refused for the downgrade, whose check reads
the database; the upgrade renders normally.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "executions"
OLD = "uq_executions_idempotency_key"
SCOPED = "uq_executions_scoped_idempotency_key"
LEGACY = "uq_executions_legacy_idempotency_key"


def _where(clause: str) -> dict:
    return {"sqlite_where": sa.text(clause), "postgresql_where": sa.text(clause)}


def upgrade() -> None:
    op.create_index(SCOPED, TABLE, ["scope_id", "idempotency_key"], unique=True,
                    **_where("scope_id IS NOT NULL"))
    op.create_index(LEGACY, TABLE, ["idempotency_key"], unique=True,
                    **_where("scope_id IS NULL"))
    op.drop_index(OLD, table_name=TABLE)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "0023 refused: offline (--sql) downgrade is not supported. Its check for "
            "keys shared across scopes must read the database; run it online. "
            "Nothing was generated."
        )
    shared = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM (SELECT idempotency_key FROM executions "
        "GROUP BY idempotency_key HAVING COUNT(*) > 1) AS shared"
    )).scalar_one()
    if shared:
        raise RuntimeError(
            "0023 downgrade refused: the global unique index cannot be restored, "
            f"because {shared} idempotency keys are shared across scopes. Nothing was changed."
        )
    op.create_index(OLD, TABLE, ["idempotency_key"], unique=True)
    op.drop_index(LEGACY, table_name=TABLE)
    op.drop_index(SCOPED, table_name=TABLE)
