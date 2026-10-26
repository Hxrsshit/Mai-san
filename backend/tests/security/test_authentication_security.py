"""Stage 6M.4 security: one authenticator, one request seam, one owner.

Structural claims are AST walks over `app/`, plus a route-introspection test
over the app itself: a new data route registered without the seam fails here.
The frontend is checked too: the token is user-entered and session-scoped,
never built in.
"""

import ast
import re
from pathlib import Path

from fastapi.routing import APIRoute

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
APP = BACKEND / "app"
AUTH = APP / "core" / "auth.py"
OWNER = APP / "core" / "owner.py"
DEPS = APP / "api" / "deps.py"
MAIN = APP / "main.py"
FRONTEND_API = REPO / "frontend" / "lib" / "api.ts"
OWNER_LITERAL = "00000000-0000-0000-0000-00000000a1a1"
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/api/integrations/google/callback"),
    ("GET", "/api/integrations/gmail/callback"),
    ("POST", "/api/telegram/webhook"),
}


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


def function(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def calls(name_or_unparsed):
    found = []
    for path in APP.rglob("*.py"):
        for n in ast.walk(parse(path)):
            if isinstance(n, ast.Call) and ast.unparse(n.func) == name_or_unparsed:
                found.append(str(path.relative_to(BACKEND)))
    return sorted(found)


# ============================================================================
# A. The authenticator
# ============================================================================


def test_the_core_authenticator_is_pure() -> None:
    assert imports(parse(AUTH)) == {
        "hashlib", "hmac", "re", "uuid", "typing", "app.core.config", "app.core.owner",
    }


def test_it_compares_sha256_digests_in_constant_time_and_nowhere_with_equality() -> None:
    fn = function(parse(AUTH), "authenticate")
    text = ast.unparse(fn)
    assert "hashlib.sha256(token.encode('utf-8')).hexdigest()" in text
    assert "hmac.compare_digest(supplied, expected)" in text
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in n.ops)]


def test_it_answers_only_the_configured_owner_or_none() -> None:
    fn = function(parse(AUTH), "authenticate")
    returns = sorted(ast.unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert returns == ["CONFIGURED_OWNER_ID if hmac.compare_digest(supplied, expected) else None", "None"]


def test_there_is_one_authenticator_and_one_reader_of_the_digest() -> None:
    assert calls("auth.authenticate") == ["app/api/deps.py"]
    readers = sorted(str(p.relative_to(BACKEND)) for p in APP.rglob("*.py")
                     if "MAI_API_TOKEN_SHA256" in p.read_text(encoding="utf-8"))
    assert readers == ["app/core/auth.py", "app/core/config.py"]
    # Constant-time comparisons in the app: the API token here, and the
    # Telegram webhook's own secret -- two credentials, never shared.
    assert calls("hmac.compare_digest") == ["app/api/routes/telegram.py", "app/core/auth.py"]


# ============================================================================
# B. The request seam
# ============================================================================


def test_the_seam_is_the_only_reader_of_the_authorization_header() -> None:
    """Every HTTP header parameter (`= Header(...)`) named for, or aliased to,
    `Authorization` in the app. (`authorization` is also the name of the
    tool-authorization service elsewhere; that is not a header.)"""
    readers = []
    for path in APP.rglob("*.py"):
        for fn in ast.walk(parse(path)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = fn.args.args[len(fn.args.args) - len(fn.args.defaults):]
            for arg, default in zip(args, fn.args.defaults):
                if isinstance(default, ast.Call) and ast.unparse(default.func) == "Header":
                    alias = [k.value.value for k in default.keywords if k.arg == "alias"]
                    if arg.arg == "authorization" or [a for a in alias if a.lower() == "authorization"]:
                        readers.append((str(path.relative_to(BACKEND)), fn.name))
    assert readers == [("app/api/deps.py", "get_owner_id")]


def test_the_seam_fails_closed_in_the_specified_order() -> None:
    fn = ast.unparse(function(parse(DEPS), "get_owner_id"))
    configured = fn.index("if not auth.is_configured(settings):")
    malformed = fn.index("raise AuthenticationRequired()")
    wrong = fn.index("raise InvalidCredentials()")
    assert configured < fn.index("raise AuthenticationNotConfigured()") < malformed < wrong
    # Stage 7A: then the member must be active; only then is an owner returned.
    inactive = fn.index("raise MembershipInactive()")
    assert wrong < fn.index("await _active_principal(session, owner_id)") < inactive < fn.index("return owner_id")
    assert "if scheme.lower() != 'bearer' or not token or ' ' in token:" in fn


def test_the_auth_errors_have_the_specified_status_codes_and_challenge() -> None:
    from app.core.errors import AuthenticationNotConfigured, AuthenticationRequired, InvalidCredentials

    assert (AuthenticationRequired.status_code, AuthenticationRequired.code) == (401, "authentication_required")
    assert (InvalidCredentials.status_code, InvalidCredentials.code) == (401, "invalid_credentials")
    assert (AuthenticationNotConfigured.status_code, AuthenticationNotConfigured.code) == (
        503, "authentication_not_configured")
    assert AuthenticationRequired.headers == InvalidCredentials.headers == {"WWW-Authenticate": "Bearer"}


def test_owner_bound_dependencies_take_the_seams_owner() -> None:
    deps = parse(DEPS)
    for name, ret in (("get_task_service", "TaskService(session=session, settings=settings, owner_id=owner_id)"),
                      ("get_notification_delivery_service", "notification_delivery_service(session, owner_id)"),
                      ("get_notification_service", "NotificationService(session, owner_id)"),
                      ("get_delivery_status_reader", "DeliveryStatusReader(session, owner_id)")):
        fn = function(deps, name)
        assert "owner_id" in [a.arg for a in fn.args.args], name
        [r] = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        assert ast.unparse(r.value) == ret, name


# ============================================================================
# C. Every data route depends on the seam (introspection)
# ============================================================================


def _depends_on_seam(dependant) -> bool:
    for sub in dependant.dependencies:
        if getattr(sub.call, "__name__", None) == "get_owner_id" or _depends_on_seam(sub):
            return True
    return False


def test_every_route_outside_the_allow_list_depends_on_get_owner_id(monkeypatch) -> None:
    import app.main as main_module
    from app.core.config import Settings

    everything = Settings(_env_file=None, EXECUTION_ENABLED=True, MEMORY_ENABLED=True,
                          HISTORY_IMPORT_ENABLED=True, REMINDERS_ENABLED=True)
    monkeypatch.setattr(main_module, "get_settings", lambda: everything)
    app = main_module.create_app()
    unprotected = sorted(
        (method, r.path) for r in app.routes if isinstance(r, APIRoute)
        for method in r.methods if not _depends_on_seam(r.dependant)
    )
    assert set(unprotected) == PUBLIC, unprotected
    assert len([r for r in app.routes if isinstance(r, APIRoute)]) > 55


def test_main_protects_every_router_but_the_three_public_ones() -> None:
    tree = parse(MAIN)
    includes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "app.include_router"]
    bare = sorted(ast.unparse(n.args[0]) for n in includes if not n.keywords)
    assert bare == ["health_router", "integrations_router", "telegram_router"]
    assert {ast.unparse(k.value) for n in includes for k in n.keywords} == {"AUTHENTICATED"}
    assert "AUTHENTICATED = [Depends(get_owner_id)]" in ast.unparse(function(tree, "create_app"))


def test_only_the_oauth_callbacks_are_public_on_the_integrations_router() -> None:
    tree = parse(APP / "api" / "routes" / "integrations.py")
    public = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            for d in fn.decorator_list:
                if isinstance(d, ast.Call) and ast.unparse(d.func).startswith("router."):
                    if not [k for k in d.keywords if k.arg == "dependencies"]:
                        public.append(d.args[0].value)
    assert sorted(public) == ["/gmail/callback", "/google/callback"]


# ============================================================================
# D. One owner; the background never touches request auth
# ============================================================================


def test_the_owner_literal_exists_once() -> None:
    sites = sorted(str(p.relative_to(BACKEND)) for p in APP.rglob("*.py")
                   if OWNER_LITERAL in p.read_text(encoding="utf-8"))
    assert sites == ["app/core/owner.py"]
    models = ast.unparse(parse(APP / "tasks" / "models.py"))
    assert "LOCAL_OWNER_ID = CONFIGURED_OWNER_ID" in models


def test_the_background_reaches_no_request_authentication() -> None:
    for path in (APP / "background").rglob("*.py"):
        tree = parse(path)
        assert not [m for m in imports(tree) if m.startswith(("app.api", "fastapi", "starlette"))], path.name
        assert not {"get_owner_id", "authenticate", "is_configured"} & {
            n.attr if isinstance(n, ast.Attribute) else n.id
            for n in ast.walk(tree) if isinstance(n, (ast.Name, ast.Attribute))}, path.name
    deliveries = parse(APP / "background" / "deliveries.py")
    assert "app.core.owner" in imports(deliveries)


def test_6m4_adds_no_migration() -> None:
    versions = sorted(p.name for p in (BACKEND / "alembic" / "versions").glob("0*.py"))
    # Changed deliberately in 7A: 0020 is the organisation-identity migration,
    # and the 6M.4 / 6M.2 migration-free guarantee still covers 0019.
    assert versions[-2:] == ["0019_notification_deliveries.py", "0020_organisation_identity.py"]


# ============================================================================
# E. Configuration and the frontend never carry the token
# ============================================================================


def test_the_setting_is_empty_in_compose_and_the_example() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "MAI_API_TOKEN_SHA256: ${MAI_API_TOKEN_SHA256:-}" in compose
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "\nMAI_API_TOKEN_SHA256=\n" in example
    for text in (compose, example):
        assert not re.search(r"MAI_API_TOKEN_SHA256[=:]\s*[0-9a-fA-F]{64}", text)


def test_the_frontend_build_carries_no_token() -> None:
    for path in [*(REPO / "frontend").glob("*.ts*"), *(REPO / "frontend").glob("next.config.*"),
                 REPO / "docker" / "frontend" / "Dockerfile", REPO / "docker-compose.yml"]:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"NEXT_PUBLIC_[A-Z_]*(TOKEN|SECRET|KEY|AUTH)", text), path.name
    for source in (REPO / "frontend").rglob("*.ts*"):
        if "node_modules" in source.parts or ".next" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"NEXT_PUBLIC_[A-Z_]*(TOKEN|SECRET|KEY|AUTH)", text), source.name


def test_the_frontend_keeps_the_token_in_session_storage_only() -> None:
    text = FRONTEND_API.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith(("*", "/*", "//")))
    assert "window.sessionStorage.getItem(TOKEN_KEY)" in code
    assert "window.sessionStorage.setItem(TOKEN_KEY, token)" in code
    assert not re.search(r"localStorage\.|document\.cookie|console\.(log|info|debug|warn|error)", code)
    # The header is built in one place, from the stored token, for the one API URL.
    assert code.count("Authorization: `Bearer ${token}`") == 1
    assert code.count("fetch(") == 1 and "fetch(`${API_URL}${path}`" in code
    assert not re.search(r"[?&](token|api_token|auth)=", code)
