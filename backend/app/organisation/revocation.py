"""Stage 7A: revoking a member, atomically.

`MemberRevocation.revoke` is the whole of a revocation, in one transaction
that it owns and commits exactly once, at the end:

1. `MembershipService.mark_revoked` -- the member's status, open link codes
   and attached identities. Not found, or an Admin: rolled back, answered,
   nothing else touched.
2. Every non-terminal task the member owns is cancelled through the cancel
   service the composition root supplies (`MonitorRequestService.cancel`,
   bound to this member), journalled as `member_revoked`.
3. Every live standing grant the member holds is revoked through
   `GrantService.revoke`, reason `member_revoked`.
4. Postconditions are counted directly in the database: no non-terminal
   task and no live grant may remain.
5. Commit.

Any failure after the membership is read rolls everything back -- the member
stays as it was, active or invited, with its codes, identity, tasks and
grants untouched -- and is reported as `RevocationIncomplete`. A half-revoked
member (membership revoked, work still running) cannot be committed.

Both loops are bounded (`MAX_ROUNDS` x `BATCH_SIZE`); work beyond the bound
fails the postcondition rather than being left behind silently.

The cancel service is built only for the member `mark_revoked` has just
accepted in the caller's organisation, never for a request value. This
module never imports the task service, the runner, the background runtime,
execution or the API; `MonitorRequestService` is named for type checking
only. Log lines carry reason codes, member ids and counts: never a code, a
subject, a display name or an exception's text.
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authorization.grants import GrantService
from app.authorization.models import ApprovalGrant
from app.core.logging import get_logger
from app.identity.schemas import RevokeOutcome
from app.identity.service import MembershipService
from app.tasks.models import Task
from app.tasks.schemas import CancelReason, TaskOutcome, TaskResult
from app.tasks.states import TERMINAL_STATES, TaskState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.tasks.requests import MonitorRequestService

logger = get_logger(__name__)

#: How many batches each loop reads before giving up.
MAX_ROUNDS = 10
#: How many ids each batch reads.
BATCH_SIZE = 50

#: The reason recorded on the membership, the journal and the grants.
REVOKED_REASON = "admin_revoked"
GRANT_REVOKED_REASON = "member_revoked"

#: States a task is finished in, in a stable order for the query.
_TERMINAL = sorted(TERMINAL_STATES, key=lambda state: state.value)

#: Outcomes that mean "this task is now cancelled".
_CANCELLED_OUTCOMES = frozenset({TaskOutcome.UPDATED, TaskOutcome.CANCELLED})


class RevocationIncomplete(Exception):
    """The revocation was rolled back; the member is unchanged.

    `reason` is an application code. The message is the code too: it never
    carries the text of the error that caused it.
    """

    def __init__(self, reason: str = "revocation_incomplete") -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _accepted(result: object) -> bool:
    """Whether a cancel result leaves the task finished."""
    if not isinstance(result, TaskResult):
        return False
    if result.outcome is TaskOutcome.TERMINAL:
        return True
    return result.outcome in _CANCELLED_OUTCOMES and result.state == TaskState.CANCELLED


class MemberRevocation:
    """One member's revocation: membership, work and grants, or nothing."""

    def __init__(
        self,
        session: AsyncSession,
        cancel_service_for: Callable[[uuid.UUID], "MonitorRequestService"],
    ) -> None:
        self._session = session
        self._cancel_service_for = cancel_service_for

    async def revoke(
        self, organisation_id: uuid.UUID, member_id: uuid.UUID
    ) -> RevokeOutcome:
        """Revoke `member_id` of `organisation_id`. Commits once, on success.

        Raises `RevocationIncomplete` after rolling back if any part fails.
        Idempotent: ALREADY_REVOKED still cancels and revokes anything left.
        """
        try:
            outcome = await MembershipService(self._session).mark_revoked(
                organisation_id, member_id, reason=REVOKED_REASON
            )
        except Exception:  # noqa: BLE001 - reported as a code, see _fail
            await self._fail("membership_failed")

        if outcome in (RevokeOutcome.NOT_FOUND, RevokeOutcome.CANNOT_REVOKE_ADMIN):
            # Nothing was written; ending the read must not turn a refusal
            # into a raw database error. The session is discarded with the
            # request either way.
            try:
                await self._session.rollback()
            except Exception:  # noqa: BLE001 - the refusal is the answer
                logger.warning(
                    "Member revocation refusal rollback failed",
                    extra={"reason": "refusal_rollback_failed"},
                )
            return outcome

        # From here on `member_id` is the member `mark_revoked` accepted in
        # this organisation, and the only value the cancel service is built for.
        try:
            moment = _now()
            cancelled = await self._cancel_tasks(member_id)
            revoked = await self._revoke_grants(member_id, moment)
            await self._check_postconditions(member_id, moment)
            await self._session.commit()
        except RevocationIncomplete as exc:
            await self._fail(exc.reason)
        except Exception:  # noqa: BLE001 - reported as a code, see _fail
            await self._fail("unexpected_error")

        logger.info(
            "Member revocation committed",
            extra={
                "member_id": str(member_id),
                "outcome": outcome.value,
                "tasks_cancelled": cancelled,
                "grants_revoked": revoked,
            },
        )
        return outcome

    # --- Steps --------------------------------------------------------------

    async def _cancel_tasks(self, member_id: uuid.UUID) -> int:
        cancelled = 0
        for _ in range(MAX_ROUNDS):
            task_ids = await self._open_task_ids(member_id)
            if not task_ids:
                break
            for task_id in task_ids:
                result = await self._cancel_service_for(member_id).cancel(
                    task_id, reason=CancelReason.MEMBER_REVOKED
                )
                if not _accepted(result):
                    raise RevocationIncomplete("task_not_cancelled")
                cancelled += 1
        return cancelled

    async def _revoke_grants(self, member_id: uuid.UUID, moment: datetime) -> int:
        grants = GrantService(self._session, owner_id=member_id)
        revoked = 0
        for _ in range(MAX_ROUNDS):
            grant_ids = await self._live_grant_ids(member_id, moment)
            if not grant_ids:
                break
            for grant_id in grant_ids:
                grant = await grants.revoke(grant_id, reason=GRANT_REVOKED_REASON)
                if grant is None or grant.revoked_at is None:
                    raise RevocationIncomplete("grant_not_revoked")
                revoked += 1
        return revoked

    async def _check_postconditions(self, member_id: uuid.UUID, moment: datetime) -> None:
        open_tasks = (
            await self._session.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.owner_id == member_id, Task.state.notin_(_TERMINAL))
            )
        ).scalar_one()
        if open_tasks != 0:
            raise RevocationIncomplete("tasks_remain")
        live_grants = (
            await self._session.execute(
                select(func.count())
                .select_from(ApprovalGrant)
                .where(
                    ApprovalGrant.owner_id == member_id,
                    ApprovalGrant.revoked_at.is_(None),
                    ApprovalGrant.expires_at > moment,
                )
            )
        ).scalar_one()
        if live_grants != 0:
            raise RevocationIncomplete("grants_remain")

    # --- Reads --------------------------------------------------------------

    async def _open_task_ids(self, member_id: uuid.UUID) -> List[uuid.UUID]:
        return list(
            (
                await self._session.execute(
                    select(Task.id)
                    .where(Task.owner_id == member_id, Task.state.notin_(_TERMINAL))
                    .order_by(Task.created_at.desc(), Task.id.desc())
                    .limit(BATCH_SIZE)
                )
            ).scalars().all()
        )

    async def _live_grant_ids(
        self, member_id: uuid.UUID, moment: datetime
    ) -> List[uuid.UUID]:
        return list(
            (
                await self._session.execute(
                    select(ApprovalGrant.id)
                    .where(
                        ApprovalGrant.owner_id == member_id,
                        ApprovalGrant.revoked_at.is_(None),
                        ApprovalGrant.expires_at > moment,
                    )
                    .order_by(ApprovalGrant.created_at.desc(), ApprovalGrant.id.desc())
                    .limit(BATCH_SIZE)
                )
            ).scalars().all()
        )

    # --- Failure ------------------------------------------------------------

    async def _fail(self, reason: str) -> None:
        """Roll back, log the code, raise `RevocationIncomplete`. Never returns.

        Deliberately no `exc_info` and no exception text: a driver's message
        can quote bound parameters. A rollback that itself fails does not
        replace the report; the session is discarded with the request.
        """
        try:
            await self._session.rollback()
        except Exception:  # noqa: BLE001 - the original failure is reported
            pass
        logger.error("Member revocation failed", extra={"reason": reason})
        raise RevocationIncomplete(reason) from None


__all__ = ["MemberRevocation", "RevocationIncomplete"]
