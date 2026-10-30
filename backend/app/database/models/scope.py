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
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Tuple, runtime_checkable

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
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


def same_scope_parent_key(table: str) -> UniqueConstraint:
    """`UNIQUE (id, organisation_id, scope_id)` on a scoped parent (Stage 7B
    unit 10, migration 0025): what a same-scope reference points at."""
    return UniqueConstraint("id", "organisation_id", "scope_id", name=f"uq_{table}_id_scope")


def same_scope_reference(table: str, column: str, parent: str) -> ForeignKeyConstraint:
    """A reference from a scoped row to a scoped parent of the *same* scope,
    made relational (Stage 7B unit 10; design 6a).

    Added beside the column's existing foreign key, which keeps its CASCADE /
    SET NULL action: this one has none (NO ACTION, checked at the end of the
    statement), so a SET NULL leaves the scope columns alone -- a composite
    key with a NULL component is not checked (MATCH SIMPLE), and neither is a
    legacy row with no scope."""
    return ForeignKeyConstraint(
        [column, "organisation_id", "scope_id"],
        [f"{parent}.id", f"{parent}.organisation_id", f"{parent}.scope_id"],
        name=f"fk_{table}_{column}_same_scope",
    )


@runtime_checkable
class ScopeBinding(Protocol):
    """What a layer that may not import `app.identity` needs of a trusted
    scope (Stage 7B unit 8b): execution, tasks, authorization and the
    background runtime are kept free of identity by a Stage 7A structure
    test, yet their scoped rows must be filtered and written exactly as
    everyone else's.

    `app.identity.scope.KnowledgeScope` satisfies this structurally; neither
    module imports the other. A binding is only ever *received* -- from the
    composition root, built there from an authenticated, active `Principal`
    -- never constructed in these layers (a structure test pins that no class
    there defines `write_columns`; the scope-write tripwire catches any other
    way of naming the scope columns)."""

    @property
    def member_id(self) -> uuid.UUID: ...

    def clause(self, model) -> Any: ...

    def write_columns(self) -> Dict[str, object]: ...

    def owns(self, row: object) -> bool: ...


def require_binding(value: object, owner: str) -> "ScopeBinding":
    """`value` itself if it is a scope binding; otherwise `TypeError`. The
    identity-free counterpart of `app.identity.scope.require_scope`: a
    missing scope fails closed at construction."""
    if value is None or not isinstance(value, ScopeBinding) or isinstance(value, (dict, str)):
        raise TypeError(f"{owner} needs a scope binding")
    return value


#: The composition root's member -> scope resolver (Stage 7B unit 8b). Set
#: once by `app.main.create_app` to `app.identity.scope.scope_for_member`;
#: until then every resolution is None, so work that needs a scope is
#: refused rather than run unscoped.
_member_scope_resolver: Optional[
    Callable[[Any, uuid.UUID], Awaitable[Optional["ScopeBinding"]]]
] = None


def register_member_scope_resolver(
    resolver: Callable[[Any, uuid.UUID], Awaitable[Optional["ScopeBinding"]]],
) -> None:
    """Install the resolver. Called by the composition root only (a structure
    test pins the one call site and forbids any other write). Installing the
    same resolver again is a no-op; a different one is refused."""
    global _member_scope_resolver
    if _member_scope_resolver is not None and _member_scope_resolver is not resolver:
        raise RuntimeError("a different member scope resolver is already registered")
    _member_scope_resolver = resolver


async def resolve_member_scope(session, member_id: uuid.UUID) -> Optional["ScopeBinding"]:
    """The scope of `member_id` if they are an active member, re-read now in
    `session` (under a share lock, so revocation serializes with the work), or
    None -- also when no resolver is installed. For layers that know only an
    owner id (tasks, the runner) and may not import `app.identity`."""
    if _member_scope_resolver is None or member_id is None:
        return None
    binding = await _member_scope_resolver(session, member_id)
    # Belt and braces: a binding is accepted only for the member asked about.
    if binding is None or not isinstance(binding, ScopeBinding) or binding.member_id != member_id:
        return None
    return binding


__all__ = [
    "same_scope_parent_key",
    "same_scope_reference",
    "ScopeBinding",
    "register_member_scope_resolver",
    "require_binding",
    "resolve_member_scope",
    "KNOWLEDGE_SCOPE_CHECK_NAME",
    "KNOWLEDGE_SCOPE_CHECK_SQL",
    "KNOWLEDGE_SCOPE_VISIBILITIES",
    "KnowledgeScopeColumns",
    "knowledge_scope_check",
    "scoped_unique_indexes",
]
