"""Stage 6F security: the runtime schedules and claims, and nothing more.

The runtime runs with no request and no user in the room -- which is exactly
when an authorization shortcut would be most tempting and least visible. So
the claims tested here are about what it *cannot* do: reach a tool, answer an
authorization question, read a grant, invent an identity, or be scheduled by
content.
"""

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.background.runtime import BackgroundRuntime, run_due_tasks
from app.execution.models import Execution
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks.models import Task, TaskEvent, TaskStep
from app.tasks.service import TaskService
from app.tasks.states import TaskState, TaskStepState

pytestmark = pytest.mark.anyio

BACKEND = Path(__file__).resolve().parents[2]
RUNTIME = BACKEND / "app" / "background" / "runtime.py"
OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")
WS = "list_workspace_files"


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


def tree():
    return ast.parse(RUNTIME.read_text(encoding="utf-8"))


def imports(parsed) -> set:
    found = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def called(parsed) -> set:
    names = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


# ============================================================================
# A. One runtime, one loop, no second scheduler
# ============================================================================


def test_there_is_exactly_one_background_loop_class() -> None:
    """`ReminderScheduler` is the same class under its old name, not a second."""
    from app.reminders.scheduler import ReminderScheduler

    assert ReminderScheduler is BackgroundRuntime

    definitions = []
    for path in (BACKEND / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and (
                node.name.endswith(("Scheduler", "Runtime", "Poller", "Worker"))
            ):
                definitions.append((str(path.relative_to(BACKEND)), node.name))
    assert definitions == [("app/background/runtime.py", "BackgroundRuntime")], definitions


def test_exactly_one_module_spawns_a_background_task() -> None:
    spawners = []
    for path in (BACKEND / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"create_task", "ensure_future"}
            ):
                spawners.append(str(path.relative_to(BACKEND)))
    assert spawners == ["app/background/runtime.py"], spawners


def test_no_queue_or_worker_framework_exists() -> None:
    forbidden = {"celery", "redis", "rq", "dramatiq", "arq", "kombu", "pika",
                 "aiokafka", "kafka", "apscheduler", "huey", "taskiq"}
    for path in (BACKEND / "app").rglob("*.py"):
        for module in imports(ast.parse(path.read_text(encoding="utf-8"))):
            assert module.split(".")[0].lower() not in forbidden, (path.name, module)


def test_the_runtime_spawns_nothing_per_item() -> None:
    """Sequential by design: the bound on concurrent background work is one."""
    parsed = tree()
    for node in ast.walk(parsed):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                    assert inner.func.attr not in {
                        "create_task", "ensure_future", "gather",
                    }, inner.lineno
    assert "gather" not in called(parsed)


# ============================================================================
# B. The runtime reaches everything through the runner
# ============================================================================


def test_the_runtime_calls_the_runner() -> None:
    parsed = tree()
    assert "app.tasks.runner" in imports(parsed)
    assert "advance" in called(parsed)


def test_the_runtime_imports_nothing_that_executes_or_authorizes() -> None:
    parsed = tree()
    for module in imports(parsed):
        for forbidden in (
            "app.execution", "app.tools", "app.authorization", "app.integrations",
            "app.mail", "app.calendar", "app.research", "app.llm",
        ):
            assert not module.startswith(forbidden), module


def test_the_runtime_calls_no_tool_dispatcher_or_grant() -> None:
    names = called(tree())
    for forbidden in (
        "dispatch", "run", "run_returning_outcome", "approve", "authorize",
        "authorize_with_grants", "active_for", "bind_step", "bind_plan",
        "validate_arguments", "fingerprint_for", "payload_fingerprint",
    ):
        assert forbidden not in names, forbidden


def test_the_runtime_writes_no_step_or_argument() -> None:
    """Task state transitions stay the service's and the runner's."""
    offenders = []
    for node in ast.walk(tree()):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AugAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr in {
                "state", "arguments", "capability", "execution_id",
                "authorized_at", "spent", "plan", "completed_at",
            }:
                offenders.append((node.lineno, target.attr))
    assert offenders == [], offenders


def test_next_run_at_has_exactly_two_writers() -> None:
    writers = set()
    for path in (BACKEND / "app").rglob("*.py"):
        if "reminders" in path.parts:
            continue  # reminders have their own, unrelated `next_run_at`
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = node.targets if isinstance(node, ast.Assign) else []
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "next_run_at":
                    writers.add(str(path.relative_to(BACKEND)))
            if isinstance(node, ast.keyword) and node.arg == "next_run_at":
                writers.add(str(path.relative_to(BACKEND)))
    assert writers == {
        "app/background/runtime.py", "app/tasks/service.py",
    }, writers


def test_no_request_local_identity_is_used() -> None:
    """The owner comes from the task row, never from a request."""
    parsed = tree()
    for module in imports(parsed):
        assert not module.startswith(("fastapi", "starlette", "app.api")), module
    source = RUNTIME.read_text(encoding="utf-8")
    code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
    assert "LOCAL_OWNER_ID" not in code


def test_the_runtime_has_no_dangerous_import_or_call() -> None:
    parsed = tree()
    for module in imports(parsed):
        assert module.split(".")[0] not in {
            "subprocess", "os", "shutil", "socket", "httpx", "requests",
            "urllib", "importlib", "pty", "threading",
        }, module
    for forbidden in ("eval", "exec", "compile", "__import__", "system", "popen"):
        assert forbidden not in called(parsed), forbidden


def test_the_runtime_logs_no_content() -> None:
    offenders = []
    for node in ast.walk(tree()):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"debug", "info", "warning", "error"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr in {
                "objective", "arguments", "plan", "title", "capability",
                "result_summary", "text",
            }:
                offenders.append((node.lineno, inner.attr))
    assert offenders == [], offenders


# ============================================================================
# C. Content cannot schedule work
# ============================================================================


def test_no_content_handling_module_can_reach_the_runtime_or_scheduling() -> None:
    watched = [
        "app/mail", "app/calendar", "app/research", "app/reminders",
        "app/integrations", "app/workflows", "app/history", "app/synthesis",
        "app/memory", "app/knowledge", "app/entities", "app/relationships",
        "app/orchestration", "app/retrieval", "app/intent", "app/planning",
        "app/llm", "app/prompt", "app/services", "app/execution", "app/tools",
    ]
    offenders = []
    for folder in watched:
        for path in (BACKEND / folder).rglob("*.py"):
            if path.name == "scheduler.py" and "reminders" in path.parts:
                continue  # the documented alias, and only the class name
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    module = ",".join(a.name for a in node.names)
                if "app.background" in module or "app.tasks" in module:
                    offenders.append((str(path.relative_to(BACKEND)), module))
            if "schedule_background" in source:
                offenders.append((str(path.relative_to(BACKEND)), "schedule_background"))
    assert offenders == [], offenders


def test_the_reminder_alias_imports_only_the_class() -> None:
    parsed = ast.parse(
        (BACKEND / "app" / "reminders" / "scheduler.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.module == "app.background.runtime":
            assert [(a.name, a.asname) for a in node.names] == [
                ("BackgroundRuntime", "ReminderScheduler")
            ]


def test_scheduling_has_no_source_parameter() -> None:
    import inspect

    parameters = set(inspect.signature(TaskService.schedule_background).parameters)
    assert parameters == {"self", "task_id", "now"}


def test_there_is_no_http_surface_for_background_work() -> None:
    for path in (BACKEND / "app" / "api").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in ("schedule_background", "BackgroundRuntime", "run_due_tasks"):
            assert name not in source, (path.name, name)


async def test_a_full_chat_turn_schedules_nothing(client, conversation_id) -> None:
    for message in (
        "keep working on this in the background",
        "schedule this task to run every hour",
        "run my tasks while I'm away",
    ):
        response = await client.post(
            f"/api/conversations/{conversation_id}/messages", json={"content": message},
        )
        assert response.status_code == 201
    assert (await client.get("/api/tasks")).json() == {"tasks": [], "total": 0}


# ============================================================================
# D. Behavioural security
# ============================================================================


async def _scheduled(session_factory, settings, capability=WS, arguments=None, grant=False):
    from app.authorization.grants import GrantService
    from app.tools.schemas import RiskLevel

    async with session_factory() as session:
        service = TaskService(session, settings=settings)
        created = await service.create_for_user("background security")
        await service.attach_plan(created.task_id, Plan(
            goal=Goal(summary="g", source_intent=IntentType.ACTION),
            tasks=[PlanTask(id="a", title="A", order=1, depth=0,
                            capability=capability, arguments=arguments or {})],
        ))
        await service.authorize_plan(created.task_id)
        await service.transition(created.task_id, TaskState.QUEUED, actor="user")
        if grant:
            await GrantService(session, owner_id=OWNER).create(WS, RiskLevel.LOW)
        await service.schedule_background(created.task_id)
        await session.commit()
        return created.task_id


async def test_no_request_context_is_not_authorization(
    session_factory, execution_settings
) -> None:
    """The runtime has no user, and that grants nothing: a step needing a
    person still waits for one."""
    task_id = await _scheduled(session_factory, execution_settings)
    await run_due_tasks(session_factory, execution_settings)

    async with session_factory() as session:
        execution = (await session.execute(select(Execution))).scalars().one()
        step = (await session.execute(select(TaskStep))).scalars().one()
    assert execution.approved_at is None
    assert step.state is TaskStepState.PENDING


async def test_a_replanned_step_is_reauthorised_not_inherited(
    session_factory, execution_settings
) -> None:
    """A grant for the workspace tool does not cover a step changed to
    another capability after scheduling."""
    task_id = await _scheduled(session_factory, execution_settings, grant=True)
    async with session_factory() as session:
        step = (await session.execute(select(TaskStep))).scalars().one()
        step.capability = "web_search"
        step.arguments = {"query": "x"}
        await session.commit()

    await run_due_tasks(session_factory, execution_settings)
    async with session_factory() as session:
        executions = (await session.execute(select(Execution))).scalars().all()
    assert all(e.approved_at is None for e in executions)
    assert all(e.tool_name != "web_search" or e.state.value == "proposed" for e in executions)


async def test_a_forbidden_capability_is_refused_in_the_background(
    session_factory, execution_settings
) -> None:
    """CRITICAL and FORBIDDEN are refused by policy before any grant, with
    or without a person present."""
    from app.tools import policy
    from app.tools.schemas import RiskLevel

    assert policy.MAX_PERMITTED_RISK is RiskLevel.HIGH

    task_id = await _scheduled(session_factory, execution_settings, grant=True)
    async with session_factory() as session:
        step = (await session.execute(select(TaskStep))).scalars().one()
        step.capability = "future_send_email"  # declared, not executable
        await session.commit()

    await run_due_tasks(session_factory, execution_settings)
    async with session_factory() as session:
        task = (await session.execute(select(Task))).scalars().one()
        assert (await session.execute(
            select(func.count()).select_from(Execution)
        )).scalar() == 0
    assert task.next_run_at is None


async def test_every_background_decision_is_in_the_journal(
    session_factory, execution_settings
) -> None:
    task_id = await _scheduled(session_factory, execution_settings, grant=True)
    await run_due_tasks(session_factory, execution_settings)

    async with session_factory() as session:
        events = (await session.execute(
            select(TaskEvent).order_by(TaskEvent.sequence)
        )).scalars().all()
    kinds = [e.event_type.value for e in events]
    for expected in ("background_scheduled", "background_claimed",
                     "standing_grant_used", "execution_created", "task_completed"):
        assert expected in kinds, expected
    scheduled = next(e for e in events if e.event_type.value == "background_scheduled")
    assert scheduled.actor == "user"
    claimed = next(e for e in events if e.event_type.value == "background_claimed")
    assert claimed.actor == "system"


def test_stage_6f_added_exactly_one_migration() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("*.py"))
    assert [v for v in versions if "0015" < v[:4] <= "0016"] == [
        "0016_background_runtime.py"
    ]


def test_the_migration_adds_one_column_and_alters_nothing_else() -> None:
    parsed = ast.parse(
        (BACKEND / "alembic" / "versions" / "0016_background_runtime.py").read_text()
    )
    operations = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"create_table", "drop_table", "add_column",
                                  "drop_column", "alter_column"}:
                operations.append(node.func.attr)
    assert sorted(operations) == ["add_column", "drop_column"], operations
