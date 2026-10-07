"""Stage 6M.2 security: the read API is a thin adapter, and reads only.

The owner is resolved in one place, every query is owner-scoped in the
service that owns it, delivery state is read without touching the 6M.1
writer, and no response model can carry anything but references, times and
codes. Every structural claim is an AST walk.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
ROUTE = APP / "api" / "routes" / "notification_delivery.py"
DEPS = APP / "api" / "deps.py"
STATUS = APP / "delivery" / "status.py"
NOTIFICATIONS = APP / "tasks" / "notifications.py"


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


def model_fields(tree, name) -> list:
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name)
    return [ast.unparse(s.target) for s in cls.body if isinstance(s, ast.AnnAssign)]


def model_config(tree, name) -> dict:
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name)
    [config] = [s for s in cls.body if isinstance(s, ast.Assign)
                and ast.unparse(s.targets[0]) == "model_config"]
    return {k.arg: ast.unparse(k.value) for k in config.value.keywords}


# ============================================================================
# A. One owner seam
# ============================================================================


def test_the_owner_is_resolved_in_one_function_from_the_credential_only() -> None:
    """Changed deliberately in 6M.4: the seam now takes the settings and the
    `Authorization` header -- and nothing else a caller could use to name an
    owner. The owner it returns is whatever `core.auth` authenticated."""
    owner = function(parse(DEPS), "get_owner_id")
    assert [(a.arg, ast.unparse(a.annotation)) for a in owner.args.args] == [
        ("settings", "AppSettings"), ("authorization", "Optional[str]")]
    assert [ast.unparse(d) for d in owner.args.defaults] == ["Header(default=None)"]
    assert not owner.args.kwonlyargs
    [ret] = [n for n in ast.walk(owner) if isinstance(n, ast.Return)]
    assert ast.unparse(ret.value) == "owner_id"
    assigned = [ast.unparse(n.value) for n in ast.walk(owner)
                if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "owner_id"]
    assert assigned == ["auth.authenticate(token, settings)"]


def test_the_read_dependencies_bind_the_seams_owner_and_nothing_else() -> None:
    deps = parse(DEPS)
    for name, built in (("get_notification_service", "NotificationService(session, owner_id)"),
                        ("get_delivery_status_reader", "DeliveryStatusReader(session, owner_id)")):
        fn = function(deps, name)
        assert [(a.arg, ast.unparse(a.annotation)) for a in fn.args.args] == [
            ("session", "DbSession"), ("owner_id", "OwnerId")]
        [ret] = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        assert ast.unparse(ret.value) == built


def test_no_route_takes_an_owner() -> None:
    for path in (APP / "api" / "routes").rglob("*.py"):
        for fn in ast.walk(parse(path)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
                assert not [p for p in params if "owner" in p], (path.name, fn.name, params)


def test_the_read_services_are_built_only_where_expected() -> None:
    """The 6H read service: in `deps.py` for the API, and inside 6I for its
    owner-scoped read. The delivery-state reader: only in `deps.py`."""
    sites = {"NotificationService": [], "DeliveryStatusReader": []}
    for path in APP.rglob("*.py"):
        for n in ast.walk(parse(path)):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) in sites:
                sites[n.func.id].append(str(path.relative_to(BACKEND)))
    assert sorted(sites["NotificationService"]) == ["app/api/deps.py", "app/delivery/service.py"]
    # Stage 6M.3 adds the automatic-delivery work source, for its candidates.
    assert sorted(sites["DeliveryStatusReader"]) == ["app/api/deps.py", "app/background/deliveries.py"]


# ============================================================================
# B. The endpoints: thin, bounded, owner-free
# ============================================================================


def test_each_read_endpoint_takes_exactly_these_parameters() -> None:
    route = parse(ROUTE)
    expected = {
        "list_task_notifications": [
            ("notifications", "TaskNotifications"), ("deliveries", "DeliveryStatuses"),
            ("limit", "int"), ("before", "Optional[uuid.UUID]"), ("unread_only", "bool")],
        "get_task_notification": [
            ("notification_id", "uuid.UUID"), ("notifications", "TaskNotifications"),
            ("deliveries", "DeliveryStatuses")],
        "mark_task_notification_read": [
            ("notification_id", "uuid.UUID"), ("request", "MarkReadRequest"),
            ("notifications", "TaskNotifications")],
    }
    for name, params in expected.items():
        fn = function(route, name)
        assert [(a.arg, ast.unparse(a.annotation)) for a in fn.args.args] == params, name


def test_the_list_limit_is_bounded_by_the_6h_ceiling() -> None:
    fn = function(parse(ROUTE), "list_task_notifications")
    limit = fn.args.defaults[fn.args.args.index(next(a for a in fn.args.args if a.arg == "limit"))
                             - (len(fn.args.args) - len(fn.args.defaults))]
    keywords = {k.arg: ast.unparse(k.value) for k in limit.keywords}
    assert keywords == {"default": "DEFAULT_LISTED", "ge": "1", "le": "MAX_LISTED"}


def test_the_endpoints_only_delegate_to_the_owner_bound_services() -> None:
    """Every service call in a read endpoint is on `notifications` (6H) or
    `deliveries` (the reader); none touches the session, the writer, the
    composition or a model directly."""
    route = parse(ROUTE)
    allowed = {
        "list_task_notifications": {"notifications.page", "deliveries.for_notifications"},
        "get_task_notification": {"notifications.get", "deliveries.for_notifications"},
        "mark_task_notification_read": {"notifications.mark_read", "notifications.get"},
    }
    for name, calls in allowed.items():
        fn = function(route, name)
        awaited = {ast.unparse(n.value.func) for n in ast.walk(fn)
                   if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)}
        assert awaited == calls, (name, awaited)
    assert not {"records", "claim", "finish", "notification_delivery_service",
                "get_delivery_registry", "session", "select", "update"} & mentioned(route)


def test_response_models_carry_only_the_contract_fields() -> None:
    route, status = parse(ROUTE), parse(STATUS)
    assert model_fields(route, "TaskNotificationRead") == [
        "id", "task_id", "kind", "check_number", "execution_id", "created_at", "read_at",
        "deliveries"]
    assert model_fields(route, "TaskNotificationList") == ["notifications", "next_before"]
    assert model_fields(status, "DeliveryState") == [
        "adapter", "status", "attempts", "last_attempt_at", "delivered_at", "retryable"]
    for tree, name in ((route, "TaskNotificationRead"), (route, "TaskNotificationList"),
                       (status, "DeliveryState")):
        assert model_config(tree, name)["extra"] == "'forbid'", name


def test_mark_read_requires_an_empty_strict_json_object() -> None:
    route = parse(ROUTE)
    assert model_fields(route, "MarkReadRequest") == []
    assert model_config(route, "MarkReadRequest") == {"extra": "'forbid'", "strict": "True"}


# ============================================================================
# C. Delivery state is read, never written
# ============================================================================


def test_the_status_module_reaches_only_the_record_model() -> None:
    assert imports(parse(STATUS)) == {
        "uuid", "datetime", "typing", "pydantic", "sqlalchemy", "sqlalchemy.ext.asyncio",
        "app.delivery.models",
        # Stage 6M.3: the candidate query reads notification ids and times.
        "app.tasks.models",
    }


def test_the_status_modules_only_database_calls_are_owner_scoped_selects() -> None:
    """Changed deliberately in 6M.3: a second read, the automatic-delivery
    candidates. Both are SELECTs, both filtered on the reader's owner."""
    tree = parse(STATUS)
    writes = {"add", "add_all", "flush", "commit", "delete", "merge", "execute", "update",
              "insert", "begin", "rollback", "refresh"}
    assert not writes & set(called(tree))
    scalars = {fn.name: [ast.unparse(n.args[0]) for n in ast.walk(fn) if isinstance(n, ast.Call)
                         and getattr(n.func, "attr", None) == "scalars"]
               for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef)}
    scalars = {k: v for k, v in scalars.items() if v}
    assert sorted(scalars) == ["awaiting_first_attempt", "for_notifications"]
    [states] = scalars["for_notifications"]
    assert states.startswith("select(NotificationDelivery)")
    assert "NotificationDelivery.owner_id == self._owner_id" in states
    assert "NotificationDelivery.notification_id.in_(ids)" in states
    [candidates] = scalars["awaiting_first_attempt"]
    assert candidates.startswith("select(TaskNotification.id)")
    assert "TaskNotification.owner_id == self._owner_id" in candidates
    assert "TaskNotification.created_at >= created_since" in candidates
    assert "~attempted" in candidates and ".limit(limit)" in candidates


def test_retryable_restates_the_writers_reclaim_condition() -> None:
    """`retryable` must be exactly when `records.claim` would grant a new
    claim: `failed`, or `sending` whose lease has passed."""
    records = ast.unparse(parse(APP / "delivery" / "records.py"))
    assert "NotificationDelivery.status == DeliveryRecordStatus.FAILED" in records
    assert "NotificationDelivery.lease_expires_at < moment" in records
    retryable = ast.unparse(function(parse(STATUS), "is_retryable"))
    assert "record.status is DeliveryRecordStatus.FAILED" in retryable
    assert "_aware(record.lease_expires_at) < now" in retryable


# ============================================================================
# D. 6H owns the paging query; nothing new in the schema
# ============================================================================


def test_paging_is_owner_scoped_bounded_and_anchored_through_get() -> None:
    page = ast.unparse(function(parse(NOTIFICATIONS), "page"))
    assert "TaskNotification.owner_id == self._owner_id" in page
    assert "min(int(limit), MAX_LISTED)" in page
    assert "await self.get(before)" in page
    assert ".limit(bounded + 1)" in page
    assert "order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())" in page


def test_6m2_adds_no_migration() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("0*.py"))
    assert versions[-1] == "0019_notification_deliveries.py"
