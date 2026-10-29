"""Stage 7B (L0): knowledge-scope columns, shared by every scoped table.

Three nullable columns describe who a row belongs to:

    organisation_id   -- the organisation the row belongs to;
    scope_id          -- the knowledge scope inside it: a member's id for
                         personal knowledge, the organisation's id for shared;
    scope_visibility  -- 'personal' or 'organisation'.

Stage 7B L0 adds the columns and nothing writes them: no service sets a
scope yet and no query filters on one. A NULL scope means **unattributed** --
not "everyone's" and not the Admin's. Migration `0021_knowledge_scope`
backfills only `organisation_id` on legacy rows (there has only ever been
the one default organisation); it never guesses a scope, because legacy
Telegram content may not be the Admin's own.

Deliberately: no default and no server default on any of the three columns,
so a row is never attributed by accident (a structural test pins this); no
foreign keys and no NOT NULL yet -- those are enforcement, after orphan
checks, in a later migration.

This lives in the database layer, not in `app.identity`, so that every
model package -- including execution, which must never import identity --
can use it. `app.identity.scope.KnowledgeScope` is the trusted value the
later units will write from; its visibilities must equal
`KNOWLEDGE_SCOPE_VISIBILITIES` (a test checks).
"""

import uuid
from typing import Optional, Tuple

from sqlalchemy import CheckConstraint, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

#: The only visibilities a scoped row may carry.
KNOWLEDGE_SCOPE_VISIBILITIES = ("personal", "organisation")

#: The CHECK's name suffix; the naming convention makes it
#: `ck_<table>_knowledge_scope_valid`.
KNOWLEDGE_SCOPE_CHECK_NAME = "knowledge_scope_valid"

#: Either no scope at all, or a complete and consistent one: a personal scope
#: is not the organisation's own id, a shared scope is exactly that id. Written
#: so no branch can evaluate to NULL -- a NULL CHECK result would pass.
KNOWLEDGE_SCOPE_CHECK_SQL = (
    "(scope_id IS NULL AND scope_visibility IS NULL)"
    " OR (scope_id IS NOT NULL AND organisation_id IS NOT NULL"
    " AND scope_visibility IS NOT NULL"
    " AND ((scope_visibility = 'personal' AND scope_id <> organisation_id)"
    " OR (scope_visibility = 'organisation' AND scope_id = organisation_id)))"
)


class KnowledgeScopeColumns:
    """Mixin: the three knowledge-scope columns. See the module docstring."""

    organisation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    scope_visibility: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)


def knowledge_scope_check() -> CheckConstraint:
    """A fresh CHECK for one table's `__table_args__` (one object per table)."""
    return CheckConstraint(KNOWLEDGE_SCOPE_CHECK_SQL, name=KNOWLEDGE_SCOPE_CHECK_NAME)


def scoped_unique_indexes(table: str, stem: str, *columns: str) -> Tuple[Index, Index]:
    """Uniqueness per knowledge scope (Stage 7B, migration 0022).

    Replaces a global unique index, which let one member's row collide with
    another's: `(scope_id, *columns)` unique among attributed rows, and
    `(*columns)` unique among unattributed (legacy) rows -- the old guarantee,
    kept for them alone. Both partial, so no NOT NULL is needed."""
    attributed = text("scope_id IS NOT NULL")
    unattributed = text("scope_id IS NULL")
    return (
        Index(f"uq_{table}_scoped_{stem}", "scope_id", *columns, unique=True,
              sqlite_where=attributed, postgresql_where=attributed),
        Index(f"uq_{table}_legacy_{stem}", *columns, unique=True,
              sqlite_where=unattributed, postgresql_where=unattributed),
    )


__all__ = [
    "KNOWLEDGE_SCOPE_CHECK_NAME",
    "KNOWLEDGE_SCOPE_CHECK_SQL",
    "KNOWLEDGE_SCOPE_VISIBILITIES",
    "KnowledgeScopeColumns",
    "knowledge_scope_check",
    "scoped_unique_indexes",
]
