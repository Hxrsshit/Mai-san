"""Stage 6P security: task state is written by compare-and-set, everywhere.

Known defect 8 was a read-then-write: a copy read before another transaction
moved the task could overwrite the newer state. These pins keep every
lifecycle write conditional on the state it was decided from, so a future
edit cannot quietly bring an unconditional write back. AST walks only.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
SERVICE = APP / "tasks" / "service.py"
RUNNER = APP / "tasks" / "runner.py"
RUNTIME = APP / "background" / "runtime.py"
REQUESTS = APP / "tasks" / "requests.py"


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))


def function(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def updates_of(node) -> list:
    """The rendered `.where(...)` of every `update(Task)` statement in `node`."""
    found = []
    for call in ast.walk(node):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "where" and "update(Task)" in ast.unparse(call.func.value)):
            found.append(ast.unparse(call))
    return found


def test_the_only_direct_state_assignments_are_the_two_safe_ones() -> None:
    """`x.state = ...` on a task: only where the row is already this
    transaction's -- the runner's completion, after its conditional UPDATE
    matched, and `attach_plan`, while the proposal is still being created."""
    sites = []
    for path in list((APP / "tasks").glob("*.py")) + [RUNTIME]:
        tree = parse(path)
        for f in ast.walk(tree):
            if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(f):
                if isinstance(n, ast.Assign):
                    for target in n.targets:
                        if (isinstance(target, ast.Attribute) and target.attr == "state"
                                and ast.unparse(target.value) in {"task", "refreshed"}):
                            sites.append((path.name, f.name))
    assert sorted(set(sites)) == [("runner.py", "_complete"), ("service.py", "attach_plan")], sites


def test_transition_is_compare_and_set() -> None:
    transition = function(parse(SERVICE), "transition")
    [where] = updates_of(transition)
    assert "Task.state == current" in where and "Task.owner_id == self._owner_id" in where
    body = ast.unparse(transition)
    assert "if moved.rowcount != 1:" in body and "raise _StaleState()" in body
    assert "except _StaleState:" in body and "self._lost_race(task_id)" in body


def test_authorization_is_compare_and_set() -> None:
    authorize = function(parse(SERVICE), "authorize_plan")
    [where] = updates_of(authorize)
    assert "Task.state == TaskState.PLANNED" in where
    assert "Task.authorized_at.is_(None)" in where


def test_a_lost_race_reports_fresh_state_and_never_success() -> None:
    lost = ast.unparse(function(parse(SERVICE), "_lost_race"))
    assert "await self.refresh(task_id)" in lost
    assert "state_changed_concurrently" in lost
    assert "TaskOutcome.UPDATED" not in lost and "TaskOutcome.CANCELLED" not in lost
    refresh = ast.unparse(function(parse(SERVICE), "refresh"))
    assert "populate_existing=True" in refresh and "Task.owner_id == self._owner_id" in refresh


def test_the_runner_writes_state_only_conditionally() -> None:
    tree = parse(RUNNER)
    [mark] = updates_of(function(tree, "mark_task_running"))
    assert "Task.state == TaskState.QUEUED" in mark
    [claim] = updates_of(function(tree, "_claim_check"))
    assert "Task.state.in_([TaskState.QUEUED, TaskState.RUNNING])" in claim
    complete = function(tree, "_complete")
    [still_active] = updates_of(complete)
    assert "Task.state.in_([TaskState.QUEUED, TaskState.RUNNING])" in still_active
    body = ast.unparse(complete)
    assert body.index("still_active.rowcount != 1") < body.index("task.state = TaskState.COMPLETED")


def test_the_runtime_writes_schedules_and_failures_only_while_active() -> None:
    tree = parse(RUNTIME)
    [schedule] = updates_of(function(tree, "_schedule_if_active"))
    assert "Task.state.in_(list(ADVANCEABLE_STATES))" in schedule
    [bump] = updates_of(function(tree, "_record_failure"))
    assert "Task.state.in_(list(ADVANCEABLE_STATES))" in bump
    assert "Task.failure_count == previous" in bump
    record = ast.unparse(function(tree, "_record_failure"))
    assert "if not blocked.ok:" in record
    assert record.index("if not blocked.ok:") < record.index("record_outcome(")
    reschedule = function(tree, "_reschedule")
    # No direct write of a schedule: only through `_schedule_if_active`, or
    # clearing it, which is always safe.
    for n in ast.walk(reschedule):
        if isinstance(n, ast.Assign):
            assert "next_run_at" not in ast.unparse(n.targets[0]), ast.unparse(n)


def test_grants_are_released_with_every_monitor_ending() -> None:
    tree = parse(RUNTIME)
    advance = ast.unparse(function(tree, "advance_claimed_task"))
    met = advance.index("RunnerOutcome.CONDITION_MET")
    assert met < advance.index("release_monitor_grants(session, owner_id)") < advance.index("_reschedule(")
    assert "release_monitor_grants(session, owner_id)" in ast.unparse(function(tree, "_expire_monitor"))
    record = ast.unparse(function(tree, "_record_failure"))
    assert record.index("if not blocked.ok:") < record.index("release_monitor_grants(session, owner_id)")
    assert "release_monitor_grants(self._session, self._owner_id)" in ast.unparse(
        function(parse(REQUESTS), "cancel"))


def test_a_withdrawal_touches_only_a_pending_proposal() -> None:
    discard = ast.unparse(function(parse(REQUESTS), "_discard_by_id"))
    guard = "task.state is not TaskState.PLANNED or task.authorized_at is not None"
    assert guard in discard
    assert discard.index(guard) < discard.index("self._tasks.cancel(")
    assert "return result.ok" in discard
