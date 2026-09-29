"""Stage 6E: standing approval grants.

A standing grant removes one question and nothing else. Every test drives the
real `AuthorizationService` and, where execution is involved, the real
`TaskRunner` and dispatcher.

Time is injected throughout. No test sleeps, and no expiry expectation is
computed from the constant it is meant to be guarding.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.authorization.grants import GrantRefused, GrantService
from app.authorization.models import (
    DEFAULT_GRANT_TTL_SECONDS,
    MAX_GRANT_TTL_SECONDS,
    ApprovalGrant,
    GrantStatus,
)
from app.execution.models import Execution
from app.execution.states import ExecutionState
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks.models import Task, TaskEvent, TaskStep
from app.tasks.runner import TaskRunner
from app.tasks.schemas import RunnerOutcome
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState
from app.tools.authorization import AuthorizationService
from app.tools.schemas import (
    ActionProposal,
    ActionSource,
    AuthorizationStatus,
    DenialReason,
    RiskLevel,
)

pytestmark = pytest.mark.anyio

OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
OTHER_OWNER = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000bbbb")

#: MEDIUM risk, executable, and the tool's own declaration requires a
#: person -- so policy returns APPROVAL_REQUIRED, which is exactly the
#: status a standing grant may satisfy.
GATED = "web_search"
GATED_RISK = RiskLevel.MEDIUM
GATED_ARGS = {"query": "kerala in october"}
#: MEDIUM risk, executable, and policy needs no person.
AUTO = "calendar_list_events"
AUTO_ARGS = {
    "starts_at": "2026-10-01T00:00:00+00:00",
    "ends_at": "2026-10-02T00:00:00+00:00",
    "max_results": 5,
}
#: A fixed instant. Every expiry expectation below is a literal against it.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


@pytest.fixture
def grants(db_session) -> GrantService:
    return GrantService(db_session, owner_id=OWNER)


@pytest.fixture
def authorization() -> AuthorizationService:
    return AuthorizationService()


def proposal(tool=GATED, arguments=None, source=ActionSource.MODEL):
    return ActionProposal(
        tool_name=tool,
        arguments=dict(GATED_ARGS if arguments is None else arguments),
        source=source,
    )


# ============================================================================
# A. Creation
# ============================================================================


async def test_a_grant_is_created_and_persisted(grants, db_session) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)

    assert grant.owner_id == OWNER
    assert grant.capability == GATED
    assert grant.risk_level is GATED_RISK
    assert grant.revoked_at is None

    stored = (await db_session.execute(select(ApprovalGrant))).scalars().one()
    assert stored.id == grant.id
    assert stored.expires_at == NOW + timedelta(seconds=DEFAULT_GRANT_TTL_SECONDS)


async def test_the_capability_name_is_canonicalised(grants) -> None:
    grant = await grants.create("  WEB_SEARCH  ", GATED_RISK, now=NOW)
    assert grant.capability == GATED


@pytest.mark.parametrize("bad, reason", [
    ("", "empty_capability"),
    ("   ", "empty_capability"),
    ("no_such_tool_at_all", "unknown_capability"),
    ("gmail_*", "wildcards_not_supported"),
    ("web_search?", "wildcards_not_supported"),
    ("%", "wildcards_not_supported"),
])
async def test_an_unusable_capability_is_refused(bad, reason, grants) -> None:
    with pytest.raises(GrantRefused) as caught:
        await grants.create(bad, GATED_RISK, now=NOW)
    assert caught.value.reason == reason


async def test_a_critical_grant_cannot_be_created(grants) -> None:
    """Refused at creation as well as being unreachable at decision time."""
    with pytest.raises(GrantRefused) as caught:
        await grants.create(GATED, RiskLevel.CRITICAL, now=NOW)
    assert caught.value.reason == "critical_risk_not_grantable"


async def test_a_grant_below_the_capabilitys_risk_is_refused(grants) -> None:
    """`web_search` is MEDIUM; a LOW grant could never satisfy it."""
    with pytest.raises(GrantRefused) as caught:
        await grants.create(GATED, RiskLevel.LOW, now=NOW)
    assert caught.value.reason == "risk_below_capability"


@pytest.mark.parametrize("ttl", [0, -1, MAX_GRANT_TTL_SECONDS + 1, True, 1.5, "600"])
async def test_an_unusable_ttl_is_refused(ttl, grants) -> None:
    with pytest.raises(GrantRefused):
        await grants.create(GATED, GATED_RISK, ttl_seconds=ttl, now=NOW)


def test_the_grant_bounds_are_what_they_say() -> None:
    """Literal-pinned, so widening either has to be argued for."""
    assert DEFAULT_GRANT_TTL_SECONDS == 86_400
    assert MAX_GRANT_TTL_SECONDS == 604_800


async def test_a_grant_always_expires(grants) -> None:
    grant = await grants.create(
        GATED, GATED_RISK, ttl_seconds=MAX_GRANT_TTL_SECONDS, now=NOW
    )
    assert grant.expires_at is not None
    assert grant.expires_at.isoformat() == "2026-10-08T12:00:00+00:00"


# ============================================================================
# B. Expiry, at the boundary
# ============================================================================


async def test_the_expiry_boundary_is_exact(grants) -> None:
    grant = await grants.create(GATED, GATED_RISK, ttl_seconds=3600, now=NOW)
    # Literal instants, not arithmetic on the TTL the test is guarding.
    assert grant.expires_at.isoformat() == "2026-10-01T13:00:00+00:00"

    before = datetime(2026, 10, 1, 12, 59, 59, tzinfo=timezone.utc)
    exactly = datetime(2026, 10, 1, 13, 0, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 1, 13, 0, 1, tzinfo=timezone.utc)

    assert grant.status(before) is GrantStatus.ACTIVE
    assert grant.status(exactly) is GrantStatus.EXPIRED
    assert grant.status(after) is GrantStatus.EXPIRED


async def test_an_expired_grant_is_not_found_but_is_still_recorded(
    grants, db_session
) -> None:
    await grants.create(GATED, GATED_RISK, ttl_seconds=3600, now=NOW)
    later = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)

    assert await grants.active_for(GATED, now=later) is None
    assert len((await db_session.execute(select(ApprovalGrant))).scalars().all()) == 1


# ============================================================================
# C. Revocation
# ============================================================================


async def test_revocation_is_immediate_and_persisted(grants, db_session) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    assert await grants.active_for(GATED, now=NOW) is not None

    revoked = await grants.revoke(grant.id, reason="user_changed_mind", now=NOW)
    assert revoked.revoked_at == NOW
    assert revoked.revoked_reason == "user_changed_mind"
    assert revoked.status(NOW) is GrantStatus.REVOKED

    assert await grants.active_for(GATED, now=NOW) is None


async def test_revocation_never_deletes_the_record(grants, db_session) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    await grants.revoke(grant.id, now=NOW)

    rows = (await db_session.execute(select(ApprovalGrant))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == grant.id
    assert [g.id for g in await grants.list_grants()] == [grant.id]


async def test_revoking_twice_keeps_the_first_timestamp(grants) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    first = await grants.revoke(grant.id, reason="one", now=NOW)

    later = NOW + timedelta(minutes=5)
    second = await grants.revoke(grant.id, reason="two", now=later)

    def utc(value):
        # SQLite hands back naive values; comparing one to an aware instant
        # would fail for a reason that has nothing to do with revocation.
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    assert utc(second.revoked_at) == utc(first.revoked_at) == NOW
    assert second.revoked_reason == "one"


async def test_revocation_beats_expiry(grants) -> None:
    grant = await grants.create(GATED, GATED_RISK, ttl_seconds=3600, now=NOW)
    await grants.revoke(grant.id, now=NOW)
    long_after = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert grant.status(long_after) is GrantStatus.REVOKED


async def test_another_owner_cannot_revoke(grants, db_session) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    theirs = GrantService(db_session, owner_id=OTHER_OWNER)

    assert await theirs.revoke(grant.id, now=NOW) is None
    assert (await grants.get(grant.id)).revoked_at is None


# ============================================================================
# D. The authorization decision
# ============================================================================


async def test_without_a_grant_nothing_changes(authorization, grants) -> None:
    plain = authorization.authorize(proposal())
    with_grants = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )

    assert with_grants.status is plain.status is AuthorizationStatus.APPROVAL_REQUIRED
    assert with_grants.requires_approval is True
    assert with_grants.standing_grant_id is None


async def test_a_live_grant_supplies_the_approval(authorization, grants) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    decision = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )

    # The status is unchanged and stays truthful: policy does require an
    # approval. What changed is that one already exists.
    assert decision.status is AuthorizationStatus.APPROVAL_REQUIRED
    assert decision.requires_approval is False
    assert decision.standing_grant_id == grant.id
    assert decision.reason == DenialReason.STANDING_GRANT


async def test_a_grant_is_reusable(authorization, grants) -> None:
    await grants.create(GATED, GATED_RISK, now=NOW)
    for _ in range(4):
        decision = await authorization.authorize_with_grants(
            proposal(), grants=grants, now=NOW
        )
        assert decision.requires_approval is False


async def test_an_expired_grant_does_not_authorize(authorization, grants) -> None:
    await grants.create(GATED, GATED_RISK, ttl_seconds=3600, now=NOW)
    after = datetime(2026, 10, 1, 13, 0, 1, tzinfo=timezone.utc)

    decision = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=after
    )
    assert decision.requires_approval is True
    assert decision.standing_grant_id is None


async def test_a_revoked_grant_does_not_authorize(authorization, grants) -> None:
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    await grants.revoke(grant.id, now=NOW)

    decision = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )
    assert decision.requires_approval is True
    assert decision.standing_grant_id is None


async def test_a_grant_for_one_capability_does_not_cover_another(
    authorization, grants
) -> None:
    await grants.create(GATED, GATED_RISK, now=NOW)

    other = await authorization.authorize_with_grants(
        proposal(tool="gmail_list_messages", arguments={}),
        grants=grants, now=NOW,
    )
    assert other.standing_grant_id is None
    assert other.requires_approval is True


async def test_a_grant_cannot_overturn_a_refusal(authorization, grants) -> None:
    """Only an approval requirement may be satisfied, never a denial."""
    await grants.create(GATED, GATED_RISK, now=NOW)

    # Unknown capability.
    unknown = await authorization.authorize_with_grants(
        proposal(tool="not_a_tool", arguments={}), grants=grants, now=NOW
    )
    assert unknown.status is AuthorizationStatus.UNKNOWN_TOOL
    assert unknown.requires_approval is True
    assert unknown.standing_grant_id is None

    # Arguments that fail the capability's own model.
    bad = await authorization.authorize_with_grants(
        proposal(arguments={"not_a_field": 1}), grants=grants, now=NOW
    )
    assert bad.status is AuthorizationStatus.FORBIDDEN
    assert bad.requires_approval is True
    assert bad.standing_grant_id is None


async def test_a_grant_does_not_touch_an_already_allowed_action(
    authorization, grants
) -> None:
    """`calendar_list_events` needs no approval; a grant changes nothing."""
    decision = await authorization.authorize_with_grants(
        proposal(tool=AUTO, arguments=AUTO_ARGS), grants=grants, now=NOW
    )
    assert decision.status is AuthorizationStatus.ALLOWED
    assert decision.requires_approval is False
    assert decision.standing_grant_id is None


async def test_a_grant_does_not_cover_a_risk_raised_since(
    authorization, grants, db_session
) -> None:
    """A capability that became riskier stops being covered, silently and
    without anyone having to remember to revoke."""
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    # The person agreed to LOW; the capability is MEDIUM now.
    grant.risk_level = RiskLevel.LOW
    await db_session.flush()

    decision = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )
    assert decision.requires_approval is True
    assert decision.standing_grant_id is None


async def test_critical_is_never_satisfiable_by_a_grant(authorization) -> None:
    """Structural: policy refuses CRITICAL before a grant is consulted."""
    from app.tools import policy
    from app.tools.schemas import risk_rank

    assert policy.MAX_PERMITTED_RISK is RiskLevel.HIGH
    assert risk_rank(RiskLevel.CRITICAL) > risk_rank(policy.MAX_PERMITTED_RISK)

    class AlwaysGranting:
        async def active_for(self, capability, now=None):  # pragma: no cover
            raise AssertionError("a grant was consulted for a refused action")

    from app.tools.schemas import ToolCategory, ToolDefinition

    critical = ToolDefinition(
        name="dangerous", description="A critical capability.",
        category=ToolCategory.SYSTEM, risk_level=RiskLevel.CRITICAL,
    )
    status, _ = policy.evaluate(critical, "dangerous")
    assert status is AuthorizationStatus.FORBIDDEN


async def test_duplicate_grants_agree(authorization, grants, db_session) -> None:
    """Two grants for the same thing cannot contradict each other."""
    first = await grants.create(GATED, GATED_RISK, ttl_seconds=3600, now=NOW)
    second = await grants.create(GATED, GATED_RISK, ttl_seconds=7200, now=NOW)

    decision = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )
    assert decision.requires_approval is False
    # The longest-lived one, deterministically.
    assert decision.standing_grant_id == second.id

    # Revoking one leaves the other working.
    await grants.revoke(second.id, now=NOW)
    still = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )
    assert still.standing_grant_id == first.id

    await grants.revoke(first.id, now=NOW)
    gone = await authorization.authorize_with_grants(
        proposal(), grants=grants, now=NOW
    )
    assert gone.requires_approval is True


# ============================================================================
# E. Owner isolation
# ============================================================================


async def test_a_grant_does_not_cross_owners(
    authorization, grants, db_session
) -> None:
    await grants.create(GATED, GATED_RISK, now=NOW)
    theirs = GrantService(db_session, owner_id=OTHER_OWNER)

    assert await theirs.active_for(GATED, now=NOW) is None
    decision = await authorization.authorize_with_grants(
        proposal(), grants=theirs, now=NOW
    )
    assert decision.requires_approval is True
    assert decision.standing_grant_id is None


async def test_listing_shows_only_this_owners_grants(
    grants, db_session
) -> None:
    await grants.create(GATED, GATED_RISK, now=NOW)
    theirs = GrantService(db_session, owner_id=OTHER_OWNER)
    await theirs.create(AUTO, RiskLevel.MEDIUM, now=NOW)

    assert [g.capability for g in await grants.list_grants()] == [GATED]
    assert [g.capability for g in await theirs.list_grants()] == [AUTO]


# ============================================================================
# F. Through the runner
# ============================================================================


def goal() -> Goal:
    return Goal(summary="Do the thing", source_intent=IntentType.ACTION)


def step(key, order, deps=(), capability=GATED, arguments=None) -> PlanTask:
    return PlanTask(
        id=key, title=f"Step {key}", dependencies=list(deps), order=order,
        depth=len(deps), capability=capability,
        arguments=GATED_ARGS if arguments is None else arguments,
    )


async def queued_task(service, db_session, *steps):
    created = await service.create_for_user("Do the thing")
    assert (await service.attach_plan(
        created.task_id, Plan(goal=goal(), tasks=list(steps))
    )).ok
    await service.authorize_plan(created.task_id)
    # The plan-level gate is a person's; this stands in for one so the
    # per-step grant decision below is what is being tested.
    task = (await db_session.execute(
        select(Task).where(Task.id == created.task_id)
    )).scalars().one()
    task.state = TaskState.QUEUED
    await db_session.flush()
    return created.task_id


async def test_without_a_grant_the_runner_still_blocks(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    task_id = await queued_task(service, db_session, step("a", 1))

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "awaiting_human_approval"


async def test_a_grant_lets_the_runner_proceed(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    await grants.create(GATED, GATED_RISK)

    task_id = await queued_task(service, db_session, step("a", 1))
    result = await runner.advance(task_id)

    assert result.reason != "awaiting_human_approval"
    assert result.outcome in {
        RunnerOutcome.STEP_COMPLETED, RunnerOutcome.TASK_COMPLETED,
        RunnerOutcome.STEP_FAILED,
    }, result

    # The step went through the real envelope: an execution exists, and it
    # was approved with a fingerprint before it ran.
    execution = (await db_session.execute(select(Execution))).scalars().one()
    assert execution.approved_at is not None
    assert execution.approved_fingerprint is not None
    assert execution.state is not ExecutionState.PROPOSED


async def test_the_journal_says_why_mai_did_not_ask(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    grant = await grants.create(GATED, GATED_RISK)

    task_id = await queued_task(service, db_session, step("a", 1))
    await runner.advance(task_id)

    events = (await db_session.execute(
        select(TaskEvent).order_by(TaskEvent.sequence)
    )).scalars().all()
    used = [e for e in events if e.event_type.value == "standing_grant_used"]
    assert len(used) == 1
    assert used[0].event_metadata["grant_id"] == str(grant.id)
    assert used[0].event_metadata["capability"] == GATED


async def test_revoking_stops_the_next_step(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    grant = await grants.create(GATED, GATED_RISK)

    task_id = await queued_task(
        service, db_session, step("a", 1, capability=AUTO, arguments=AUTO_ARGS),
        step("b", 2, ["a"]),
    )
    first = await runner.advance(task_id)
    assert first.outcome is RunnerOutcome.STEP_COMPLETED

    await grants.revoke(grant.id, reason="user_changed_mind")
    second = await runner.advance(task_id)
    assert second.outcome is RunnerOutcome.BLOCKED
    assert second.reason == "awaiting_human_approval"

    blocked = (await db_session.execute(
        select(TaskStep).where(TaskStep.step_key == "b")
    )).scalars().one()
    assert blocked.state is TaskStepState.PENDING


async def test_a_grant_does_not_bypass_argument_validation(
    calendar_runner
) -> None:
    """The capability's own model stays authoritative."""
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    await grants.create(GATED, GATED_RISK)

    task_id = await queued_task(
        service, db_session, step("a", 1, arguments={"query": "x"})
    )
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.arguments = {"not_a_field": "anything"}
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "arguments_failed_validation"
    assert (await db_session.execute(select(Execution))).scalars().all() == []


async def test_a_grant_does_not_bypass_capability_availability(
    calendar_runner
) -> None:
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    await grants.create(GATED, GATED_RISK)

    task_id = await queued_task(service, db_session, step("a", 1))
    row = (await db_session.execute(select(TaskStep))).scalars().one()
    row.capability = "future_send_email"
    await db_session.flush()

    result = await runner.advance(task_id)
    assert result.outcome is RunnerOutcome.REFUSED
    assert result.reason == "capability_unavailable"


async def test_a_replan_to_another_capability_does_not_inherit_the_grant(
    calendar_runner
) -> None:
    """The escalation boundary. A grant is never transferable."""
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    await grants.create(AUTO, RiskLevel.MEDIUM)  # the *low*-stakes one

    task_id = await queued_task(service, db_session, step("a", 1))  # GATED
    result = await runner.advance(task_id)

    assert result.outcome is RunnerOutcome.BLOCKED
    assert result.reason == "awaiting_human_approval"


async def test_a_grant_survives_a_restart(calendar_runner) -> None:
    service, runner, db_session = calendar_runner
    grants = GrantService(db_session, owner_id=service.owner_id)
    grant = await grants.create(GATED, GATED_RISK)
    await db_session.commit()

    fresh = GrantService(db_session, owner_id=service.owner_id)
    found = await fresh.active_for(GATED)
    assert found is not None and found.id == grant.id

    await fresh.revoke(grant.id, reason="done")
    await db_session.commit()

    after = GrantService(db_session, owner_id=service.owner_id)
    assert await after.active_for(GATED) is None
    assert (await after.get(grant.id)).revoked_reason == "done"


# ============================================================================
# G. Concurrency
# ============================================================================


async def test_concurrent_revocation_settles_on_one_timestamp(
    db_session, session_factory
) -> None:
    grants = GrantService(db_session, owner_id=OWNER)
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    grant_id = grant.id
    await db_session.commit()

    async with session_factory() as first, session_factory() as second:
        a = GrantService(first, owner_id=OWNER)
        b = GrantService(second, owner_id=OWNER)
        await a.revoke(grant_id, reason="first", now=NOW)
        await first.commit()
        await b.revoke(grant_id, reason="second", now=NOW + timedelta(minutes=5))
        await second.commit()

    async with session_factory() as session:
        final = (await session.execute(select(ApprovalGrant))).scalars().one()
    assert final.revoked_reason == "first"
    assert final.revoked_at.replace(tzinfo=timezone.utc) == NOW


async def test_a_revoked_grant_cannot_be_used_from_another_session(
    db_session, session_factory
) -> None:
    grants = GrantService(db_session, owner_id=OWNER)
    grant = await grants.create(GATED, GATED_RISK, now=NOW)
    await db_session.commit()

    async with session_factory() as revoker:
        await GrantService(revoker, owner_id=OWNER).revoke(grant.id, now=NOW)
        await revoker.commit()

    async with session_factory() as reader:
        decision = await AuthorizationService().authorize_with_grants(
            proposal(), grants=GrantService(reader, owner_id=OWNER), now=NOW
        )
    assert decision.requires_approval is True


async def test_concurrent_authorizations_agree(db_session, session_factory) -> None:
    grants = GrantService(db_session, owner_id=OWNER)
    await grants.create(GATED, GATED_RISK, now=NOW)
    await db_session.commit()

    async def decide():
        async with session_factory() as session:
            return await AuthorizationService().authorize_with_grants(
                proposal(), grants=GrantService(session, owner_id=OWNER), now=NOW
            )

    decisions = await asyncio.gather(*(decide() for _ in range(4)))

    # The property is that concurrency never produces a *wrongly permissive*
    # decision, and that every decision which found a grant found the same
    # one. A reader under SQLite lock contention may fail to read the table
    # at all, and `active_for` then returns None -- asking the user is the
    # correct conservative outcome, not a contradiction.
    permitted = [d for d in decisions if not d.requires_approval]
    assert permitted, "no reader saw the grant at all"
    assert len({d.standing_grant_id for d in permitted}) == 1
    for refused in (d for d in decisions if d.requires_approval):
        assert refused.standing_grant_id is None
        assert refused.status is AuthorizationStatus.APPROVAL_REQUIRED


# ============================================================================
# H. Gaps found by mutation testing
# ============================================================================


async def test_the_grant_records_the_capabilitys_risk_not_the_requested_one(
    grants, db_session
) -> None:
    """K5: every earlier test passed the capability's exact risk, so the
    two were always equal and the distinction was never observed.

    A caller may ask for a grant at a higher risk than the capability
    carries -- and what is stored is the capability's, because that is what
    a later comparison must be made against. Storing the request would let
    an over-broad ask quietly widen the grant.
    """
    grant = await grants.create(GATED, RiskLevel.HIGH, now=NOW)

    assert grant.risk_level is RiskLevel.MEDIUM, "the request was stored"
    stored = (await db_session.execute(select(ApprovalGrant))).scalars().one()
    assert stored.risk_level is RiskLevel.MEDIUM


async def test_no_grant_service_means_no_grant(authorization) -> None:
    """D5: nothing exercised the absent-lookup path.

    A caller with no grant service must get the plain policy answer, not an
    error and certainly not a permission.
    """
    decision = await authorization.authorize_with_grants(
        proposal(), grants=None, now=NOW
    )
    assert decision.status is AuthorizationStatus.APPROVAL_REQUIRED
    assert decision.requires_approval is True
    assert decision.standing_grant_id is None

    plain = authorization.authorize(proposal())
    assert decision.status is plain.status
    assert decision.requires_approval == plain.requires_approval


async def test_canonicalisation_is_belt_and_braces(grants) -> None:
    """C2 is an equivalent mutation, and this records why.

    `ToolRegistry.canonical` is exactly `strip().lower()`, and `create`
    already applies both before calling it -- so removing the call changes
    nothing today. It stays because the normalisation above it could change,
    and the registry is the authority on what a name means.
    """
    from app.tools.registry import get_registry

    registry = get_registry()
    for spelling in ("  WEB_SEARCH  ", "Web_Search", "web_search"):
        assert registry.canonical(spelling) == GATED
        grant = await grants.create(spelling, GATED_RISK, now=NOW)
        assert grant.capability == GATED
