"""Stage 4E: at most one successful attempt per execution record.

The guarantee is enforced by the database, not by the application. Two
concurrent runners issue the same conditional UPDATE; the `state = 'approved'`
predicate makes exactly one of them match a row. That is why the property
holds across processes, where an in-memory lock would not.

The race tests use `concurrent_session_factory`, a file-backed database where
each session holds its own connection. The default in-memory fixture shares
one connection through StaticPool, so two "concurrent" transactions there are
the same transaction and would prove nothing.

SQLite still serialises writers, so what these prove is the *logic*: the loser
sees no matching row and refuses rather than running. Behaviour under true
parallelism is PostgreSQL's to demonstrate, and the acceptance report covers
it there.
"""

import asyncio

import pytest

from app.execution.errors import AlreadyRunning, ExecutionError
from app.execution.schemas import ExecutionRequest
from app.execution.service import ExecutionService
from app.execution.states import ExecutionState


async def _approved(executions, path="race.txt", key="race-1"):
    execution = await executions.create(
        ExecutionRequest(
            tool_name="create_text_file",
            arguments={"path": path, "content": "once"},
            idempotency_key=key,
        )
    )
    await executions.approve(execution.id)
    return execution


async def test_a_second_claim_on_a_claimed_execution_is_refused(
    executions, workspace
) -> None:
    """The primitive itself: claim twice, and the second refuses.

    Claiming directly rather than through `run`, so the window between the
    claim and the tool call -- the only window that matters -- is the thing
    under test.
    """
    execution = await _approved(executions)
    dispatcher = executions._dispatcher

    await dispatcher._claim(execution)
    assert execution.state is ExecutionState.EXECUTING

    with pytest.raises(AlreadyRunning):
        await dispatcher._claim(execution)


async def test_two_runners_produce_one_file_and_one_refusal(
    concurrent_session_factory, execution_settings, workspace
) -> None:
    """Two services, two sessions, one execution record.

    Separate sessions rather than one shared session: a single session would
    let SQLAlchemy's identity map answer the second attempt from memory, and
    the test would pass without the database deciding anything.
    """
    async with concurrent_session_factory() as setup_session:
        setup = ExecutionService(setup_session, settings=execution_settings)
        execution = await _approved(setup, path="contested.txt", key="race-2")
        await setup_session.commit()
        execution_id = execution.id

    async def attempt():
        async with concurrent_session_factory() as session:
            service = ExecutionService(session, settings=execution_settings)
            try:
                await service.run(execution_id)
                await session.commit()
                return "ran"
            except ExecutionError as refusal:
                return refusal.reason

    outcomes = await asyncio.gather(attempt(), attempt())

    assert sorted(outcomes).count("ran") == 1, outcomes
    assert (workspace / "contested.txt").read_text() == "once"


async def test_the_loser_of_a_race_did_not_run_the_tool(
    session_factory, execution_settings, workspace
) -> None:
    """A refused claim must not reach the tool at all.

    Proved against the filesystem: `create_text_file` refuses to overwrite, so
    a second run that reached the tool would raise a *different* error. Seeing
    the claim refusal means it stopped before the tool.
    """
    async with session_factory() as setup_session:
        setup = ExecutionService(setup_session, settings=execution_settings)
        execution = await _approved(setup, path="single.txt", key="race-3")
        await setup_session.commit()
        execution_id = execution.id

    async with session_factory() as first_session:
        first = ExecutionService(first_session, settings=execution_settings)
        await first.run(execution_id)
        await first_session.commit()

    async with session_factory() as second_session:
        second = ExecutionService(second_session, settings=execution_settings)
        with pytest.raises(ExecutionError) as refused:
            await second.run(execution_id)

    # Approval required, because the record is now SUCCEEDED rather than
    # APPROVED -- refused by state, before the claim and long before the tool.
    assert refused.value.reason == "approval_required"
    assert (workspace / "single.txt").read_text() == "once"


async def test_concurrent_creates_with_one_key_make_one_record(
    concurrent_session_factory, execution_settings
) -> None:
    """The idempotency guarantee, under a race.

    Two concurrent creates cannot see each other's uncommitted row, so the
    application-level check loses by construction. The UNIQUE index is what
    decides, and the loser reads the winner's record instead of raising.
    """
    from sqlalchemy import func, select

    from app.execution.models import Execution

    async def create():
        async with concurrent_session_factory() as session:
            service = ExecutionService(session, settings=execution_settings)
            execution = await service.create(
                ExecutionRequest(
                    tool_name="create_text_file",
                    arguments={"path": "dup.txt", "content": "x"},
                    idempotency_key="duplicate-key",
                )
            )
            await session.commit()
            return execution.id

    first, second = await asyncio.gather(create(), create())

    assert first == second

    async with concurrent_session_factory() as session:
        total = (
            await session.execute(select(func.count()).select_from(Execution))
        ).scalar_one()
    assert total == 1


async def test_the_database_refuses_a_duplicate_idempotency_key(
    db_session, execution_settings
) -> None:
    """The constraint itself, tested directly rather than through a race.

    The service checks for an existing key before inserting, and on SQLite --
    which serialises writers -- that check always wins, so the race test above
    passes whether or not the index is unique. This one bypasses the service
    and asks the database, which is where the guarantee actually lives.
    """
    from sqlalchemy.exc import IntegrityError

    from app.execution.models import Execution
    from app.execution.states import ExecutionState
    from app.tools.schemas import AuthorizationStatus

    def row():
        return Execution(
            tool_name="create_text_file",
            arguments={"path": "a.txt", "content": "x"},
            state=ExecutionState.PROPOSED,
            authorization_status=AuthorizationStatus.APPROVAL_REQUIRED,
            idempotency_key="the-same-key",
        )

    db_session.add(row())
    await db_session.flush()

    db_session.add(row())
    with pytest.raises(IntegrityError):
        await db_session.flush()

    await db_session.rollback()
