"""Stage 7B L0: `KnowledgeScope` is a validated, frozen, principal-derived value."""

import dataclasses
import uuid

import pytest

from app.identity.constants import DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID, Role
from app.identity.principal import Principal
from app.identity.scope import (
    WRITABLE_VISIBILITIES,
    InvalidScope,
    KnowledgeScope,
    ScopeNotWritable,
    ScopeVisibility,
)

ORG = DEFAULT_ORGANISATION_ID
MEMBER = LEGACY_ADMIN_MEMBER_ID
PRINCIPAL = Principal(member_id=MEMBER, organisation_id=ORG, role=Role.ADMIN)


def test_a_personal_scope_is_the_members_own() -> None:
    scope = KnowledgeScope.personal(PRINCIPAL)
    assert (scope.organisation_id, scope.scope_id, scope.visibility) == (
        ORG, MEMBER, ScopeVisibility.PERSONAL)
    assert scope.write_columns() == {
        "organisation_id": ORG, "scope_id": MEMBER, "scope_visibility": "personal"}


def test_the_organisation_scope_is_reserved_and_not_writable() -> None:
    scope = KnowledgeScope.organisation(PRINCIPAL)
    assert scope.scope_id == scope.organisation_id == ORG
    with pytest.raises(ScopeNotWritable):
        scope.write_columns()
    assert WRITABLE_VISIBILITIES == frozenset({ScopeVisibility.PERSONAL})


def test_the_scope_is_frozen() -> None:
    scope = KnowledgeScope.personal(PRINCIPAL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.scope_id = uuid.uuid4()  # type: ignore[misc]


@pytest.mark.parametrize("organisation_id, scope_id, visibility", [
    (None, MEMBER, ScopeVisibility.PERSONAL),                    # missing organisation
    (ORG, None, ScopeVisibility.PERSONAL),                       # missing scope
    (ORG, MEMBER, None),                                         # missing visibility
    (str(ORG), MEMBER, ScopeVisibility.PERSONAL),                # a string is not a UUID
    (ORG, str(MEMBER), ScopeVisibility.PERSONAL),
    (ORG.int, MEMBER, ScopeVisibility.PERSONAL),                 # nor is an int
    (uuid.UUID(int=0), MEMBER, ScopeVisibility.PERSONAL),        # nil UUIDs
    (ORG, uuid.UUID(int=0), ScopeVisibility.PERSONAL),
    (ORG, MEMBER, "personal"),                                   # a plain string visibility
    (ORG, ORG, ScopeVisibility.PERSONAL),                        # personal == organisation id
    (ORG, MEMBER, ScopeVisibility.ORGANISATION),                 # shared != organisation id
])
def test_invalid_or_missing_identifiers_are_refused(organisation_id, scope_id, visibility) -> None:
    with pytest.raises(InvalidScope):
        KnowledgeScope(organisation_id, scope_id, visibility)


@pytest.mark.parametrize("not_a_principal", [None, "member", MEMBER, {"member_id": MEMBER}])
def test_a_scope_is_derived_only_from_a_principal(not_a_principal) -> None:
    with pytest.raises(InvalidScope):
        KnowledgeScope.personal(not_a_principal)
    with pytest.raises(InvalidScope):
        KnowledgeScope.organisation(not_a_principal)
