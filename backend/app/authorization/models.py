"""Stage 6E: standing approval grants.

A standing grant is a persisted, scoped, expiring, revocable record that a
person has decided a *class* of action may proceed without being asked again
each time.

### What it is not

It is not permission. It is the absence of one specific question.

Everything else still happens: the capability is resolved, its availability
checked, its arguments validated against its own model, the policy in
`app.tools.policy` re-evaluated, the approval fingerprint bound to the exact
payload, and the dispatcher's gates re-asked at run time. A grant removes the
human prompt and nothing else.

### Scope is the tool name, and there are no wildcards

`capability` holds the registry's canonical tool name, exactly. That gives
capability *and* action scope for free, because the registry already
distinguishes them: `gmail_list_messages` and `gmail_get_message` are
different names, and there is no `gmail_send_message` to grant at all.

There is deliberately no prefix, pattern or category matching. The
specification asks that a grant never silently widen because a future
capability shares a prefix, and the simplest way to guarantee that is to have
no mechanism that could. Matching is string equality.

### Risk is recorded and compared, not assumed

`risk_level` is the risk the capability carried when the grant was made. A
grant satisfies an action only when the capability's risk *now* is no higher
than that. So a tool whose risk is raised later stops being covered by grants
taken when it was safer, without anyone having to remember to revoke them.

### CRITICAL is refused twice

`app.tools.policy.MAX_PERMITTED_RISK` is `HIGH`, so a critical capability is
`FORBIDDEN` before grants are ever consulted -- the grant path only ever sees
`APPROVAL_REQUIRED`. Creation refuses a critical grant as well, so the record
cannot exist to be misread later. Neither is a switch.
"""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, utcnow
from app.tools.schemas import RiskLevel


def _enum_values(enum_cls) -> list:
    return [member.value for member in enum_cls]


#: Longest capability name a grant may name. Matches the tool registry's own
#: bound, asserted equal by a test.
MAX_CAPABILITY_CHARS = 64
MAX_REASON_CHARS = 64

#: Longest a grant may last, in seconds. Seven days.
#:
#: A bound rather than a default: a grant that never expires is a standing
#: permission with no end, which is the thing this stage was told not to
#: build. Creation refuses anything longer.
MAX_GRANT_TTL_SECONDS = 7 * 24 * 60 * 60
#: What a caller gets if they name no expiry. One day.
DEFAULT_GRANT_TTL_SECONDS = 24 * 60 * 60


class GrantStatus(str, enum.Enum):
    """Where a grant is. Derived from timestamps, never stored.

    Stored as columns rather than a status field so there is one source of
    truth per fact: `revoked_at` says whether and when, and a status column
    beside it could disagree with it.
    """

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ApprovalGrant(Base):
    """One standing approval, as a person granted it."""

    __tablename__ = "approval_grants"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    #: Whose grant this is. The same identity tasks carry.
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    #: The registry's canonical tool name. Exact match, no wildcards.
    capability: Mapped[str] = mapped_column(
        String(MAX_CAPABILITY_CHARS), nullable=False
    )

    #: The risk the capability carried when this was granted.
    risk_level: Mapped[RiskLevel] = mapped_column(
        Enum(RiskLevel, name="execution_risk_level", values_callable=_enum_values),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow,
        server_default=func.now(),
    )
    #: When it stops working. Always set: see `MAX_GRANT_TTL_SECONDS`.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: When a person withdrew it, if they did. Revocation never deletes the
    #: row: the record of what was permitted, and for how long, is the audit.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: An application reason code. Never model text.
    revoked_reason: Mapped[Optional[str]] = mapped_column(
        String(MAX_REASON_CHARS), nullable=True
    )

    __table_args__ = (
        # Every lookup is "this owner, this capability, still valid".
        Index("ix_approval_grants_owner_capability", "owner_id", "capability"),
        Index("ix_approval_grants_expires_at", "expires_at"),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_reason IS NULL)"
            " OR (revoked_at IS NOT NULL)",
            name="revoked_reason_needs_revocation",
        ),
    )

    def status(self, now: Optional[datetime] = None) -> GrantStatus:
        """Derived, never stored. Revocation wins over expiry."""
        from datetime import timezone as _tz

        if self.revoked_at is not None:
            return GrantStatus.REVOKED
        moment = now or datetime.now(_tz.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            # SQLite returns naive values; reading one as machine-local would
            # expire a fresh grant or revive a stale one.
            expires = expires.replace(tzinfo=_tz.utc)
        # At the boundary a grant is expired: `now == expires_at` is the
        # moment it stops, not the last moment it works.
        return GrantStatus.EXPIRED if moment >= expires else GrantStatus.ACTIVE

    def is_active(self, now: Optional[datetime] = None) -> bool:
        return self.status(now) is GrantStatus.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApprovalGrant {self.capability} {self.status().value}>"


__all__ = [
    "DEFAULT_GRANT_TTL_SECONDS",
    "MAX_CAPABILITY_CHARS",
    "MAX_GRANT_TTL_SECONDS",
    "ApprovalGrant",
    "GrantStatus",
]
