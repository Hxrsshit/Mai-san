"""Stage 7A structure: identity and organisation boundaries, by AST.

Written from the Stage 7A contract, not from the implementation. Every claim
here is an AST walk (never a substring search) or route introspection over
the real app:

- dependency directions of `app.identity` and `app.organisation`, and who
  may import them;
- the construction sites of `MemberRevocation` and `MonitorRequestService`;
- `Principal` is a frozen dataclass of plain values;
- the dependencies in `app/api/deps.py` write nothing;
- no logger call in the identity paths carries a secret, and the identity
  service and revocation log no exception text;
- every `/api/organisation` route depends on `require_admin` and
  `get_owner_id`, and no response model names a user id, a subject, a code
  hash or a token.
"""

import ast
import dataclasses
import importlib.util
import os
import sys
import sysconfig
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from fastapi.routing import APIRoute

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
IDENTITY = APP / "identity"
ORGANISATION = APP / "organisation"
DEPS = APP / "api" / "deps.py"
TELEGRAM_ROUTE = APP / "api" / "routes" / "telegram.py"
CHAT_SERVICE = APP / "services" / "chat_service.py"
IDENTITY_SERVICE = IDENTITY / "service.py"
REVOCATION = ORGANISATION / "revocation.py"


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    return str(path.relative_to(BACKEND))


def module_name(path: Path) -> str:
    parts = list(path.relative_to(BACKEND).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def app_files() -> List[Path]:
    return sorted(APP.rglob("*.py"))


def under(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _resolve_relative(path: Path, node: ast.ImportFrom) -> str:
    """The absolute module an `ImportFrom` names, relative imports resolved."""
    if not node.level:
        return node.module or ""
    package = module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _type_checking_blocks(tree: ast.AST) -> Set[int]:
    """ids of every node inside an `if TYPE_CHECKING:` body."""
    inside: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) in (
            "TYPE_CHECKING", "typing.TYPE_CHECKING"
        ):
            for child in node.body:
                for sub in ast.walk(child):
                    inside.add(id(sub))
    return inside


def imported_modules(path: Path, include_type_checking: bool = True) -> List[Tuple[str, bool]]:
    """Every module `path` imports, as (module, under_type_checking).

    `from app import organisation` counts as importing `app.organisation`.
    """
    tree = parse(path)
    guarded = _type_checking_blocks(tree)
    found: List[Tuple[str, bool]] = []
    for node in ast.walk(tree):
        checking = id(node) in guarded
        if checking and not include_type_checking:
            continue
        if isinstance(node, ast.Import):
            found.extend((alias.name, checking) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_relative(path, node)
            found.append((module, checking))
            for alias in node.names:
                found.append((f"{module}.{alias.name}" if module else alias.name, checking))
    return found


_STDLIB_DIRS = tuple(
    os.path.realpath(p) for p in {sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]}
)


def is_stdlib(module: str) -> bool:
    top = module.split(".")[0]
    if top == "__future__" or top in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(top)
    if spec is None or spec.origin is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    origin = os.path.realpath(spec.origin)
    return origin.startswith(_STDLIB_DIRS) and "site-packages" not in origin


def disallowed(path: Path, allowed_packages: Iterable[str]) -> List[str]:
    """Imports of `path` outside the stdlib, SQLAlchemy and `allowed_packages`.

    Each `from M import N` is judged by `M`; the `M.N` form is only consulted
    by the "who imports X" rules below.
    """
    tree = parse(path)
    guarded = _type_checking_blocks(tree)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [_resolve_relative(path, node)]
            if modules == ["app"]:
                modules = [f"app.{alias.name}" for alias in node.names]
        else:
            continue
        for module in modules:
            if id(node) in guarded:
                bad.append(f"TYPE_CHECKING:{module}")
                continue
            if is_stdlib(module) or under(module, "sqlalchemy"):
                continue
            if any(under(module, package) for package in allowed_packages):
                continue
            bad.append(module)
    return bad


# ============================================================================
# A. Dependency directions
# ============================================================================


def test_the_stdlib_detector_is_not_vacuous() -> None:
    assert is_stdlib("uuid") and is_stdlib("datetime") and is_stdlib("__future__")
    assert is_stdlib("unicodedata") and is_stdlib("typing")
    assert not is_stdlib("sqlalchemy") and not is_stdlib("fastapi") and not is_stdlib("app")


def test_app_identity_imports_only_core_database_itself_stdlib_and_sqlalchemy() -> None:
    files = sorted(IDENTITY.rglob("*.py"))
    assert len(files) >= 6, files
    allowed = ("app.core", "app.database", "app.identity")
    for path in files:
        assert disallowed(path, allowed) == [], rel(path)


def test_app_organisation_imports_only_its_declared_dependencies() -> None:
    files = sorted(ORGANISATION.rglob("*.py"))
    assert REVOCATION in files
    allowed = (
        "app.identity",
        "app.tasks.models",
        "app.tasks.states",
        "app.tasks.schemas",
        "app.authorization.grants",
        "app.authorization.models",
        "app.core",
        "app.database",
    )
    for path in files:
        bad = disallowed(path, allowed)
        # `MonitorRequestService` is allowed, for type checking only.
        bad = [b for b in bad if b != "TYPE_CHECKING:app.tasks.requests"]
        assert bad == [], rel(path)


def test_monitor_request_service_is_named_in_organisation_only_for_type_checking() -> None:
    for path in sorted(ORGANISATION.rglob("*.py")):
        for module, checking in imported_modules(path):
            if under(module, "app.tasks.requests"):
                assert checking, f"{rel(path)} imports {module} at run time"


def test_only_deps_imports_app_organisation() -> None:
    importers = sorted(
        rel(path) for path in app_files()
        if not under(module_name(path), "app.organisation")
        and any(under(m, "app.organisation") for m, _ in imported_modules(path))
    )
    assert importers == ["app/api/deps.py"]


def test_the_task_authorization_execution_and_background_layers_never_import_identity() -> None:
    checked = 0
    for package in ("tasks", "authorization", "execution", "background"):
        for path in sorted((APP / package).rglob("*.py")):
            checked += 1
            reached = [m for m, _ in imported_modules(path)
                       if under(m, "app.identity") or under(m, "app.organisation")]
            assert reached == [], rel(path)
    assert checked > 20


def test_the_telegram_route_never_imports_app_organisation() -> None:
    modules = [m for m, _ in imported_modules(TELEGRAM_ROUTE)]
    assert modules, "the walk found no imports at all"
    assert not [m for m in modules if under(m, "app.organisation")]


# ============================================================================
# B. Construction sites
# ============================================================================


def call_sites(name: str) -> List[str]:
    """Every call to `name(...)` or `<anything>.name(...)` in `app/`, one
    entry per call."""
    sites = []
    for path in app_files():
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if called == name:
                sites.append(rel(path))
    return sorted(sites)


def test_member_revocation_is_constructed_only_in_deps() -> None:
    assert call_sites("MemberRevocation") == ["app/api/deps.py"]


def test_monitor_request_service_is_constructed_only_in_deps_twice_and_in_chat_service() -> None:
    sites = call_sites("MonitorRequestService")
    assert sites.count("app/api/deps.py") == 2, sites
    assert set(sites) == {"app/api/deps.py", "app/services/chat_service.py"}, sites


# ============================================================================
# C. Principal
# ============================================================================


def test_principal_is_a_frozen_dataclass_of_plain_values() -> None:
    from app.identity.constants import Role
    from app.identity.principal import Principal

    assert dataclasses.is_dataclass(Principal)
    assert Principal.__dataclass_params__.frozen
    principal = Principal(member_id=uuid.uuid4(), organisation_id=uuid.uuid4(), role=Role.ADMIN)
    try:
        principal.role = Role.MEMBER  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("Principal is mutable")
    for field in dataclasses.fields(Principal):
        value = getattr(principal, field.name)
        assert isinstance(value, (uuid.UUID, Role)), field.name
    # Not an ORM row: no mapper, no instance state.
    assert not hasattr(Principal, "__table__") and not hasattr(principal, "_sa_instance_state")


# ============================================================================
# D. deps.py dependencies write nothing
# ============================================================================

#: Session methods that write, or run SQL a dependency must not run.
WRITE_METHODS = {"add", "add_all", "flush", "commit", "execute", "merge", "delete"}


def functions(tree: ast.AST):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def method_calls(fn) -> List[str]:
    return [n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]


def test_no_deps_function_writes_or_executes() -> None:
    tree = parse(DEPS)
    fns = functions(tree)
    assert len(fns) >= 25
    offenders = sorted(
        (fn.name, call) for fn in fns for call in method_calls(fn) if call in WRITE_METHODS
    )
    assert offenders == []


def test_the_seam_read_only_rolls_back_on_its_session() -> None:
    fn = next(f for f in functions(parse(DEPS)) if f.name == "_active_principal")
    on_session = sorted({
        n.func.attr for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and ast.unparse(n.func.value) == "session"
    })
    # `in_transaction()` is the guard's question; the one action is rollback.
    assert set(on_session) <= {"rollback", "in_transaction"}, on_session
    assert "rollback" in on_session


# ============================================================================
# E. Logging
# ============================================================================

SECRET_NAMES = {
    "code", "code_text", "plaintext", "subject", "sender_id", "chat_id", "text", "display_name",
}
LOGGED_FILES = (IDENTITY_SERVICE, REVOCATION, TELEGRAM_ROUTE)


def logger_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            yield node


def names_in(node: ast.AST) -> Set[str]:
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def enclosing_function(tree: ast.AST, target: ast.AST) -> Optional[ast.AST]:
    best = None
    for fn in functions(tree):
        if any(sub is target for sub in ast.walk(fn)):
            if best is None or any(sub is fn for sub in ast.walk(best)):
                best = fn
    return best


def extra_values(tree: ast.AST, call: ast.Call) -> List[ast.AST]:
    """The values a logger call's `extra` can carry.

    A dict literal: its values. A variable: every value assigned to it, or
    to one of its keys, in the same function.
    """
    values: List[ast.AST] = []
    for keyword in call.keywords:
        if keyword.arg != "extra":
            continue
        if isinstance(keyword.value, ast.Dict):
            values.extend(keyword.value.values)
            continue
        if isinstance(keyword.value, ast.Name):
            name = keyword.value.id
            scope = enclosing_function(tree, call) or tree
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            values.extend(node.value.values if isinstance(node.value, ast.Dict)
                                          else [node.value])
                        elif (isinstance(target, ast.Subscript)
                              and ast.unparse(target.value) == name):
                            values.append(node.value)
            assert values, f"extra={name} resolves to nothing"
            continue
        values.append(keyword.value)
    return values


def test_no_logger_call_carries_a_secret() -> None:
    seen = 0
    for path in LOGGED_FILES:
        tree = parse(path)
        for call in logger_calls(tree):
            seen += 1
            carried = set()
            for arg in call.args:
                carried |= names_in(arg)
            for keyword in call.keywords:
                if keyword.arg not in ("extra",):
                    carried |= names_in(keyword.value)
            for value in extra_values(tree, call):
                carried |= names_in(value)
            leaked = carried & SECRET_NAMES
            assert not leaked, f"{rel(path)}:{call.lineno} logs {sorted(leaked)}"
    assert seen >= 15, seen


def test_the_secret_name_walk_is_not_vacuous() -> None:
    sample = ast.parse(
        "def f(classified, subject):\n"
        "    extra = {'category': 'x'}\n"
        "    extra['who'] = classified.chat_id\n"
        "    logger.info('m', extra=extra)\n"
        "    logger.info('m', extra={'s': str(subject)})\n"
        "    logger.info('m %s', text)\n"
    )
    hits = []
    for call in logger_calls(sample):
        carried = set()
        for arg in call.args:
            carried |= names_in(arg)
        for value in extra_values(sample, call):
            carried |= names_in(value)
        hits.append(sorted(carried & SECRET_NAMES))
    assert hits == [["chat_id"], ["subject"], ["text"]]


def test_identity_service_and_revocation_log_no_exception_text() -> None:
    for path in (IDENTITY_SERVICE, REVOCATION):
        calls = list(logger_calls(parse(path)))
        assert calls, rel(path)
        for call in calls:
            assert call.func.attr != "exception", f"{rel(path)}:{call.lineno}"
            for keyword in call.keywords:
                if keyword.arg == "exc_info":
                    assert isinstance(keyword.value, ast.Constant) and not keyword.value.value, (
                        f"{rel(path)}:{call.lineno}")


# ============================================================================
# F. Every organisation route is behind the seam and require_admin
# ============================================================================


def _depends_on(dependant, target) -> bool:
    for sub in dependant.dependencies:
        if sub.call is target or _depends_on(sub, target):
            return True
    return False


def _organisation_routes(monkeypatch):
    import app.main as main_module
    from app.core.config import Settings

    everything = Settings(_env_file=None, EXECUTION_ENABLED=True, MEMORY_ENABLED=True,
                          HISTORY_IMPORT_ENABLED=True, REMINDERS_ENABLED=True)
    monkeypatch.setattr(main_module, "get_settings", lambda: everything)
    app = main_module.create_app()
    return [r for r in app.routes
            if isinstance(r, APIRoute) and r.path.startswith("/api/organisation")]


def test_every_organisation_route_requires_an_admin_behind_the_seam(monkeypatch) -> None:
    from app.api import deps

    routes = _organisation_routes(monkeypatch)
    pairs = sorted((m, r.path) for r in routes for m in r.methods)
    assert len(pairs) == 5, pairs
    for route in routes:
        assert _depends_on(route.dependant, deps.get_owner_id), route.path
        assert _depends_on(route.dependant, deps.require_admin), route.path
        assert _depends_on(route.dependant, deps.get_principal), route.path


FORBIDDEN_FIELDS = {"user_id", "subject", "code_sha256", "token", "sender_id", "chat_id"}


def _model_fields(model, seen=None) -> Set[str]:
    from pydantic import BaseModel

    seen = set() if seen is None else seen
    if model in seen:
        return set()
    seen.add(model)
    names = set()
    for name, info in model.model_fields.items():
        names.add(name)
        stack = [info.annotation]
        while stack:
            annotation = stack.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                names |= _model_fields(annotation, seen)
            stack.extend(getattr(annotation, "__args__", ()) or ())
    return names


def test_no_organisation_response_model_names_an_identifier_hash_or_token(monkeypatch) -> None:
    routes = _organisation_routes(monkeypatch)
    for route in routes:
        assert route.response_model is not None, route.path
        fields = _model_fields(route.response_model)
        assert fields, route.path
        assert not fields & FORBIDDEN_FIELDS, (route.path, fields & FORBIDDEN_FIELDS)
