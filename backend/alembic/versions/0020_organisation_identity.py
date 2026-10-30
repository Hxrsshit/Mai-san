"""Stage 7A: organisations, users, members, linked identities and link codes.

Five new tables; no existing table, column or constraint changes. Enumerated
columns are strings with CHECK constraints, so no database type is created
and none survives a downgrade.

* `organisations`, `users`, `organisation_members`: a member is one person's
  membership of one organisation, with a role (`admin` | `member`) and a
  status (`invited` | `active` | `revoked`). Checks tie `revoked` to
  `revoked_at` and `active` to `activated_at`.
* `user_identities`: an external account (a Telegram user id, as text)
  attached to a person. Two **partial** unique indexes hold only among
  attached identities (`detached_at IS NULL`), so a revoked member's
  identity is detached rather than deleted and can be linked again later.
* `identity_link_codes`: single-use, expiring link codes, stored only as the
  SHA-256 of the code.

**Seed.** One default organisation, its Admin user, and the Admin member
whose id is the configured owner (`00000000-0000-0000-0000-00000000a1a1`),
so every existing owner-scoped row already belongs to that Admin. The rows
are inserted through typed columns (`sa.Uuid`), so each dialect stores the
ids in the form the application reads back; a raw string insert would not
match on SQLite. The values must equal `app.identity.constants`; a test
checks that.

**Downgrade** drops the five tables. It irreversibly destroys every member,
linked identity and link code created after the upgrade.
"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORGANISATION_ID = uuid.UUID("00000000-0000-0000-0000-00000000b1b1")
DEFAULT_ORGANISATION_NAME = "Default organisation"
LEGACY_ADMIN_USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000c1c1")
LEGACY_ADMIN_DISPLAY_NAME = "Admin"
LEGACY_ADMIN_MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")

ATTACHED = "detached_at IS NULL"


def _timestamps(*names: str):
    return [
        sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
        for name in names
    ]


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps("created_at", "updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisations")),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name=op.f("ck_organisations_status_valid")
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        *_timestamps("created_at", "updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_table(
        "organisation_members",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("organisation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps("created_at"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisation_members")),
        sa.ForeignKeyConstraint(
            ["organisation_id"], ["organisations.id"], ondelete="RESTRICT",
            name=op.f("fk_organisation_members_organisation_id_organisations"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT",
            name=op.f("fk_organisation_members_user_id_users"),
        ),
        sa.UniqueConstraint(
            "organisation_id", "user_id",
            name=op.f("uq_organisation_members_organisation_id_user_id"),
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'member')", name=op.f("ck_organisation_members_role_valid")
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'revoked')",
            name=op.f("ck_organisation_members_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL)"
            " OR (status <> 'revoked' AND revoked_at IS NULL)",
            name=op.f("ck_organisation_members_revoked_iff_revoked_at"),
        ),
        sa.CheckConstraint(
            "status <> 'active' OR activated_at IS NOT NULL",
            name=op.f("ck_organisation_members_active_has_activated_at"),
        ),
    )
    op.create_index(
        "ix_organisation_members_organisation_id_status",
        "organisation_members", ["organisation_id", "status"],
    )
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=20), nullable=False),
        *_timestamps("linked_at"),
        sa.Column("detached_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_identities")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT",
            name=op.f("fk_user_identities_user_id_users"),
        ),
        sa.CheckConstraint(
            "provider IN ('telegram')", name=op.f("ck_user_identities_provider_valid")
        ),
        sa.CheckConstraint(
            "length(subject) > 0", name=op.f("ck_user_identities_subject_not_empty")
        ),
    )
    op.create_index(
        "uq_user_identities_provider_subject_attached",
        "user_identities", ["provider", "subject"], unique=True,
        sqlite_where=sa.text(ATTACHED), postgresql_where=sa.text(ATTACHED),
    )
    op.create_index(
        "uq_user_identities_user_id_provider_attached",
        "user_identities", ["user_id", "provider"], unique=True,
        sqlite_where=sa.text(ATTACHED), postgresql_where=sa.text(ATTACHED),
    )
    op.create_table(
        "identity_link_codes",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("member_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        *_timestamps("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_link_codes")),
        sa.ForeignKeyConstraint(
            ["member_id"], ["organisation_members.id"], ondelete="RESTRICT",
            name=op.f("fk_identity_link_codes_member_id_organisation_members"),
        ),
        sa.UniqueConstraint("code_sha256", name=op.f("uq_identity_link_codes_code_sha256")),
        sa.CheckConstraint(
            "length(code_sha256) = 64", name=op.f("ck_identity_link_codes_hash_length")
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR invalidated_at IS NULL",
            name=op.f("ck_identity_link_codes_not_consumed_and_invalidated"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_identity_link_codes_expires_after_created")
        ),
    )
    op.create_index(
        "ix_identity_link_codes_member_id", "identity_link_codes", ["member_id"]
    )

    _seed()


def _seed() -> None:
    now = datetime.now(timezone.utc)
    organisations = sa.table(
        "organisations",
        sa.column("id", sa.Uuid(as_uuid=True)), sa.column("name", sa.String),
        sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    users = sa.table(
        "users",
        sa.column("id", sa.Uuid(as_uuid=True)), sa.column("display_name", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    members = sa.table(
        "organisation_members",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("organisation_id", sa.Uuid(as_uuid=True)),
        sa.column("user_id", sa.Uuid(as_uuid=True)),
        sa.column("role", sa.String), sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("activated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(organisations, [{
        "id": DEFAULT_ORGANISATION_ID, "name": DEFAULT_ORGANISATION_NAME,
        "status": "active", "created_at": now, "updated_at": now,
    }])
    op.bulk_insert(users, [{
        "id": LEGACY_ADMIN_USER_ID, "display_name": LEGACY_ADMIN_DISPLAY_NAME,
        "created_at": now, "updated_at": now,
    }])
    op.bulk_insert(members, [{
        "id": LEGACY_ADMIN_MEMBER_ID, "organisation_id": DEFAULT_ORGANISATION_ID,
        "user_id": LEGACY_ADMIN_USER_ID, "role": "admin", "status": "active",
        "created_at": now, "activated_at": now,
    }])


def downgrade() -> None:
    op.drop_index("ix_identity_link_codes_member_id", table_name="identity_link_codes")
    op.drop_table("identity_link_codes")
    op.drop_index("uq_user_identities_user_id_provider_attached", table_name="user_identities")
    op.drop_index("uq_user_identities_provider_subject_attached", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_index(
        "ix_organisation_members_organisation_id_status", table_name="organisation_members"
    )
    op.drop_table("organisation_members")
    op.drop_table("users")
    op.drop_table("organisations")
