"""Stage 7B (L0): the trusted knowledge scope.

A `KnowledgeScope` says whose knowledge something is: which organisation,
which scope inside it, and whether it is personal (one member's) or shared
(the organisation's). It is a frozen value, validated on construction, and
it is to be made only by `KnowledgeScope.personal(principal)` /
`.organisation(principal)` from an authenticated `Principal` -- never from a
request value, a Telegram payload or model output. Python cannot enforce
that (the constructor is public); it is a repository rule, checked by
`tests/security/test_scope_construction_structure.py`. Later Stage 7B units
take it at service construction and write it to the scope columns
(`app.database.models.scope`).

Stage 7B L0 policy:

* **Personal is the only writable visibility.** The organisation scope exists
  in the type so the design is complete, but `write_columns` refuses it:
  shared-memory writes are not enabled, and nothing in L0 writes a scope.
* No scope means unattributed. There is deliberately no "default scope".

Since unit 4 the scoped services read with `clause(model)` and write with
`write_columns()`: those two methods are the only way application code names
the scope columns (`tests/security/test_knowledge_scope_structure.py`).
"""

import enum
import uuid
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional

from sqlalchemy import ColumnElement, and_

from app.database.models.scope import KNOWLEDGE_SCOPE_VISIBILITIES
from app.identity.principal import Principal

_NIL = uuid.UUID(int=0)


class ScopeVisibility(str, enum.Enum):
    PERSONAL = "personal"
    #: Reserved: shared organisational knowledge. Not writable in Stage 7B L0.
    ORGANISATION = "organisation"


#: What may be written. Widening this enables shared-memory writes, which
#: needs the owner's approval (a structural test pins the set).
WRITABLE_VISIBILITIES: FrozenSet[ScopeVisibility] = frozenset({ScopeVisibility.PERSONAL})


class InvalidScope(ValueError):
    """A scope that is incomplete, inconsistent or of the wrong type."""


class ScopeNotWritable(Exception):
    """A valid scope whose visibility may not be written (yet)."""


def _uuid(name: str, value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidScope(f"{name} must be a UUID")
    if value == _NIL:
        raise InvalidScope(f"{name} must not be the nil UUID")
    return value


@dataclass(frozen=True)
class KnowledgeScope:
    organisation_id: uuid.UUID
    scope_id: uuid.UUID
    visibility: ScopeVisibility

    def __post_init__(self) -> None:
        _uuid("organisation_id", self.organisation_id)
        _uuid("scope_id", self.scope_id)
        if not isinstance(self.visibility, ScopeVisibility):
            raise InvalidScope("visibility must be a ScopeVisibility")
        if self.visibility is ScopeVisibility.PERSONAL and self.scope_id == self.organisation_id:
            raise InvalidScope("a personal scope is a member's, not the organisation's")
        if self.visibility is ScopeVisibility.ORGANISATION and self.scope_id != self.organisation_id:
            raise InvalidScope("an organisation scope is the organisation's own id")

    @classmethod
    def personal(cls, principal: Principal) -> "KnowledgeScope":
        """The authenticated member's own personal scope."""
        _principal(principal)
        return cls(principal.organisation_id, principal.member_id, ScopeVisibility.PERSONAL)

    @classmethod
    def organisation(cls, principal: Principal) -> "KnowledgeScope":
        """The member's organisation's shared scope. Reserved: valid to name,
        refused by `write_columns` in Stage 7B L0."""
        _principal(principal)
        return cls(
            principal.organisation_id, principal.organisation_id, ScopeVisibility.ORGANISATION
        )

    def clause(self, model) -> ColumnElement[bool]:
        """The read filter for a scoped `model`: its rows in this scope only.

        All three columns, so an unattributed (legacy, quarantined) row --
        NULL scope -- never matches, and neither does a row of another
        organisation or another member. Stage 7B units 4-10 read only
        personal scopes (`docs/stage7b_ownership_design.md`, section 2); the
        organisation scope is refused here until shared reads are approved.
        The only way a scoped query should name the scope columns."""
        if self.visibility not in WRITABLE_VISIBILITIES:
            raise ScopeNotWritable(f"{self.visibility.value} scope is not readable yet")
        return and_(
            model.organisation_id == self.organisation_id,
            model.scope_id == self.scope_id,
            model.scope_visibility == self.visibility.value,
        )

    def write_columns(self) -> Dict[str, object]:
        """The scope-column values for a row being written -- the only way a
        scope should reach a model. Refuses a visibility that is not writable."""
        if self.visibility not in WRITABLE_VISIBILITIES:
            raise ScopeNotWritable(f"{self.visibility.value} scope is not writable")
        return {
            "organisation_id": self.organisation_id,
            "scope_id": self.scope_id,
            "scope_visibility": self.visibility.value,
        }


def _principal(principal: Optional[Principal]) -> None:
    if not isinstance(principal, Principal):
        raise InvalidScope("a scope is derived from an authenticated Principal only")


# The column CHECK and this type must agree on the visibilities. An explicit
# check, not `assert`, so it also holds under `python -O`.
if tuple(v.value for v in ScopeVisibility) != KNOWLEDGE_SCOPE_VISIBILITIES:  # pragma: no cover
    raise RuntimeError("ScopeVisibility and the scope-column CHECK disagree")

__all__ = [
    "InvalidScope",
    "KnowledgeScope",
    "ScopeNotWritable",
    "ScopeVisibility",
    "WRITABLE_VISIBILITIES",
]
