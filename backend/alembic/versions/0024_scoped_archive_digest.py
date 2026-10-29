"""Stage 7B unit 9: history-import duplicate check per knowledge scope.

`uq_imported_archives_source_sha256` -- the last global unique index on a
scoped table -- made "this file is already imported" a global answer: one
member's import of a file would block another's, and the duplicate lookup
would have answered with the other member's archive. It is replaced, as
0022 and 0023 replaced theirs, by two partial unique indexes:

    uq_imported_archives_scoped_source_sha256  (scope_id, source_sha256)  WHERE scope_id IS NOT NULL
    uq_imported_archives_legacy_source_sha256  (source_sha256)            WHERE scope_id IS NULL

No data changes; the upgrade cannot fail on existing data.

**Downgrade** restores the global index and refuses -- changing nothing --
if a key is shared across scopes (or by a scoped and a legacy row).
**Offline mode** (`--sql`) is refused for the downgrade, whose check reads
the database; the upgrade renders normally.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "imported_archives"
OLD = "uq_imported_archives_source_sha256"
SCOPED = "uq_imported_archives_scoped_source_sha256"
LEGACY = "uq_imported_archives_legacy_source_sha256"


def _where(clause: str) -> dict:
    return {"sqlite_where": sa.text(clause), "postgresql_where": sa.text(clause)}


def upgrade() -> None:
    op.create_index(SCOPED, TABLE, ["scope_id", "source_sha256"], unique=True,
                    **_where("scope_id IS NOT NULL"))
    op.create_index(LEGACY, TABLE, ["source_sha256"], unique=True,
                    **_where("scope_id IS NULL"))
    op.drop_index(OLD, table_name=TABLE)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "0024 refused: offline (--sql) downgrade is not supported. Its check for "
            "keys shared across scopes must read the database; run it online. "
            "Nothing was generated."
        )
    shared = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM (SELECT source_sha256 FROM imported_archives "
        "GROUP BY source_sha256 HAVING COUNT(*) > 1) AS shared"
    )).scalar_one()
    if shared:
        raise RuntimeError(
            "0024 downgrade refused: the global unique index cannot be restored, "
            f"because {shared} archive digests are shared across scopes. Nothing was changed."
        )
    op.create_index(OLD, TABLE, ["source_sha256"], unique=True)
    op.drop_index(LEGACY, table_name=TABLE)
    op.drop_index(SCOPED, table_name=TABLE)
