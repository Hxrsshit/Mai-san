"""Stage 6J security: Telegram is a channel, not a system.

The Telegram adapter is where a Mai record meets a third party that holds a
bot token Mai cannot rotate for it. So the claims here are about what the
adapter cannot do: reach the task, execution or authorization machinery,
create or change a notification, read the environment, build its own HTTP
client, choose where a message goes, or put a secret anywhere. Every
structural claim is an AST walk, not a substring search.
"""

import ast
import inspect
from pathlib import Path

from app.core.config import Settings
from app.telegram.notifier import TelegramNotificationAdapter

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
NOTIFIER = APP / "telegram" / "notifier.py"
CLIENT = APP / "telegram" / "client.py"
DELIVERY = APP / "delivery"


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


def called(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
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
# A. What the adapter may reach
# ============================================================================


def test_the_adapter_imports_exactly_these_modules() -> None:
    assert imports(parse(NOTIFIER)) == {
        "asyncio", "re", "collections", "datetime", "typing",
        "app.core.config", "app.core.logging", "app.delivery.contract",
        "app.tasks.models", "app.telegram.client",
    }


def test_the_adapter_names_nothing_from_the_core_machinery() -> None:
    names = mentioned(parse(NOTIFIER))
    for forbidden in (
        "TaskRunner", "BackgroundRuntime", "ExecutionService", "Dispatcher",
        "AuthorizationService", "GrantService", "TaskService", "NotificationService",
        "NotificationDeliveryService", "AdapterRegistry", "record_outcome",
        "TaskNotification", "LLMProvider", "get_llm_provider", "evaluate",
        "parse_spec", "SecureHttpClient", "NetworkPolicy", "httpx", "requests",
    ):
        assert forbidden not in names, forbidden


def test_the_adapter_has_no_http_client_of_its_own() -> None:
    """The one Telegram client is the foundation's. This module reuses it."""
    tree = parse(NOTIFIER)
    for module in imports(tree):
        assert module.split(".")[0] not in {
            "httpx", "requests", "aiohttp", "urllib", "http", "socket", "ssl",
            "websockets", "telegram", "telethon", "aiogram",
        }, module
    constructed = [
        ast.unparse(n.func) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id.endswith(("Client", "Sender", "Session"))
    ]
    assert constructed == ["BotApiSender"]


def test_the_adapter_reads_no_environment_and_no_files() -> None:
    tree = parse(NOTIFIER)
    assert "os" not in imports(tree) and "dotenv" not in imports(tree)
    assert not {"environ", "getenv", "open", "read_text", "read_bytes"} & mentioned(tree)


def test_the_adapter_has_no_dynamic_or_dangerous_call() -> None:
    tree = parse(NOTIFIER)
    names = called(tree)
    for forbidden in (
        "eval", "exec", "compile", "__import__", "import_module", "getattr",
        "setattr", "system", "popen", "Popen", "run", "loads", "load",
    ):
        if forbidden == "compile":
            # `re.compile` of a literal pattern only.
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "compile":
                    assert ast.unparse(n.func) == "re.compile"
                    assert isinstance(n.args[0], ast.Constant)
            continue
        assert forbidden not in names, forbidden


def test_the_adapter_has_no_loop_retry_schedule_or_worker() -> None:
    tree = parse(NOTIFIER)
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.While, ast.For, ast.AsyncFor))]
    for forbidden in ("create_task", "ensure_future", "gather", "sleep", "call_later",
                      "call_at", "run_in_executor", "Thread", "wait_for", "shield"):
        assert forbidden not in called(tree), forbidden


def test_the_adapter_never_touches_the_database() -> None:
    tree = parse(NOTIFIER)
    assert not {"AsyncSession", "Session", "session", "select", "sqlalchemy"} & mentioned(tree)
    assert not any(m.startswith("sqlalchemy") or m.startswith("app.database")
                   for m in imports(tree))
    for forbidden in ("add", "add_all", "flush", "commit", "rollback", "delete",
                      "merge", "execute", "update", "insert", "begin", "refresh",
                      "scalars"):
        assert forbidden not in called(tree), forbidden


def test_the_adapter_is_not_reachable_from_a_route_the_runtime_or_content() -> None:
    """Changed deliberately in 6K: the composition root is the one importer.
    Routes, the runtime, the runner and every content-handling module still
    cannot reach the adapter."""
    importers = []
    for path in APP.rglob("*.py"):
        if path == NOTIFIER:
            continue
        if any(m == "app.telegram.notifier" or m.startswith("app.telegram.notifier.")
               for m in imports(parse(path))):
            importers.append(str(path.relative_to(BACKEND)))
    assert importers == ["app/composition/notification_delivery.py"]


def test_only_the_composition_root_constructs_the_adapter_and_only_once() -> None:
    """Changed deliberately in 6K. 6J built the channel; 6K's composition root
    constructs it, in exactly one place, so the process holds one instance and
    its duplicate memory. Deciding what triggers delivery is still a later,
    separate decision, and nothing else constructs the adapter."""
    constructions = []
    for path in APP.rglob("*.py"):
        if path == NOTIFIER:
            continue
        tree = parse(path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "TelegramNotificationAdapter"):
                constructions.append(str(path.relative_to(BACKEND)))
        if "TelegramNotificationAdapter" in mentioned(tree):
            assert path.relative_to(BACKEND).as_posix() == "app/composition/notification_delivery.py", path
    assert constructions == ["app/composition/notification_delivery.py"]


def test_the_delivery_package_still_knows_nothing_of_telegram() -> None:
    for path in DELIVERY.glob("*.py"):
        tree = parse(path)
        for module in imports(tree):
            assert not module.startswith("app.telegram"), (path.name, module)
        # Identifiers and code strings, not prose: the 6I docstring rightly
        # says channels such as Telegram come later.
        assert not [n for n in mentioned(tree) if "telegram" in n.lower()], path.name
        assert not [v for v in code_strings(tree) if "telegram" in v.lower()], path.name


# ============================================================================
# B. Where the destination and the credential come from
# ============================================================================


def test_the_adapter_reads_exactly_the_two_foundation_settings() -> None:
    attrs = {
        n.attr for n in ast.walk(parse(NOTIFIER))
        if isinstance(n, ast.Attribute) and n.attr.startswith("TELEGRAM_")
    }
    assert attrs == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID"}


def test_the_settings_are_read_once_in_the_constructor_and_not_kept() -> None:
    tree = parse(NOTIFIER)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "TelegramNotificationAdapter")
    readers = {
        fn.name for fn in cls.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr.startswith("TELEGRAM_")
    }
    assert readers == {"__init__"}
    stored = {
        ast.unparse(t) for fn in cls.body if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets
    }
    assert "self._settings" not in stored and "self._token" not in stored


def test_the_webhook_only_settings_are_never_read() -> None:
    tree = parse(NOTIFIER)
    for unused in ("TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_CONVERSATION_ID"):
        assert unused not in mentioned(tree)
        assert unused not in code_strings(tree)


def test_there_is_exactly_one_telegram_configuration_namespace() -> None:
    assert sorted(f for f in Settings.model_fields if "TELEGRAM" in f.upper()) == [
        "TELEGRAM_ALLOWED_CHAT_ID", "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CONVERSATION_ID", "TELEGRAM_WEBHOOK_SECRET",
    ]
    for path in list(APP.rglob("*.py")) + [BACKEND.parent / "docker-compose.yml"]:
        assert "TELEGRAM_NOTIFY" not in path.read_text(encoding="utf-8"), path


def test_the_destination_is_never_a_parameter_of_delivery() -> None:
    assert list(inspect.signature(TelegramNotificationAdapter.deliver).parameters) == [
        "self", "payload",
    ]
    assert list(inspect.signature(TelegramNotificationAdapter.__init__).parameters) == [
        "self", "settings", "sender",
    ]
    tree = parse(NOTIFIER)
    sends = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "send_text"]
    assert len(sends) == 1
    assert [ast.unparse(a) for a in sends[0].args] == [
        "self._chat_id", "render_message(payload)",
    ]
    assert sends[0].keywords == []


def test_no_parse_mode_or_extra_request_field_is_ever_named() -> None:
    """The adapter hands the sender a chat id and a text. It names no other
    Bot API field, in code (its docstring explains why `parse_mode` is absent)."""
    tree = parse(NOTIFIER)
    for field in ("parse_mode", "reply_markup", "disable_web_page_preview",
                  "entities", "reply_to_message_id", "message_thread_id"):
        assert field not in mentioned(tree)
        assert field not in code_strings(tree)
    assert "chat_id" not in code_strings(tree)
    # Exactly two dict literals exist: the closed headline table (keyed by enum
    # members) and the log record's three-key `extra`. Neither is a request body.
    shapes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            if all(isinstance(k, ast.Attribute) for k in node.keys):
                shapes.append("headlines")
            else:
                shapes.append(tuple(sorted(k.value for k in node.keys)))
    assert sorted(shapes, key=str) == sorted(
        ["headlines", ("adapter", "notification_id", "reason")], key=str
    )


# ============================================================================
# C. One sender, one host, one adapter
# ============================================================================


def test_the_telegram_host_and_endpoint_live_in_one_module() -> None:
    holders = {"api.telegram.org": set(), "sendMessage": set()}
    for path in APP.rglob("*.py"):
        strings = code_strings(parse(path))      # code, not prose
        for needle in holders:
            if any(needle in value for value in strings):
                holders[needle].add(str(path.relative_to(BACKEND)))
    assert holders == {
        "api.telegram.org": {"app/telegram/client.py"},
        "sendMessage": {"app/telegram/client.py"},
    }


def test_there_is_one_telegram_sender_and_one_telegram_adapter() -> None:
    senders, adapters = [], []
    for path in APP.rglob("*.py"):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ClassDef):
                if node.name == "BotApiSender":
                    senders.append(str(path.relative_to(BACKEND)))
                if node.name == "TelegramNotificationAdapter":
                    adapters.append(str(path.relative_to(BACKEND)))
    assert senders == ["app/telegram/client.py"]
    assert adapters == ["app/telegram/notifier.py"]


def test_exactly_one_adapter_is_named_telegram() -> None:
    names = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(parse(path)):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and getattr(node.targets[0], "id", None) == "name"
                    and isinstance(node.value, ast.Constant) and node.value.value == "telegram"):
                names.append(str(path.relative_to(BACKEND)))
    assert names == ["app/telegram/notifier.py"]


def test_the_foundation_sender_was_not_changed_to_serve_notifications() -> None:
    """6J reuses `BotApiSender` as committed: same public surface, no new
    parameter to name a destination, host or credential."""
    tree = parse(CLIENT)
    sender = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.ClassDef) and n.name == "BotApiSender")
    methods = {m.name: [a.arg for a in m.args.args]
               for m in sender.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert methods == {
        "__init__": ["self", "settings", "client"],
        "_send_url": ["self"],
        "send_text": ["self", "chat_id", "text"],
    }


# ============================================================================
# D. Errors, logs, and the persistence 6J does not add
# ============================================================================


def test_no_exception_is_ever_bound_rendered_or_chained() -> None:
    """Telegram's API URL contains the bot token and transport errors carry
    the URL. A handler that names the exception invites `str(exc)`."""
    tree = parse(NOTIFIER)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1 and handlers[0].name is None
    assert ast.unparse(handlers[0].type) == "Exception"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert not {"exception", "format_exc", "print_exc", "format_exception"} & called(tree)


def test_logs_carry_only_an_adapter_a_reason_and_an_id() -> None:
    tree = parse(NOTIFIER)
    logged = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            logged += 1
            extra = next(k.value for k in node.keywords if k.arg == "extra")
            assert sorted(k.value for k in extra.keys) == ["adapter", "notification_id", "reason"]
            assert [type(a).__name__ for a in node.args] == ["Constant"]
    assert logged == 1


def test_the_reason_codes_are_literals() -> None:
    reasons = {
        c.args[0].value for c in ast.walk(parse(NOTIFIER))
        if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "_failed"
    }
    assert reasons == {
        "invalid_payload", "telegram_not_configured", "delivery_key_mismatch",
        "telegram_send_failed",
    }


def test_6j_adds_no_migration_table_or_persisted_delivery_state() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("0*.py"))
    # Changed deliberately in 6M.1: the next migration after 6H's 0018 is
    # 6M.1's delivery-records table, and nothing else.
    assert [v for v in versions if v[:4] > "0018"] == ["0019_notification_deliveries.py"]
    from app.database.metadata import Base

    # The only delivery table is 6M.1's, written only by `app.delivery.records`
    # (pinned there); the adapter itself still persists nothing.
    assert [t for t in Base.metadata.tables if "deliver" in t or "telegram" in t] == [
        "notification_deliveries"
    ]


def test_the_adapter_stays_python_39_compatible() -> None:
    """The local environment is Python 3.9: no `X | Y`, no builtin generics
    evaluated at runtime, no 3.10+ syntax (the parse above pins the syntax)."""
    tree = parse(NOTIFIER)
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        if isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
        for annotation in annotations:
            for inner in ast.walk(annotation):
                assert not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr))
                if isinstance(inner, ast.Subscript) and isinstance(inner.value, ast.Name):
                    assert inner.value.id not in {"list", "dict", "tuple", "set", "frozenset", "type"}
