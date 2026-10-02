"""Stage 6K security: the composition root is wiring, and only wiring.

It is the one place production code assembles delivery, so it is the one
place that could quietly grow a policy, a second path or a trigger. These
claims say it cannot: it reaches only the existing components, builds each
once, decides nothing about when delivery happens, makes no request itself,
and nothing in the application calls it yet. Every structural claim is an
AST walk.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
ROOT = APP / "composition" / "notification_delivery.py"
PACKAGE = APP / "composition"


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


def code_strings(tree) -> list:
    """String literals that are code, not documentation: docstrings excluded."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def mentioned(tree) -> set:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return names | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


# ============================================================================
# A. What the composition root may reach
# ============================================================================


def test_the_composition_package_has_exactly_these_modules() -> None:
    assert sorted(p.name for p in PACKAGE.glob("*.py")) == [
        "__init__.py", "notification_delivery.py",
    ]


def test_the_root_imports_exactly_the_existing_components() -> None:
    assert imports(parse(ROOT)) == {
        "threading", "uuid", "typing", "sqlalchemy.ext.asyncio",
        "app.core.config", "app.core.logging",
        "app.delivery.registry", "app.delivery.service",
        "app.tasks.models", "app.telegram.notifier",
    }


def test_the_root_reaches_no_telegram_transport_and_no_http() -> None:
    """It composes the 6J adapter; the adapter owns the sender, the sender
    owns the client. The root names none of them."""
    tree = parse(ROOT)
    for module in imports(tree):
        assert not module.startswith(("app.telegram.client", "app.integrations",
                                      "httpx", "requests", "aiohttp", "urllib",
                                      "socket", "ssl", "http")), module
    for name in ("BotApiSender", "SecureHttpClient", "NetworkPolicy",
                 "TELEGRAM_API_HOST", "send_text", "post_json"):
        assert name not in mentioned(tree), name
    assert not [v for v in code_strings(tree)
                if "api.telegram.org" in v or "sendMessage" in v or "/bot" in v]


def test_the_root_reaches_no_task_runtime_execution_or_model_machinery() -> None:
    tree = parse(ROOT)
    for module in imports(tree):
        assert not module.startswith((
            "app.tasks.runner", "app.tasks.monitoring", "app.tasks.service",
            "app.tasks.notifications", "app.background", "app.execution",
            "app.tools", "app.authorization", "app.llm", "app.memory",
            "app.api", "app.services", "app.reminders",
        )), module
    for name in ("TaskRunner", "BackgroundRuntime", "ExecutionService", "Dispatcher",
                 "AuthorizationService", "GrantService", "record_outcome",
                 "TaskNotification", "LocalRecordingAdapter"):
        assert name not in mentioned(tree), name


def test_the_root_reads_no_environment_and_no_telegram_setting_itself() -> None:
    """One configuration path: `get_settings()`, handed whole to the 6J
    adapter, which reads its own two fields."""
    tree = parse(ROOT)
    assert "os" not in imports(tree)
    assert not {"environ", "getenv", "open", "read_text"} & mentioned(tree)
    assert not [n for n in mentioned(tree) if n.startswith("TELEGRAM_")]
    assert called(tree).count("get_settings") == 1


# ============================================================================
# B. Built once; no trigger, worker, retry or loop
# ============================================================================


def test_each_component_is_constructed_exactly_once_in_the_builder() -> None:
    tree = parse(ROOT)
    builder = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "build_delivery_registry")
    inside = called(builder)
    assert inside.count("AdapterRegistry") == 1
    assert inside.count("TelegramNotificationAdapter") == 1
    assert inside.count("register") == 1 and inside.count("seal") == 1
    everywhere = called(tree)
    assert everywhere.count("AdapterRegistry") == 1
    assert everywhere.count("TelegramNotificationAdapter") == 1
    assert everywhere.count("build_delivery_registry") == 1     # only the cached getter


def test_telegram_is_registered_only_behind_its_configured_check_and_then_sealed() -> None:
    tree = parse(ROOT)
    builder = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "build_delivery_registry")
    [guard] = [n for n in builder.body if isinstance(n, ast.If)]
    assert ast.unparse(guard.test) == "telegram.configured"
    assert [ast.unparse(s) for s in guard.body] == ["registry.register(telegram)"]
    statements = [ast.unparse(s) for s in builder.body]
    assert statements.index("registry.seal()") > statements.index(ast.unparse(guard))


def test_the_cache_is_built_under_a_lock_with_a_double_check() -> None:
    tree = parse(ROOT)
    getter = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "get_delivery_registry")
    text = ast.unparse(getter)
    assert "with _build_lock:" in text
    assert text.count("if _registry is None:") == 2


def test_the_root_decides_nothing_about_when_delivery_happens() -> None:
    tree = parse(ROOT)
    names = called(tree)
    for forbidden in ("deliver", "create_task", "ensure_future", "gather", "sleep",
                      "call_later", "run_in_executor", "Thread", "Timer", "start",
                      "wait_for", "unread", "for_task", "mark_read"):
        assert forbidden not in names, forbidden
    assert not [n for n in ast.walk(tree)
                if isinstance(n, (ast.While, ast.For, ast.AsyncFor, ast.AsyncFunctionDef))]


def test_the_root_has_no_dynamic_or_dangerous_call() -> None:
    names = set(called(parse(ROOT)))
    assert not names & {"eval", "exec", "compile", "__import__", "import_module",
                        "getattr", "setattr", "system", "popen", "Popen", "run"}


def test_only_the_api_dependency_calls_the_composition() -> None:
    """Changed deliberately in 6L. 6K made delivery constructible and nothing
    imported it. 6L's person-driven route is the trigger, and it reaches the
    composition only through one FastAPI dependency in `deps.py`. A worker,
    the runtime, the runner, a startup hook or a second route importing it
    still fails here."""
    importers = []
    for path in APP.rglob("*.py"):
        if PACKAGE in path.parents:
            continue
        if any(m == "app.composition" or m.startswith("app.composition.")
               for m in imports(parse(path))):
            importers.append(str(path.relative_to(BACKEND)))
    assert importers == ["app/api/deps.py"]


def test_only_the_6l_route_reaches_delivery_and_never_the_lifespan() -> None:
    """Changed deliberately in 6L. `deps.py` may call the composition's
    service factory (never the registry), and the 6L route may use only that
    dependency. `main.py` and every other route reach none of it."""
    allowed = {
        "deps.py": ({"app.composition.notification_delivery", "app.delivery.service"},
                    {"notification_delivery_service", "NotificationDeliveryService"}),
        "routes/notification_delivery.py": ({"app.delivery.contract"}, set()),
    }
    for path in list((APP / "api").rglob("*.py")) + [APP / "main.py"]:
        tree = parse(path)
        relative = path.relative_to(APP / "api").as_posix() if APP / "api" in path.parents else "main.py"
        reached = {m for m in imports(tree)
                   if m.startswith(("app.composition", "app.delivery", "app.telegram.notifier"))}
        named = {"notification_delivery_service", "get_delivery_registry",
                 "build_delivery_registry", "NotificationDeliveryService"} & mentioned(tree)
        modules, names = allowed.get(relative, (set(), set()))
        assert reached == modules, (relative, reached)
        assert named == names, (relative, named)


# ============================================================================
# C. One of everything, application-wide
# ============================================================================


def _definitions(name: str) -> list:
    found = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(parse(path)):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
                found.append(str(path.relative_to(BACKEND)))
    return found


def test_exactly_one_of_each_delivery_component_exists() -> None:
    assert _definitions("NotificationDeliveryService") == ["app/delivery/service.py"]
    assert _definitions("AdapterRegistry") == ["app/delivery/registry.py"]
    assert _definitions("TelegramNotificationAdapter") == ["app/telegram/notifier.py"]
    assert _definitions("BotApiSender") == ["app/telegram/client.py"]
    assert _definitions("build_delivery_registry") == ["app/composition/notification_delivery.py"]
    assert _definitions("get_delivery_registry") == ["app/composition/notification_delivery.py"]


def test_the_only_production_service_construction_is_the_roots() -> None:
    sites = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(parse(path)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "NotificationDeliveryService"):
                sites.append(str(path.relative_to(BACKEND)))
    assert sites == ["app/composition/notification_delivery.py"]


def test_the_root_logs_only_the_adapter_names() -> None:
    tree = parse(ROOT)
    logged = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            logged += 1
            extra = next(k.value for k in node.keywords if k.arg == "extra")
            assert [k.value for k in extra.keys] == ["adapters"]
            assert "registry.names()" in ast.unparse(extra.values[0])
    assert logged == 1


def test_6k_adds_no_migration_or_table() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("0*.py"))
    # Changed deliberately in 6M.1: the next migration after 6H's 0018 is
    # 6M.1's delivery-records table, and nothing else.
    assert [v for v in versions if v[:4] > "0018"] == ["0019_notification_deliveries.py"]
