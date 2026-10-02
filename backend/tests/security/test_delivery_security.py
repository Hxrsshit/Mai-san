"""Stage 6I security: delivery reads a notification and hands it on. Nothing else.

The delivery boundary is where Mai's records will one day meet the outside
world, so the claims here are about what it cannot do: create or change a
notification, reach the task, execution or authorization machinery, carry
content, call a model or the network, run a loop, or be reached by a request
or by content. Every structural claim is an AST walk.
"""

import ast
import inspect
from pathlib import Path

from app.delivery.contract import DeliveryPayload, NotificationAdapter
from app.delivery.registry import AdapterRegistry
from app.delivery.service import NotificationDeliveryService

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
PACKAGE = APP / "delivery"
MODULES = sorted(PACKAGE.glob("*.py"))


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


def package_tree():
    return [(p, parse(p)) for p in MODULES]


# ============================================================================
# A. The package, and what it may reach
# ============================================================================


def test_the_delivery_package_has_exactly_these_modules() -> None:
    """Guards everything below: an empty glob passes vacuously."""
    assert [p.name for p in MODULES] == [
        "__init__.py", "contract.py", "local.py", "registry.py", "service.py",
    ]


def test_the_package_imports_only_the_notification_abstraction() -> None:
    allowed = {
        "asyncio", "enum", "re", "uuid", "datetime", "typing",
        "pydantic", "sqlalchemy.ext.asyncio",
        "app.core.logging", "app.tasks.models", "app.tasks.notifications",
        "app.delivery.contract", "app.delivery.registry",
    }
    for path, tree in package_tree():
        assert imports(tree) <= allowed, (path.name, imports(tree) - allowed)


def test_the_package_reaches_no_execution_authorization_runtime_or_model() -> None:
    forbidden = (
        "app.tasks.runner", "app.tasks.monitoring", "app.tasks.service",
        "app.background", "app.execution", "app.tools", "app.authorization",
        "app.llm", "app.memory", "app.entities", "app.relationships",
        "app.knowledge", "app.retrieval", "app.context", "app.prompt",
        "app.integrations", "app.telegram", "app.api", "app.services",
        "app.reminders", "app.workflows",
        "subprocess", "importlib", "os", "socket", "ssl", "http", "urllib",
        "httpx", "requests", "aiohttp", "smtplib", "telegram", "pickle",
        "marshal", "ctypes", "threading", "multiprocessing",
    )
    for path, tree in package_tree():
        for module in imports(tree):
            assert not module.startswith(forbidden), (path.name, module)


def test_the_package_names_no_runner_service_dispatcher_or_grant() -> None:
    names = set()
    for _, tree in package_tree():
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in (
        "TaskRunner", "BackgroundRuntime", "ExecutionService", "Dispatcher",
        "AuthorizationService", "GrantService", "TaskService", "evaluate",
        "parse_spec", "LLMProvider", "get_llm_provider", "SecureHttpClient",
    ):
        assert forbidden not in names, forbidden


def test_the_package_has_no_dynamic_or_dangerous_call() -> None:
    for path, tree in package_tree():
        names = called(tree)
        # `compile` appears only as `re.compile` of a literal pattern.
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and (
                getattr(n.func, "id", None) == "compile"
                or getattr(n.func, "attr", None) == "compile"
            ):
                assert ast.unparse(n.func) == "re.compile", path.name
                assert isinstance(n.args[0], ast.Constant), path.name
        for forbidden in (
            "eval", "exec", "__import__", "import_module", "getattr",
            "setattr", "system", "popen", "Popen", "run", "create_task",
            "ensure_future", "gather", "sleep", "dispatch", "approve",
            "authorize", "authorize_with_grants", "generate_response",
        ):
            assert forbidden not in names, (path.name, forbidden)


def test_the_package_has_no_loop_and_no_retry() -> None:
    for path, tree in package_tree():
        assert not [n for n in ast.walk(tree) if isinstance(n, (ast.While, ast.For))], path.name
    service = parse(PACKAGE / "service.py")
    deliver_calls = [
        n for n in ast.walk(service)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "deliver"
    ]
    assert len(deliver_calls) == 1          # one attempt per call


# ============================================================================
# B. Delivery reads; it never writes and never creates a notification
# ============================================================================


def test_the_package_never_writes_to_the_database() -> None:
    for path, tree in package_tree():
        names = called(tree)
        for forbidden in (
            "add", "add_all", "flush", "commit", "delete", "merge", "execute",
            "update", "insert", "begin", "begin_nested", "refresh",
        ):
            assert forbidden not in names, (path.name, forbidden)


def test_the_package_never_creates_or_marks_a_notification() -> None:
    for path, tree in package_tree():
        names = called(tree)
        assert "TaskNotification" not in names, path.name
        assert "record_outcome" not in names, path.name
        assert "mark_read" not in names, path.name


def test_the_only_read_is_the_owner_scoped_get() -> None:
    service = parse(PACKAGE / "service.py")
    used = {
        n.func.attr for n in ast.walk(service)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "_notifications"
    }
    assert used == {"get"}
    constructs = [
        ast.unparse(n) for n in ast.walk(service)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "NotificationService"
    ]
    assert constructs == ["NotificationService(session, owner_id)"]


def test_the_6h_writer_is_still_the_only_writer() -> None:
    """6I adds a reader. The 6H single-writer pin must still hold."""
    writers = set()
    for path in APP.rglob("*.py"):
        for fn in ast.walk(parse(path)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "TaskNotification":
                        writers.add((str(path.relative_to(BACKEND)), fn.name))
    assert writers == {("app/tasks/notifications.py", "record_outcome")}


# ============================================================================
# C. What an adapter is given
# ============================================================================


def test_the_payload_fields_are_exactly_these() -> None:
    assert sorted(DeliveryPayload.model_fields) == [
        "check_number", "created_at", "delivery_key", "kind", "notification_id",
        "task_id",
    ]
    for name in DeliveryPayload.model_fields:
        for leak in ("objective", "argument", "owner", "token", "secret", "key_",
                     "content", "text", "message", "title", "prompt", "result",
                     "chat", "url", "channel", "recipient"):
            assert leak not in name, name


def test_the_payload_is_closed_and_frozen() -> None:
    config = DeliveryPayload.model_config
    assert config["extra"] == "forbid"
    assert config["frozen"] is True
    assert config["strict"] is True


def test_an_adapter_is_given_the_payload_and_nothing_else() -> None:
    assert list(inspect.signature(NotificationAdapter.deliver).parameters) == [
        "self", "payload",
    ]
    service = parse(PACKAGE / "service.py")
    [call] = [
        n for n in ast.walk(service)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "deliver"
    ]
    assert [ast.unparse(a) for a in call.args] == ["payload"]
    assert call.keywords == []


def test_the_payload_is_built_from_the_notification_record_only() -> None:
    service = parse(PACKAGE / "service.py")
    [build] = [
        n for n in ast.walk(service)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "DeliveryPayload"
    ]
    assert {k.arg: ast.unparse(k.value) for k in build.keywords} == {
        "notification_id": "notification.id",
        "task_id": "notification.task_id",
        "kind": "notification.kind",
        "check_number": "notification.check_number",
        "created_at": "notification.created_at",
        "delivery_key": "key",
    }


def test_the_service_takes_no_channel_recipient_or_content_parameter() -> None:
    assert list(inspect.signature(NotificationDeliveryService.deliver).parameters) == [
        "self", "notification_id", "adapter_name",
    ]
    assert list(inspect.signature(NotificationDeliveryService.__init__).parameters) == [
        "self", "session", "owner_id", "registry", "timeout_seconds",
    ]


# ============================================================================
# D. Errors and logs
# ============================================================================


def test_an_adapter_exception_is_never_bound_or_rendered() -> None:
    """`except ... as exc` would invite `str(exc)` -- a channel's error can
    carry a token or an endpoint. No handler in the service names one."""
    service = parse(PACKAGE / "service.py")
    handlers = [n for n in ast.walk(service) if isinstance(n, ast.ExceptHandler)]
    assert handlers and all(h.name is None for h in handlers)


def test_logs_carry_only_ids_names_outcomes_and_reasons() -> None:
    allowed = {"notification_id", "adapter", "outcome", "reason"}
    for path, tree in package_tree():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"
            ):
                extra = next((k.value for k in node.keywords if k.arg == "extra"), None)
                if extra is not None:
                    assert {k.value for k in extra.keys} <= allowed, (path.name, node.lineno)


# ============================================================================
# E. Nothing reaches the boundary from a request or from content
# ============================================================================


def test_only_the_channel_and_the_composition_root_import_delivery() -> None:
    """Changed deliberately three times, and an exact allow-list each time.

    Through 6I nothing outside `app/delivery` imported it, because no channel
    existed. 6J's Telegram adapter had to implement the contract, so it was
    allowed exactly `app.delivery.contract`. 6K's composition root has to
    build the registry and the service, so it is allowed exactly those two.
    6L's invocation route reaches delivery through `deps.py`, which names the
    service type for its dependency, and the route reads the result contract.
    Neither may reach the registry. Any other importer, a channel reaching the
    service or registry, or one of these reaching anything else in the
    package, still fails.
    """
    importers = {}
    for path in APP.rglob("*.py"):
        if PACKAGE in path.parents:
            continue
        used = {
            module for module in imports(parse(path))
            if module == "app.delivery" or module.startswith("app.delivery.")
        }
        if used:
            importers[str(path.relative_to(BACKEND))] = used
    assert importers == {
        "app/telegram/notifier.py": {"app.delivery.contract"},
        "app/composition/notification_delivery.py": {
            "app.delivery.registry", "app.delivery.service",
        },
        "app/api/deps.py": {"app.delivery.service"},
        "app/api/routes/notification_delivery.py": {"app.delivery.contract"},
    }, importers


def test_only_the_6l_route_triggers_delivery() -> None:
    """Changed deliberately in 6L: one route may trigger delivery, and only
    through the dependency. No HTTP module constructs the service or names
    the registry, and every other route still cannot reach delivery at all."""
    for path in (APP / "api").rglob("*.py"):
        tree = parse(path)
        source = path.read_text(encoding="utf-8")
        assert "AdapterRegistry" not in source, path.name
        assert not [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "NotificationDeliveryService"], path.name
        relative = path.relative_to(APP / "api").as_posix()
        if relative in ("deps.py", "routes/notification_delivery.py"):
            continue
        for name in ("NotificationDeliveryService", "NotificationDeliveries",
                     "app.delivery", "deliver("):
            assert name not in source, (path.name, name)


def test_only_the_composition_root_builds_or_fills_a_registry() -> None:
    """Changed deliberately in 6K, and an exact allow-list.

    In 6I no production code registered an adapter. 6K's composition root
    builds the one process registry, so it is the one module outside the
    package that may name `AdapterRegistry`, and the only `register` call it
    makes is the Telegram adapter's, once. A second composer, a second
    registry, or a registration anywhere else still fails here.
    """
    namers, registrations = set(), []
    for path in APP.rglob("*.py"):
        if PACKAGE in path.parents:
            continue
        tree = parse(path)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        if "AdapterRegistry" in names:
            namers.add(str(path.relative_to(BACKEND)))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "registry"):
                registrations.append((str(path.relative_to(BACKEND)), ast.unparse(node.args[0])))
    assert namers == {"app/composition/notification_delivery.py"}, namers
    assert registrations == [("app/composition/notification_delivery.py", "telegram")], registrations


def test_the_registry_resolves_names_only_never_code() -> None:
    tree = parse(PACKAGE / "registry.py")
    assert not {"getattr", "import_module", "__import__", "eval"} & called(tree)
    public = {
        name for name, _ in inspect.getmembers(AdapterRegistry, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {"canonical", "register", "seal", "get", "names"}


def test_the_local_adapter_reaches_nothing() -> None:
    assert imports(parse(PACKAGE / "local.py")) == {"typing", "app.delivery.contract"}
