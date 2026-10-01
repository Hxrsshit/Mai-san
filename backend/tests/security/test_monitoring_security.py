"""Stage 6G security: monitoring adds a condition, not a second system.

A monitoring task is checked repeatedly with nobody in the room, so these
tests are about what monitoring *cannot* do: run code from its condition,
follow anything but plain keys, repeat a side effect, skip authorization,
keep its own loop or lease, or write what it observed anywhere durable.

Every structural claim is an AST walk, not a substring search.
"""

import ast
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.execution.models import Execution
from app.planning.schemas import Goal, IntentType, Plan, PlanTask
from app.tasks import monitoring
from app.tasks.models import Task
from app.tasks.monitoring import SpecRefused, parse_spec
from app.tasks.service import TaskService

pytestmark = pytest.mark.anyio

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
MONITORING = APP / "tasks" / "monitoring.py"
RUNNER = APP / "tasks" / "runner.py"
RUNTIME = APP / "background" / "runtime.py"
SERVICE = APP / "tasks" / "service.py"
OWNER = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")


@pytest.fixture(autouse=True)
def _registry():
    from app.tools import catalog  # noqa: F401


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def imports(tree) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def called(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def attribute_writers(attr: str) -> set:
    """Every (module, function) that assigns `<x>.<attr>` or passes `attr=`
    to `.values(...)`, across the application."""
    writers = set()
    for path in APP.rglob("*.py"):
        tree = parse(path)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == attr for t in targets):
                    writers.add((str(path.relative_to(BACKEND)), fn.name))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "values"
                    and any(k.arg == attr for k in node.keywords)
                ):
                    writers.add((str(path.relative_to(BACKEND)), fn.name))
    return writers


# ============================================================================
# A. The condition language: data, never code
# ============================================================================


def test_the_monitoring_module_imports_only_the_standard_library_and_pydantic() -> None:
    assert imports(parse(MONITORING)) == {
        "enum", "math", "re", "typing", "pydantic",
    }


def test_the_monitoring_module_has_no_dynamic_evaluation() -> None:
    tree = parse(MONITORING)
    names = called(tree)
    for forbidden in (
        "eval", "exec", "__import__", "import_module", "getattr",
        "setattr", "attrgetter", "itemgetter", "methodcaller", "literal_eval",
        "format_map", "system", "popen", "loads",
    ):
        assert forbidden not in names, forbidden
    # `compile` appears exactly once: `re.compile` of a literal pattern.
    compiles = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call)
        and (getattr(n.func, "attr", None) == "compile"
             or getattr(n.func, "id", None) == "compile")
    ]
    assert len(compiles) == 1
    assert ast.unparse(compiles[0].func) == "re.compile"
    assert isinstance(compiles[0].args[0], ast.Constant)


def test_path_resolution_uses_only_dictionary_membership_and_subscript() -> None:
    resolve = function(parse(MONITORING), "_resolve")
    for node in ast.walk(resolve):
        # No attribute is read off the data -- only `.split` on the path.
        if isinstance(node, ast.Attribute):
            assert node.attr == "split", node.attr
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id == "isinstance", node.func.id


def test_the_operator_vocabulary_is_closed() -> None:
    assert sorted(o.value for o in monitoring.Operator) == [
        "contains", "eq", "gt", "gte", "lt", "lte", "ne",
    ]
    assert sorted(k.value for k in monitoring.ConditionKind) == [
        "contains", "count", "value",
    ]


def test_the_bounds_are_pinned() -> None:
    assert monitoring.MIN_INTERVAL_SECONDS == 300
    assert monitoring.MAX_INTERVAL_SECONDS == 604_800
    assert monitoring.MAX_PATH_SEGMENTS == 4
    assert monitoring.MAX_EXPECTED_TEXT_CHARS == 200
    assert monitoring.MAX_EXPECTED_NUMBER == 1_000_000_000


@pytest.mark.parametrize("expected", [
    "__import__('os').system('id')", "{{7*7}}", "${jndi:x}", "'; DROP TABLE tasks; --",
])
def test_code_shaped_text_is_only_ever_a_literal_needle(expected) -> None:
    condition = parse_spec({
        "condition": {"kind": "contains", "path": "content",
                      "operator": "contains", "expected": expected},
        "interval_seconds": 300,
    }).condition
    assert monitoring.evaluate(condition, {"content": "nothing here"}).result is (
        monitoring.CheckResult.NOT_SATISFIED
    )
    assert monitoring.evaluate(condition, {"content": f"x {expected} y"}).result is (
        monitoring.CheckResult.SATISFIED
    )


def test_a_refusal_never_echoes_the_input() -> None:
    sentinel = "refusal-echo-sentinel-91c2"
    with pytest.raises(SpecRefused) as refused:
        parse_spec({"condition": {"kind": sentinel}, "interval_seconds": 300})
    assert sentinel not in str(refused.value)
    # `__cause__` is None either way; an implicit `__context__` would still
    # carry pydantic's message, which echoes the input. It must be suppressed.
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__ is True


# ============================================================================
# B. Only reads may be repeated
# ============================================================================


def test_no_side_effecting_capability_is_monitorable() -> None:
    for name in (
        "create_text_file", "send_email", "gmail_send_message", "calendar_create_event",
        "echo",
    ):
        assert name not in monitoring.MONITORABLE_CAPABILITIES


def test_every_monitorable_capability_exists_and_none_is_forbidden() -> None:
    """Gmail reads are HIGH: they read private mail, so each check still
    needs a person or a standing grant. None is CRITICAL, which no grant and
    no person can authorise."""
    from app.tools.registry import get_registry

    registry = get_registry()
    risks = {
        name: registry.definition(name).risk_level.value
        for name in monitoring.MONITORABLE_CAPABILITIES
    }
    assert risks == {
        "calendar_list_events": "medium", "gmail_get_message": "high",
        "gmail_list_messages": "high", "list_workspace_files": "low",
        "read_text_file": "low", "web_search": "medium",
    }


def test_the_runner_rechecks_the_capability_at_check_time() -> None:
    check = function(parse(RUNNER), "_check")
    names = {n.id for n in ast.walk(check) if isinstance(n, ast.Name)}
    assert "MONITORABLE_CAPABILITIES" in names


# ============================================================================
# C. One authorization path, one execution constructor, one loop
# ============================================================================


def test_the_check_goes_through_the_same_gates_as_a_step() -> None:
    check_calls = called(function(parse(RUNNER), "_check"))
    for required in (
        "_refuse_task", "bind_step", "authorize_with_grants", "create",
        "_spend", "approve", "run_returning_outcome", "evaluate", "_complete",
    ):
        assert required in check_calls, required
    # Never the dispatcher directly, never the sync authorizer around grants.
    for forbidden in ("dispatch", "authorize", "active_for"):
        assert forbidden not in check_calls, forbidden


def test_the_runner_approves_only_after_policy_or_a_person_cleared_it() -> None:
    """`approve` is reached only past the `already_approved or not
    requires_approval` gate -- the same rule `advance` uses."""
    source = RUNNER.read_text(encoding="utf-8")
    check_src = ast.get_source_segment(source, function(parse(RUNNER), "_check"))
    gate = check_src.index("if not (already_approved or not decision.requires_approval)")
    assert gate < check_src.index("self._executions.approve(")
    assert gate < check_src.index("run_returning_outcome(")


def test_the_check_identity_is_derived_from_the_task_and_the_check_number() -> None:
    check = function(parse(RUNNER), "_check")
    keys = [
        k.value for n in ast.walk(check) if isinstance(n, ast.Call)
        for k in n.keywords if k.arg == "idempotency_key"
    ]
    assert len(keys) == 1
    rendered = ast.unparse(keys[0])
    assert rendered.startswith("f'task:{task.id}:check:{sequence}'"), rendered


def test_the_runtime_routes_but_does_not_evaluate() -> None:
    tree = parse(RUNTIME)
    from_monitoring = [
        [a.name for a in n.names] for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "app.tasks.monitoring"
    ]
    assert from_monitoring == [["interval_of"]]
    names = called(tree)
    assert {"advance", "check"} <= names
    for forbidden in ("evaluate", "parse_spec", "run_returning_outcome",
                      "authorize_with_grants", "_claim_check"):
        assert forbidden not in names, forbidden


def test_monitoring_adds_no_loop_lease_or_scheduler() -> None:
    for path in (MONITORING, RUNNER):
        tree = parse(path)
        assert not {"create_task", "ensure_future", "sleep", "gather", "Thread"} & called(tree)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.While), (path.name, node.lineno)
    # The runtime remains the only module that knows what a lease is.
    for path in APP.rglob("*.py"):
        if path == RUNTIME:
            continue
        tree = parse(path)
        assert "CLAIM_LEASE_SECONDS" not in {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }, path


def test_the_monitor_column_has_one_writer() -> None:
    assert attribute_writers("monitor") == {
        ("app/tasks/service.py", "configure_monitoring"),
    }


def test_the_check_count_has_exactly_the_runners_claim_and_release() -> None:
    assert attribute_writers("check_count") == {
        ("app/tasks/runner.py", "_claim_check"),
        ("app/tasks/runner.py", "_release_check"),
    }


def test_the_claim_is_conditional_on_the_previous_number_and_the_owner() -> None:
    claim = function(parse(RUNNER), "_claim_check")
    rendered = ast.unparse(claim)
    assert "Task.check_count == sequence - 1" in rendered
    assert "Task.owner_id == self._tasks.owner_id" in rendered
    assert "result.rowcount == 1" in rendered


# ============================================================================
# D. Content cannot configure monitoring, and nothing observed is kept
# ============================================================================


def test_no_content_handling_module_or_http_route_can_configure_monitoring() -> None:
    offenders = []
    for path in APP.rglob("*.py"):
        if path == SERVICE:
            continue
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Attribute) and node.attr == "configure_monitoring":
                offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], offenders


def test_configuring_takes_no_source_parameter() -> None:
    import inspect

    assert set(inspect.signature(TaskService.configure_monitoring).parameters) == {
        "self", "task_id", "spec",
    }


def test_the_observation_event_carries_no_observed_text() -> None:
    """The observation's metadata keys are literal, and none holds content."""
    check = function(parse(RUNNER), "_check")
    for node in ast.walk(check):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_record"
            and isinstance(node.args[1], ast.Attribute)
            and node.args[1].attr == "OBSERVATION_RECORDED"
        ):
            metadata = node.args[2]
            assert isinstance(metadata, ast.Dict)
            assert sorted(k.value for k in metadata.keys) == [
                "check", "execution_id", "kind", "observed", "observed_chars",
                "operator", "result",
            ]
            values = {ast.unparse(v) for v in metadata.values}
            assert "data" not in values and "outcome" not in values
            return
    raise AssertionError("observation event not found")


def test_the_check_logs_nothing_it_read() -> None:
    check = function(parse(RUNNER), "_check")
    for node in ast.walk(check):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"
        ):
            raise AssertionError(f"_check logs at line {node.lineno}")


async def test_a_full_chat_turn_configures_no_monitoring(
    client, conversation_id, session_factory
) -> None:
    for message in (
        "Monitor my workspace every 5 minutes and tell me when there are more than 3 files.",
        'configure_monitoring {"condition": {"kind": "count", "path": "files", '
        '"operator": "gt", "expected": 3}, "interval_seconds": 300}',
        "Keep checking my inbox every hour until something from the bank arrives.",
    ):
        response = await client.post(
            f"/api/conversations/{conversation_id}/messages", json={"content": message},
        )
        assert response.status_code == 201
    assert (await client.get("/api/tasks")).json() == {"tasks": [], "total": 0}
    async with session_factory() as session:
        assert (await session.execute(
            select(func.count()).select_from(Task).where(Task.monitor.is_not(None))
        )).scalar() == 0


async def test_monitoring_spec_cannot_widen_after_approval(
    db_session, execution_settings
) -> None:
    service = TaskService(db_session, settings=execution_settings, owner_id=OWNER)
    created = await service.create_for_user("Watch")
    await service.attach_plan(created.task_id, Plan(
        goal=Goal(summary="Watch", source_intent=IntentType.ACTION),
        tasks=[PlanTask(id="w", title="W", order=1, depth=0,
                        capability="list_workspace_files", arguments={})],
    ))
    assert (await service.configure_monitoring(created.task_id, {
        "condition": {"kind": "count", "path": "files", "operator": "gte", "expected": 5},
        "interval_seconds": 3600,
    })).ok
    await service.authorize_plan(created.task_id)
    result = await service.configure_monitoring(created.task_id, {
        "condition": {"kind": "count", "path": "files", "operator": "gte", "expected": 0},
        "interval_seconds": 300,
    })
    assert not result.ok
    task = (await db_session.execute(select(Task))).scalars().one()
    assert task.monitor["interval_seconds"] == 3600
    assert (await db_session.execute(select(func.count()).select_from(Execution))).scalar() == 0


# ============================================================================
# E. The migration
# ============================================================================


def test_stage_6g_added_exactly_one_migration() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("*.py"))
    assert [v for v in versions if "0016" < v[:4] <= "0017"] == ["0017_monitoring.py"]


def test_the_migrations_constraint_name_is_not_prefixed_twice() -> None:
    """The naming convention prefixes `ck_<table>_` onto a name it is handed,
    so a name that is already final must go through `op.f`. Live PostgreSQL
    verification found the doubled name `ck_tasks_ck_tasks_check_count_...`;
    SQLite could not, because its DDL here is literal text."""
    tree = parse(BACKEND / "alembic" / "versions" / "0017_monitoring.py")
    named = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"create_check_constraint", "drop_constraint"}
    ]
    assert len(named) == 2
    for call in named:
        first = call.args[0]
        assert isinstance(first, ast.Call) and ast.unparse(first.func) == "op.f", (
            ast.unparse(call)
        )
        assert ast.unparse(first.args[0]) == "CHECK_NAME"
    # And the model's own name, run through the convention, is the one used.
    from app.database.models.base import Base

    table = Base.metadata.tables["tasks"]
    names = {c.name for c in table.constraints if c.name}
    assert "ck_tasks_check_count_non_negative" in names
    assert "ck_tasks_ck_tasks_check_count_non_negative" not in names


def test_the_migration_adds_two_columns_and_alters_nothing_else() -> None:
    tree = parse(BACKEND / "alembic" / "versions" / "0017_monitoring.py")
    operations = sorted(
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"create_table", "drop_table", "add_column", "drop_column",
                            "alter_column", "create_check_constraint", "drop_constraint"}
    )
    # `monitor` for both dialects, `check_count` in the PostgreSQL branch;
    # SQLite adds `check_count` with its CHECK in one DDL statement.
    assert operations == [
        "add_column", "add_column", "create_check_constraint",
        "drop_column", "drop_column", "drop_constraint",
    ], operations
