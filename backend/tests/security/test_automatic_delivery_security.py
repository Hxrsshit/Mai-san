"""Stage 6M.3 security: automatic delivery is a bounded caller of 6I, nothing more.

It reaches delivery only through the 6K composition's service factory, reads
its candidates through the read-only reader, never constructs a channel,
sender or record, never retries, and is opt-in. The runtime keeps its own
invariants: no owner constant, no per-item tasks, no request identity. Every
structural claim is an AST walk.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
AUTO = APP / "background" / "deliveries.py"
RUNTIME = APP / "background" / "runtime.py"
REPO = BACKEND.parent


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))


def imports(tree) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def called(tree) -> list:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.append(node.func.attr)
    return names


def mentioned(tree) -> set:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return names | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def function(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


# ============================================================================
# A. What the work source may reach
# ============================================================================


def test_the_work_source_imports_exactly_these() -> None:
    assert imports(parse(AUTO)) == {
        "datetime", "typing",
        "app.composition.notification_delivery", "app.core.config", "app.core.logging",
        "app.delivery.contract", "app.delivery.status", "app.tasks.models",
    }


def test_it_constructs_no_channel_sender_record_or_registry() -> None:
    tree = parse(AUTO)
    for name in ("TelegramNotificationAdapter", "BotApiSender", "SecureHttpClient",
                 "AdapterRegistry", "NotificationDelivery", "NotificationDeliveryService",
                 "build_delivery_registry", "records", "claim", "finish", "register", "seal",
                 "httpx", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID"):
        assert name not in mentioned(tree), name


def test_it_writes_nothing_to_the_database_itself() -> None:
    names = set(called(parse(AUTO)))
    assert not {"add", "add_all", "commit", "execute", "flush", "delete", "merge",
                "update", "insert", "rollback", "scalars", "select"} & names


def test_every_send_goes_through_the_composition_factory_and_6i() -> None:
    run = function(parse(AUTO), "run_automatic_deliveries")
    sends = [n for n in ast.walk(run) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "deliver"]
    assert len(sends) == 1
    assert ast.unparse(sends[0]) == (
        "notification_delivery_service(session, DELIVERY_OWNER_ID).deliver(notification_id, channel)")
    reads = [ast.unparse(n) for n in ast.walk(run) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "awaiting_first_attempt"]
    assert reads == ["DeliveryStatusReader(session, DELIVERY_OWNER_ID).awaiting_first_attempt(channel, since, budget)"]


def test_only_registered_channels_are_used_and_none_are_registered() -> None:
    channels = function(parse(AUTO), "usable_channels")
    assert "get_delivery_registry().names()" in ast.unparse(channels)
    assert "register" not in called(parse(AUTO))


def test_the_delivery_owner_is_one_named_constant() -> None:
    tree = parse(AUTO)
    owners = [n for n in tree.body if isinstance(n, ast.Assign)
              and ast.unparse(n.targets[0]) == "DELIVERY_OWNER_ID"]
    assert [ast.unparse(o.value) for o in owners] == ["LOCAL_OWNER_ID"]
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "LOCAL_OWNER_ID"]
    assert len(uses) == 1                     # only in that assignment
    assert not [m for m in imports(tree) if m.startswith(("app.api", "fastapi", "starlette"))]


# ============================================================================
# B. Bounded, sequential, never retried, opt-in
# ============================================================================


def test_the_per_tick_cap_is_a_code_constant_of_three() -> None:
    tree = parse(AUTO)
    [cap] = [n for n in tree.body if isinstance(n, ast.Assign)
             and ast.unparse(n.targets[0]) == "HARD_MAX_DELIVERIES_PER_TICK"]
    assert ast.unparse(cap.value) == "3"
    run = ast.unparse(function(tree, "run_automatic_deliveries"))
    assert "budget = HARD_MAX_DELIVERIES_PER_TICK" in run
    assert "if budget <= 0:" in run and "budget -= 1" in run


def test_nothing_is_spawned_and_nothing_loops_unbounded() -> None:
    tree = parse(AUTO)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.While)]
    assert not {"create_task", "ensure_future", "gather", "sleep", "Thread", "wait_for"} & set(called(tree))


def test_each_candidate_is_attempted_once_per_pass() -> None:
    """One `deliver` per candidate, inside the loop over the candidates, with
    no retry around it."""
    run = function(parse(AUTO), "run_automatic_deliveries")
    loops = [n for n in ast.walk(run) if isinstance(n, ast.For)]
    inner = [l for l in loops if ast.unparse(l.target) == "notification_id"]
    assert len(inner) == 1 and ast.unparse(inner[0].iter) == "candidates"
    assert not [n for n in ast.walk(inner[0]) if isinstance(n, (ast.For, ast.While, ast.Try))
                and n is not inner[0]]


def test_the_candidate_query_excludes_any_existing_record() -> None:
    """No retry, by construction: a candidate has no delivery record of any
    status for the channel -- not filtered by status."""
    reader = ast.unparse(function(parse(APP / "delivery" / "status.py"), "awaiting_first_attempt"))
    assert "NotificationDelivery.notification_id == TaskNotification.id" in reader
    assert "NotificationDelivery.adapter == adapter" in reader
    assert "~attempted" in reader
    assert "status" not in reader.split("attempted = ")[1].split(".exists()")[0]


def test_the_setting_is_off_by_default_and_names_only() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).NOTIFICATION_AUTO_DELIVER_ADAPTERS == []
    run = ast.unparse(function(parse(AUTO), "run_automatic_deliveries"))
    assert run.split("\n")[2].strip().startswith("if not settings.NOTIFICATION_AUTO_DELIVER_ADAPTERS")


def test_compose_and_the_example_pass_it_empty() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "NOTIFICATION_AUTO_DELIVER_ADAPTERS: ${NOTIFICATION_AUTO_DELIVER_ADAPTERS:-}" in compose
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "\nNOTIFICATION_AUTO_DELIVER_ADAPTERS=\n" in example


# ============================================================================
# C. The runtime keeps its invariants
# ============================================================================


def test_the_runtime_reaches_delivery_only_through_the_work_source() -> None:
    tree = parse(RUNTIME)
    reached = {m for m in imports(tree) if m.startswith(("app.delivery", "app.composition", "app.telegram"))}
    assert reached == set()
    assert "app.background.deliveries" in imports(tree)
    assert not {"notification_delivery_service", "get_delivery_registry", "deliver"} & set(called(tree))


def test_the_boundary_is_set_once_at_construction_and_never_a_parameter() -> None:
    tree = parse(RUNTIME)
    init = function(tree, "__init__")
    assert [a.arg for a in init.args.args] == ["self", "session_factory", "settings"]
    writes = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
              and "_deliveries_since" in ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target)]
    assert len(writes) == 1 and writes[0] in list(ast.walk(init))
    assert "datetime.now(timezone.utc) if self.deliveries_enabled else None" in ast.unparse(writes[0])


def test_the_runtime_passes_its_boundary_and_contains_failures() -> None:
    tick = ast.unparse(function(parse(RUNTIME), "tick"))
    assert "since=self._deliveries_since" in tick
    assert "Automatic delivery pass failed" in tick
    assert tick.index("run_due_tasks") < tick.index("run_automatic_deliveries")   # after tasks
