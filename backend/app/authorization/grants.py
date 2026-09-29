"""Creating, finding and revoking standing approval grants.

Persistence only. **This module makes no authorization decision** -- it stores
what a person granted and answers "is there a live grant for this capability
and owner?". Whether that grant is sufficient is decided in
`app.tools.authorization`, which remains the one place any authorization
answer comes from.

The split matters. If this module returned "authorized", there would be two
answers to the same question and a reader would have to know which one won.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.authorization.models import (
    DEFAULT_GRANT_TTL_SECONDS,
    MAX_CAPABILITY_CHARS,
    MAX_GRANT_TTL_SECONDS,
    MAX_REASON_CHARS,
    ApprovalGrant,
    GrantStatus,
)
from app.core.logging import get_logger
from app.tools.schemas import RiskLevel, risk_rank

logger = get_logger(__name__)

_DB_ERRORS = (SQLAlchemyError, OSError)

#: Most grants one listing returns.
MAX_LISTED = 100

#: The risk a standing grant may never cover.
#:
#: Belt and braces. `app.tools.policy` already refuses a critical capability
#: outright, so the grant path never sees one -- but a *record* saying a
#: person granted standing approval for a critical action would be a
#: dangerous thing for a later reader to find, whether or not anything
#: honoured it.
REFUSED_RISK: RiskLevel = RiskLevel.CRITICAL


class GrantRefused(Exception):
    """A grant that may not be created. `reason` is an application constant."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GrantService:
    """Standing grants for one owner. Stores; never decides."""

    def __init__(self, session: AsyncSession, owner_id: uuid.UUID) -> None:
        self._session = session
        self._owner_id = owner_id

    @property
    def owner_id(self) -> uuid.UUID:
        return self._owner_id

    async def create(
        self,
        capability: str,
        risk_level: RiskLevel,
        ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
        now: Optional[datetime] = None,
    ) -> ApprovalGrant:
        """Record a person's decision. Raises `GrantRefused` if it may not be.

        There is deliberately no `source` parameter and no way to say who or
        what asked for this. The only caller is an explicit user-originated
        operation; a model saying "the user wants me to always allow this" is
        not one, and a structural test asserts that no content-handling
        module can reach this method.
        """
        name = " ".join((capability or "").split()).strip().lower()
        if not name:
            raise GrantRefused("empty_capability")
        if len(name) > MAX_CAPABILITY_CHARS:
            raise GrantRefused("capability_too_long")
        # No wildcards. There is no pattern language here to abuse, and a
        # name carrying one is a caller expecting a mechanism that does not
        # exist -- better refused than silently treated as a literal.
        if any(character in name for character in "*?%[]"):
            raise GrantRefused("wildcards_not_supported")

        from app.tools.registry import get_registry

        registry = get_registry()
        canonical = registry.canonical(name)
        definition = registry.definition(canonical)
        if definition is None:
            # A grant for something that does not exist would sit in the
            # table waiting for a future capability to adopt that name.
            raise GrantRefused("unknown_capability")

        if risk_rank(risk_level) >= risk_rank(REFUSED_RISK):
            raise GrantRefused("critical_risk_not_grantable")
        if risk_rank(definition.risk_level) > risk_rank(risk_level):
            # Granting below the capability's own risk would create a record
            # that can never satisfy anything -- confusing rather than safe.
            raise GrantRefused("risk_below_capability")

        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            raise GrantRefused("invalid_ttl")
        if ttl_seconds < 1 or ttl_seconds > MAX_GRANT_TTL_SECONDS:
            raise GrantRefused("ttl_out_of_range")

        moment = now or datetime.now(timezone.utc)
        grant = ApprovalGrant(
            owner_id=self._owner_id,
            capability=canonical,
            risk_level=definition.risk_level,
            created_at=moment,
            expires_at=moment + timedelta(seconds=ttl_seconds),
        )
        self._session.add(grant)
        await self._session.flush()

        logger.info(
            "Standing grant created",
            # A capability name, a risk and a duration. All application
            # constants; no argument, no objective, no user text.
            extra={
                "grant_id": str(grant.id),
                "capability": canonical,
                "risk": definition.risk_level.value,
                "ttl_seconds": ttl_seconds,
            },
        )
        return grant

    async def active_for(
        self, capability: str, now: Optional[datetime] = None
    ) -> Optional[ApprovalGrant]:
        """The live grant covering this capability for this owner, or None.

        Owner and capability are filtered in the query, so there is no branch
        where another owner's row is loaded and then rejected. Expiry and
        revocation are evaluated in Python against an injected `now`, which
        is what makes the boundary testable without a clock.
        """
        if not capability:
            return None
        try:
            rows = (
                await self._session.execute(
                    select(ApprovalGrant).where(
                        ApprovalGrant.owner_id == self._owner_id,
                        ApprovalGrant.capability == capability,
                    ).order_by(ApprovalGrant.expires_at.desc())
                )
            ).scalars().all()
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad, and the one place in this module where that
            # is right. A grant that cannot be read is a grant that does not
            # apply, so every failure mode -- a database error, a driver
            # surprise, anything -- must end in "ask the user" rather than in
            # an exception whose handling somewhere else might not.
            logger.error("Could not read standing grants", extra={"error": str(exc)})
            return None

        for grant in rows:
            if grant.is_active(now):
                # The longest-lived live grant. Duplicates are permitted and
                # cannot conflict: every one of them says the same thing, so
                # picking any is the same decision.
                return grant
        return None

    async def revoke(
        self,
        grant_id: uuid.UUID,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[ApprovalGrant]:
        """Withdraw a grant. Immediate, persisted, and never a delete.

        Conditional on the row still being unrevoked, so two concurrent
        revocations settle on the first one's timestamp rather than the
        second quietly overwriting it.
        """
        moment = now or datetime.now(timezone.utc)
        try:
            result = await self._session.execute(
                update(ApprovalGrant)
                .where(
                    ApprovalGrant.id == grant_id,
                    ApprovalGrant.owner_id == self._owner_id,
                    ApprovalGrant.revoked_at.is_(None),
                )
                .values(
                    revoked_at=moment,
                    revoked_reason=(reason or "revoked")[:MAX_REASON_CHARS],
                )
                .execution_options(synchronize_session="fetch")
            )
        except _DB_ERRORS as exc:
            logger.error("Could not revoke a grant", extra={"error": str(exc)})
            return None

        grant = await self.get(grant_id)
        if result.rowcount == 1:
            logger.info("Standing grant revoked", extra={"grant_id": str(grant_id)})
        return grant

    async def get(self, grant_id: uuid.UUID) -> Optional[ApprovalGrant]:
        try:
            return (
                await self._session.execute(
                    select(ApprovalGrant).where(
                        ApprovalGrant.id == grant_id,
                        ApprovalGrant.owner_id == self._owner_id,
                    )
                )
            ).scalars().first()
        except _DB_ERRORS:
            return None

    async def list_grants(
        self, limit: int = MAX_LISTED
    ) -> List[ApprovalGrant]:
        """This owner's grants, newest first. Includes revoked and expired:
        the history is the audit."""
        try:
            rows = (
                await self._session.execute(
                    select(ApprovalGrant)
                    .where(ApprovalGrant.owner_id == self._owner_id)
                    .order_by(ApprovalGrant.created_at.desc())
                    .limit(max(1, min(int(limit), MAX_LISTED)))
                )
            ).scalars().all()
        except _DB_ERRORS:
            return []
        return list(rows)


__all__ = ["MAX_LISTED", "REFUSED_RISK", "GrantRefused", "GrantService"]
