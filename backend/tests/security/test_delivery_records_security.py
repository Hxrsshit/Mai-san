"""Stage 6M.1 security: durable delivery records stay a narrow ledger.

One writer, one table, no content, and a claim that is taken only after the
owner-scoped read and the payload check -- so nothing a caller supplies can
create a record for a notification it cannot see. The migration adds one
table and alters none. Every structural claim is an AST walk.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
RECORDS = APP / "delivery" / "records.py"
MODELS = APP / "delivery" / "models.py"
SERVICE = APP / "delivery" / "service.py"
MIGRATION = BACKEND / "alembic" / "versions" / "0019_notification_deliveries.py"


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


def mentioned(tree) -> set:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return names | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def function(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


# ============================================================================
# A. One writer, one table
# ============================================================================


def test_only_the_records_module_writes_delivery_records() -> None:
    writers = set()
    for path in APP.rglob("*.py"):
        tree = parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            constructs = isinstance(func, ast.Name) and func.id == "NotificationDelivery"
            updates = (isinstance(func, ast.Name) and func.id in ("update", "insert", "delete")
                       and node.args and ast.unparse(node.args[0]) == "NotificationDelivery")
            if constructs or updates:
                writers.add(str(path.relative_to(BACKEND)))
    assert writers == {"app/delivery/records.py"}


def test_the_records_module_reaches_only_its_table() -> None:
    tree = parse(RECORDS)
    assert imports(tree) == {
        "uuid", "datetime", "typing", "sqlalchemy", "sqlalchemy.exc",
        "sqlalchemy.ext.asyncio", "app.delivery.models",
    }
    for name in ("TaskNotification", "NotificationService", "record_outcome", "mark_read",
                 "read_at", "Task", "deliver", "adapter_name", "text", "message",
                 "chat_id", "token", "url"):
        assert name not in mentioned(tree), name


def test_nothing_outside_the_delivery_package_imports_the_writer() -> None:
    importers = sorted(
        str(p.relative_to(BACKEND)) for p in APP.rglob("*.py")
        if any(m == "app.delivery.records" for m in imports(parse(p)))
        or any(isinstance(n, ast.ImportFrom) and n.module == "app.delivery"
               and "records" in [a.name for a in n.names] for n in ast.walk(parse(p)))
    )
    assert importers == ["app/delivery/service.py"]


def test_the_records_module_has_no_loop_retry_or_scheduling() -> None:
    tree = parse(RECORDS)
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]
    assert not {"sleep", "create_task", "ensure_future", "gather", "next_run_at",
                "eval", "exec", "getattr", "setattr", "__import__"} & mentioned(tree)


def test_a_record_has_exactly_these_columns_and_no_content() -> None:
    tree = parse(MODELS)
    model = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "NotificationDelivery")
    columns = [ast.unparse(s.target) for s in model.body if isinstance(s, ast.AnnAssign)]
    assert columns == [
        "id", "notification_id", "owner_id", "adapter", "status", "attempts",
        "lease_expires_at", "created_at", "updated_at", "delivered_at",
    ]


def test_the_unique_pair_and_the_cascade_are_declared() -> None:
    source = MODELS.read_text(encoding="utf-8")
    assert '"uq_notification_deliveries_notification_adapter",\n            "notification_id", "adapter",\n            unique=True,' in source
    assert 'ForeignKey("task_notifications.id", ondelete="CASCADE")' in source


# ============================================================================
# B. The service claims only after the owner-scoped read and the payload check
# ============================================================================


def _line_of(tree, predicate) -> int:
    return min(n.lineno for n in ast.walk(tree) if predicate(n))


def test_the_claim_comes_after_the_owner_read_and_the_payload_and_before_the_adapter() -> None:
    deliver = function(parse(SERVICE), "deliver")
    is_call = lambda attr: (lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == attr)
    owner_read = _line_of(deliver, is_call("get"))
    payload = _line_of(deliver, lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", None) == "DeliveryPayload")
    claim = _line_of(deliver, is_call("claim"))
    adapter = _line_of(deliver, lambda n: isinstance(n, ast.Call) and ast.unparse(n.func) == "adapter.deliver")
    assert owner_read < payload < claim < adapter


def test_the_claim_takes_its_owner_from_the_notification_record() -> None:
    deliver = function(parse(SERVICE), "deliver")
    [claim] = [n for n in ast.walk(deliver) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "claim"]
    keywords = {k.arg: ast.unparse(k.value) for k in claim.keywords}
    assert keywords == {"notification_id": "notification.id", "owner_id": "notification.owner_id",
                        "adapter": "name"}


def test_every_adapter_outcome_finishes_the_claim() -> None:
    """Four exits after the claim -- timeout, exception, wrong type, an
    answer -- and each one records its outcome."""
    deliver = function(parse(SERVICE), "deliver")
    finishes = [n for n in ast.walk(deliver) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "finish"]
    assert len(finishes) == 4
    delivered = sorted(ast.unparse(k.value) for f in finishes for k in f.keywords if k.arg == "delivered")
    assert delivered == ["False", "False", "False", "status is not DeliveryStatus.FAILED"]


def test_the_service_itself_still_writes_nothing() -> None:
    tree = parse(SERVICE)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {"add", "commit", "execute", "flush", "delete", "merge", "rollback"} & called


# ============================================================================
# C. The migration
# ============================================================================


def test_the_migration_creates_one_table_and_alters_none() -> None:
    tree = parse(MIGRATION)
    operations = sorted(
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"create_table", "drop_table", "add_column", "drop_column",
                            "alter_column", "create_check_constraint", "drop_constraint",
                            "create_index", "drop_index", "execute", "rename_table"}
    )
    assert operations == ["create_index", "create_table", "drop_index", "drop_table"]
    tables = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) in ("create_table", "drop_table")}
    assert tables == {"notification_deliveries"}


def test_the_migration_names_constraints_through_op_f_and_drops_its_type() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for name in ("pk_notification_deliveries",
                 "fk_notification_deliveries_notification_id_task_notifications",
                 "ck_notification_deliveries_attempts_positive",
                 "ck_notification_deliveries_delivered_iff_delivered_at",
                 "ck_notification_deliveries_sending_has_lease"):
        assert f'op.f("{name}")' in source, name
    assert 'sa.Enum(name="notification_delivery_status").drop(bind, checkfirst=True)' in source
    assert 'revision: str = "0019"' in source and 'down_revision: Union[str, None] = "0018"' in source
