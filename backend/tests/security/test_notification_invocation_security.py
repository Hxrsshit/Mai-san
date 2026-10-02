"""Stage 6L security: the invocation route is HTTP, and only HTTP.

It is the first production caller of delivery, and the first HTTP route with
an outbound effect outside the execution API, so it is the place a second
path, a policy, an owner parameter or a destination could appear. These
claims say none can: it reaches delivery only through one dependency, takes
only a notification id and an adapter name, calls 6I exactly once, returns
codes only, and is registered once. Every structural claim is an AST walk.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
ROUTE = APP / "api" / "routes" / "notification_delivery.py"
DEPS = APP / "api" / "deps.py"
MAIN = APP / "main.py"


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


def code_strings(tree) -> list:
    """String literals that are code, not documentation."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def function(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def endpoints(tree) -> list:
    """(function, method, path) for every `@router.<method>(path)`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"):
                    found.append((node, d.func.attr, d.args[0].value if d.args else None))
    return found


# ============================================================================
# A. What the route may reach
# ============================================================================


def test_the_route_imports_exactly_http_and_the_result_contract() -> None:
    assert imports(parse(ROUTE)) == {
        "uuid", "fastapi", "pydantic", "app.api.deps", "app.delivery.contract",
    }


def test_the_route_reaches_no_channel_registry_composition_or_machinery() -> None:
    tree = parse(ROUTE)
    for name in ("TelegramNotificationAdapter", "BotApiSender", "SecureHttpClient",
                 "AdapterRegistry", "get_delivery_registry", "build_delivery_registry",
                 "notification_delivery_service", "NotificationDeliveryService",
                 "NotificationService", "record_outcome", "TaskService", "TaskRunner",
                 "BackgroundRuntime", "ExecutionService", "AuthorizationService",
                 "GrantService", "get_settings", "LOCAL_OWNER_ID", "AsyncSession"):
        assert name not in mentioned(tree), name


def test_the_route_names_no_destination_credential_or_message() -> None:
    tree = parse(ROUTE)
    for value in code_strings(tree):
        lowered = value.lower()
        for fragment in ("telegram", "http", "token", "chat", "/bot", "sendmessage", "@"):
            assert fragment not in lowered, value
    assert not {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID", "environ", "getenv"} & mentioned(tree)


# ============================================================================
# B. The request is a notification id and a channel name; nothing else
# ============================================================================


def test_exactly_one_endpoint_one_post_under_its_own_prefix() -> None:
    tree = parse(ROUTE)
    assert [(f.name, m, p) for f, m, p in endpoints(tree)] == [
        ("deliver_notification", "post", "/{notification_id}/deliveries"),
    ]
    [router] = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "router" for t in n.targets)]
    prefix = next(k.value.value for k in router.value.keywords if k.arg == "prefix")
    assert prefix == "/api/task-notifications"


def test_the_endpoint_takes_only_the_id_the_body_and_the_dependency() -> None:
    """No owner, settings, session, header, query or cookie parameter: the
    owner and the destination are never the caller's to name."""
    endpoint = function(parse(ROUTE), "deliver_notification")
    params = [(a.arg, ast.unparse(a.annotation)) for a in endpoint.args.args]
    assert params == [
        ("notification_id", "uuid.UUID"),
        ("request", "DeliveryRequest"),
        ("service", "NotificationDeliveries"),
    ]
    assert not endpoint.args.kwonlyargs and endpoint.args.vararg is None and endpoint.args.kwarg is None
    assert not {"Header", "Query", "Cookie", "Form", "Request", "BackgroundTasks"} & mentioned(parse(ROUTE))


def test_the_body_is_exactly_one_bounded_strict_field_and_forbids_extras() -> None:
    tree = parse(ROUTE)
    model = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DeliveryRequest")
    fields = [s for s in model.body if isinstance(s, ast.AnnAssign)]
    assert [(ast.unparse(f.target), ast.unparse(f.annotation)) for f in fields] == [("adapter", "str")]
    assert "max_length=MAX_ADAPTER_NAME_CHARS" in ast.unparse(fields[0].value)
    [config] = [s for s in model.body if isinstance(s, ast.Assign)
                and ast.unparse(s.targets[0]) == "model_config"]
    keywords = {k.arg: ast.unparse(k.value) for k in config.value.keywords}
    assert keywords == {"extra": "'forbid'", "strict": "True"}


# ============================================================================
# C. One 6I call; codes out; nothing else happens
# ============================================================================


def test_the_endpoint_makes_one_6i_call_with_the_id_and_the_name() -> None:
    endpoint = function(parse(ROUTE), "deliver_notification")
    awaited = [ast.unparse(n.value) for n in ast.walk(endpoint) if isinstance(n, ast.Await)]
    assert awaited == ["service.deliver(notification_id, request.adapter)"]
    body_calls = [name for statement in endpoint.body for name in called(statement)]
    assert sorted(set(body_calls)) == ["HTTPException", "deliver", "get"]


def test_errors_carry_only_6is_reason_code_or_a_fixed_code() -> None:
    endpoint = function(parse(ROUTE), "deliver_notification")
    raises = [n for n in ast.walk(endpoint) if isinstance(n, ast.Raise)]
    assert len(raises) == 2
    details = sorted(ast.unparse(k.value) for r in raises for k in r.exc.keywords if k.arg == "detail")
    assert details == ["result.reason or FAILED_WITHOUT_REASON", "result.reason or REFUSED_WITHOUT_REASON"]
    assert not [n for n in ast.walk(endpoint) if isinstance(n, (ast.JoinedStr, ast.Try))]
    tree = parse(ROUTE)
    constants = {ast.unparse(n.targets[0]): n.value.value for n in tree.body
                 if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)}
    assert constants["FAILED_WITHOUT_REASON"] == "delivery_failed"
    assert constants["REFUSED_WITHOUT_REASON"] == "delivery_refused"


def test_the_route_writes_logs_schedules_and_loops_nothing() -> None:
    tree = parse(ROUTE)
    for forbidden in ("mark_read", "commit", "add", "flush", "execute", "create_task",
                      "ensure_future", "gather", "sleep", "add_task", "info", "warning",
                      "error", "debug", "getattr", "setattr", "eval", "exec", "import_module",
                      "__import__", "compile"):
        assert forbidden not in called(tree), forbidden
    assert "logger" not in mentioned(tree) and "get_logger" not in mentioned(tree)
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]


# ============================================================================
# D. The dependency, and the registration
# ============================================================================


def test_the_dependency_only_asks_the_composition_for_a_service() -> None:
    """One call, one argument: the session. No owner is passed, so the
    composition's own default applies; no registry or adapter is touched."""
    dependency = function(parse(DEPS), "get_notification_delivery_service")
    assert [a.arg for a in dependency.args.args] == ["session"]
    [ret] = [n for n in ast.walk(dependency) if isinstance(n, ast.Return)]
    assert ast.unparse(ret.value) == "notification_delivery_service(session)"
    assert called(dependency) == ["notification_delivery_service"]
    tree = parse(DEPS)
    assert "NotificationDeliveryService" not in [
        n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert not {"get_delivery_registry", "build_delivery_registry", "AdapterRegistry",
                "TelegramNotificationAdapter", "BotApiSender"} & mentioned(tree)


def test_the_router_is_included_once_and_unconditionally() -> None:
    tree = parse(MAIN)
    includes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n) == "app.include_router(notification_delivery_router)"]
    assert len(includes) == 1
    create_app = function(tree, "create_app")
    top_level = [ast.unparse(s) for s in create_app.body]
    assert "app.include_router(notification_delivery_router)" in top_level      # not inside an `if`


def test_exactly_one_route_in_the_application_triggers_delivery() -> None:
    triggers = []
    for path in (APP / "api").rglob("*.py"):
        for node, method, route in endpoints(parse(path)):
            if "deliver" in node.name or (route and "deliver" in route):
                triggers.append((str(path.relative_to(BACKEND)), method, route))
    assert triggers == [("app/api/routes/notification_delivery.py", "post", "/{notification_id}/deliveries")]


def test_the_task_api_is_still_read_only() -> None:
    methods = {m for _, m, _ in endpoints(parse(APP / "api" / "routes" / "tasks.py"))}
    assert methods == {"get"}


def test_6l_adds_no_migration_or_table() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("0*.py"))
    assert versions[-1] == "0018_task_notifications.py"
