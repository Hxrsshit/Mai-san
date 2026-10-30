"""Stage 7A: organisation and identity tables. Frozen interface, owned by the lead.

The shape is multi-tenant-ready (an organisation has members; a person can
hold an identity), but Stage 7A runs one default organisation and does not
make any existing table organisation-aware. Tenant isolation is not claimed.

Enumerated columns are plain strings with CHECK constraints (no database enum
types), so a downgrade leaves nothing behind. Every constraint and index name
here matches migration `0020_organisation_identity` exactly; a test compares
the migrated schema with this metadata.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow
from app.identity.constants import (
    MAX_DISPLAY_NAME_CHARS,
    MAX_ORGANISATION_NAME_CHARS,
    MAX_REVOKED_REASON_CHARS,
    MAX_SUBJECT_CHARS,
)

#: An attached identity is one with no `detached_at`. Uniqueness holds only
#: among attached identities, so a revoked member's identity can be linked
#: again later to a new membership.
ATTACHED = text("detached_at IS NULL")


class Organisation(Base):
    __tablename__ = "organisations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(MAX_ORGANISATION_NAME_CHARS), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
    )


class User(Base):
    """A person. Holds identities; belongs to organisations through members."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(MAX_DISPLAY_NAME_CHARS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow,
        server_default=func.now(),
    )


class OrganisationMember(Base):
    """A person's membership of one organisation. Its id is the principal.

    The default organisation's Admin member has the configured owner's id, so
    every existing `owner_id` already names a member.
    """

    __tablename__ = "organisation_members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organisations.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(MAX_REVOKED_REASON_CHARS), nullable=True)

    __table_args__ = (
        UniqueConstraint("organisation_id", "user_id"),
        Index("ix_organisation_members_organisation_id_status", "organisation_id", "status"),
        CheckConstraint("role IN ('admin', 'member')", name="role_valid"),
        CheckConstraint("status IN ('invited', 'active', 'revoked')", name="status_valid"),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL)"
            " OR (status <> 'revoked' AND revoked_at IS NULL)",
            name="revoked_iff_revoked_at",
        ),
        CheckConstraint(
            "status <> 'active' OR activated_at IS NOT NULL",
            name="active_has_activated_at",
        ),
    )


class UserIdentity(Base):
    """An external account (a Telegram user id) attached to a person."""

    __tablename__ = "user_identities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The provider's account id as decimal text. Never logged or returned.
    subject: Mapped[str] = mapped_column(String(MAX_SUBJECT_CHARS), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    detached_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("provider IN ('telegram')", name="provider_valid"),
        CheckConstraint("length(subject) > 0", name="subject_not_empty"),
        Index(
            "uq_user_identities_provider_subject_attached", "provider", "subject",
            unique=True, sqlite_where=ATTACHED, postgresql_where=ATTACHED,
        ),
        Index(
            "uq_user_identities_user_id_provider_attached", "user_id", "provider",
            unique=True, sqlite_where=ATTACHED, postgresql_where=ATTACHED,
        ),
    )


class LinkCode(Base):
    """A single-use, short-lived code that links a Telegram account to a member.

    Only the SHA-256 of the code is stored. The plaintext exists once, in the
    response that issued it.
    """

    __tablename__ = "identity_link_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organisation_members.id", ondelete="RESTRICT"), nullable=False
    )
    code_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("code_sha256"),
        Index("ix_identity_link_codes_member_id", "member_id"),
        CheckConstraint("length(code_sha256) = 64", name="hash_length"),
        CheckConstraint(
            "consumed_at IS NULL OR invalidated_at IS NULL",
            name="not_consumed_and_invalidated",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )


__all__ = [
    "LinkCode",
    "Organisation",
    "OrganisationMember",
    "User",
    "UserIdentity",
]
