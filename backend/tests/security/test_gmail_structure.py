"""Stage 5B -- structural audit.

Properties asserted over the whole repository, not only the changed files.
Some guarantees are about what the code *cannot* do, and the only way to test
an absence is to go looking for it.

AST wherever an identifier is what matters: a substring scan reads comments,
and these modules discuss at length the things they must not do.
"""

import ast
import pathlib

import pytest

APP = pathlib.Path("app")
FRONTEND = pathlib.Path("../frontend")

#: Modules that legitimately hold Gmail logic.
GMAIL_MODULES = (
    "app/integrations/google_gmail.py",
    "app/integrations/gmail_schemas.py",
    "app/orchestration/mail_language.py",
    "app/mail/service.py",
    "app/mail/schemas.py",
    "app/execution/gmail_tools.py",
    "app/schemas/mail.py",
)

#: The only modules that may open a socket.
NETWORK_OWNERS = ("app/integrations/http_client.py", "app/llm/gateway.py")


def parse(path) -> ast.Module:
    return ast.parse(pathlib.Path(path).read_text())


def imported(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def called(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if parts:
            yield ".".join(reversed(parts))


def strings(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


# --- No direct network, no arbitrary URL --------------------------------------


@pytest.mark.parametrize("module", GMAIL_MODULES)
def test_no_gmail_module_imports_an_http_client(module) -> None:
    """§: all external traffic goes through the existing network boundary."""
    for name in imported(parse(module)):
        for banned in ("httpx", "requests", "aiohttp", "socket", "urllib.request",
                       "urllib3", "http.client", "ssl"):
            assert not (name == banned or name.startswith(banned + ".")), (
                module, name
            )


@pytest.mark.parametrize("module", GMAIL_MODULES)
def test_no_gmail_module_can_run_a_shell(module) -> None:
    tree = parse(module)
    for name in imported(tree):
        assert name not in {"subprocess", "os.system", "pty", "shlex",
                            "multiprocessing", "ctypes"}, (module, name)
    permitted = {"re.compile", "re.sub", "re.split", "re.escape", "re.search",
                 "re.match"}
    for call in called(tree):
        if call in permitted:
            continue
        assert call not in {
            "eval", "exec", "compile", "__import__", "getattr", "setattr",
            "delattr", "globals", "locals", "vars", "system", "popen",
        }, (module, call)


def test_only_one_module_names_a_gmail_url() -> None:
    """§: no arbitrary Gmail URL construction.

    The host and the path are constants in one file. Everything else composes
    an id onto that constant, so there is no second place a destination could
    be built.
    """
    owners = []
    for path in sorted(APP.rglob("*.py")):
        for value in strings(parse(path)):
            if "gmail.googleapis.com" in value or "/gmail/v1/" in value:
                owners.append(str(path))

    assert sorted(set(owners)) == ["app/integrations/google_gmail.py"], owners


def test_the_gmail_endpoint_is_built_from_constants() -> None:
    from app.integrations import google_gmail

    assert google_gmail.API_HOST == "gmail.googleapis.com"
    assert google_gmail.MESSAGES_ENDPOINT == (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    )
    # No other user, no other mailbox, no version selector.
    assert "/users/me/" in google_gmail.MESSAGES_ENDPOINT


def test_no_gmail_module_names_an_http_write_verb() -> None:
    """§: only GET operations exist."""
    for module in GMAIL_MODULES:
        for value in strings(parse(module)):
            assert value not in {"POST", "PUT", "PATCH", "DELETE", "HEAD"}, module


def test_only_the_owning_modules_open_a_socket() -> None:
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        relative = str(path)
        if relative in NETWORK_OWNERS or relative.startswith("app/integrations/"):
            continue
        for name in imported(ast.parse(path.read_text())):
            if name in {"httpx", "aiohttp", "requests"} or name.startswith(
                ("httpx.", "aiohttp.", "requests.")
            ):
                offenders.append((relative, name))
    assert offenders == [], offenders


# --- No generic Gmail request operation ---------------------------------------


def test_there_is_no_generic_gmail_request_operation() -> None:
    """§: no `gmail.request(url, method, headers, body)`."""
    from app.integrations.registry import get_integration_registry

    integration = get_integration_registry().get("google_gmail")
    names = [op.name for op in integration.declare_operations()]

    assert names == ["gmail_list_messages", "gmail_get_message"]
    for generic in ("request", "gmail_request", "call", "fetch", "http",
                    "raw", "query", "execute"):
        assert generic not in names, generic


def test_no_gmail_operation_accepts_a_url_method_or_header() -> None:
    """The arguments are fields, never a request description."""
    from app.tools.catalog import GmailGetMessageArguments, GmailListMessagesArguments

    assert set(GmailListMessagesArguments.model_fields) == {
        "sender", "subject_terms", "text_terms", "unread_only",
        "newer_than_days", "max_results", "body_count",
    }
    assert set(GmailGetMessageArguments.model_fields) == {"message_id"}

    for schema in (GmailListMessagesArguments, GmailGetMessageArguments):
        for forbidden in ("url", "endpoint", "method", "headers", "host",
                          "path", "q", "scope", "token", "body", "params"):
            assert forbidden not in schema.model_fields, (schema, forbidden)


# --- No credential leakage paths ----------------------------------------------


def test_only_the_integration_reads_the_gmail_token() -> None:
    """§: no credential leakage paths.

    The token store is reached from the integration and the OAuth routes and
    nowhere else -- in particular not from the mail service, the recogniser or
    the schemas, none of which has any use for a credential.
    """
    readers = []
    for path in sorted(APP.rglob("*.py")):
        for name in imported(parse(path)):
            if "token_store" in name:
                readers.append(str(path))

    assert sorted(set(readers)) == [
        "app/api/routes/integrations.py",
        "app/integrations/google_calendar.py",
        "app/integrations/google_gmail.py",
        "app/integrations/oauth.py",
    ], readers


@pytest.mark.parametrize(
    "module",
    ["app/mail/service.py", "app/mail/schemas.py",
     "app/orchestration/mail_language.py", "app/schemas/mail.py",
     "app/execution/gmail_tools.py"],
)
def test_no_credential_name_appears_outside_the_integration(module) -> None:
    for value in strings(parse(module)):
        for banned in ("access_token", "refresh_token", "client_secret",
                       "Authorization", "Bearer", "ya29."):
            assert banned not in value, (module, banned)


# --- No email content persistence ----------------------------------------------


def test_no_gmail_module_writes_to_the_database() -> None:
    """§: no email content persistence.

    The mail service creates and approves `Execution` rows -- which carry the
    *query*, never the result -- and nothing in the Gmail path constructs a
    model, a memory, an entity or a relationship.
    """
    forbidden_models = {"Memory", "Entity", "Relationship", "Message",
                        "MemoryEntity", "RelationshipEvidence"}
    for module in GMAIL_MODULES:
        tree = parse(module)
        for call in called(tree):
            assert call.split(".")[-1] not in forbidden_models, (module, call)
        for name in imported(tree):
            assert "memory" not in name.split("."), (module, name)
            assert "retrieval" not in name.split("."), (module, name)


def test_the_mail_result_is_not_a_database_model() -> None:
    """It lives for one request. Nothing persists it."""
    from sqlalchemy.orm import DeclarativeBase

    from app.mail.schemas import MailResult

    assert not issubclass(MailResult, DeclarativeBase)
    assert not hasattr(MailResult, "__tablename__")


def test_stage_5b_added_no_migration() -> None:
    """§: do not create unnecessary tables."""
    versions = sorted(p.name for p in pathlib.Path("alembic/versions").glob("*.py"))
    assert versions == [
        "0001_initial_schema.py", "0002_memories.py",
        "0003_memory_unique_constraint.py", "0004_entities.py",
        "0005_relationships.py", "0006_knowledge_conflicts.py",
        "0007_execution.py", "0008_execution_conversation.py",
        "0009_workflows.py",
    ], versions


# --- No automatic Gmail-triggered execution -------------------------------------


def test_nothing_reads_mail_without_a_turn() -> None:
    """§: no background monitoring, no watch, no history sync.

    Gmail's push and incremental-sync surfaces are how background mailbox
    monitoring gets built. None of them is named anywhere.
    """
    # String constants and identifiers, by AST. A raw scan flagged the
    # integration's own docstring, which lists these surfaces precisely
    # *because* they are absent -- the recurring lesson that substring
    # searches read the prose explaining the guarantee.
    for path in sorted(APP.rglob("*.py")):
        tree = parse(path)
        for value in strings(tree):
            if value.startswith("\n") or len(value) > 200:
                # A docstring, not an endpoint.
                continue
            for banned in ("users.watch", "users/watch", "users.stop",
                           "history.list", "users/history", "topicName",
                           "labelFilterAction"):
                assert banned not in value, (path, banned, value[:60])
        for name in imported(tree):
            assert "pubsub" not in name, (path, name)


def test_the_mail_service_is_only_reachable_from_a_chat_turn() -> None:
    importers = []
    for path in sorted(APP.rglob("*.py")):
        if str(path).startswith("app/mail/"):
            continue
        for name in imported(parse(path)):
            if name.startswith("app.mail.service"):
                importers.append(str(path))

    assert sorted(set(importers)) == ["app/services/chat_service.py"], importers


def test_no_scheduler_or_background_task_reads_mail() -> None:
    for path in sorted(APP.rglob("*.py")):
        tree = parse(path)
        names = set(imported(tree))
        if not any("mail" in n.split(".") for n in names):
            continue
        for banned in ("apscheduler", "celery", "asyncio.create_task",
                       "BackgroundScheduler", "crontab"):
            assert banned not in path.read_text(), (path, banned)


# --- Frontend ------------------------------------------------------------------


def test_the_frontend_stores_no_credential() -> None:
    """§: no Gmail tokens in browser storage, no direct Gmail calls."""
    if not FRONTEND.exists():
        pytest.skip("frontend directory is not present in this checkout")

    for path in list(FRONTEND.rglob("*.ts")) + list(FRONTEND.rglob("*.tsx")):
        # Comment lines are skipped. The panel's own docstring says it writes
        # to neither store, which a raw scan read as it writing to both.
        code = "\n".join(
            line for line in path.read_text().splitlines()
            if not line.lstrip().startswith(("//", "*", "/*"))
        )
        for banned in ("localStorage.", "sessionStorage.", "document.cookie",
                       "indexedDB."):
            assert banned not in code, (path, banned)
        for banned in ("gmail.googleapis.com", "oauth2.googleapis.com",
                       "accounts.google.com", "googleapis.com",
                       "client_secret", "access_token", "refresh_token"):
            assert banned not in code, (path, banned)


def test_the_frontend_talks_only_to_the_mai_backend() -> None:
    if not FRONTEND.exists():
        pytest.skip("frontend directory is not present in this checkout")

    api = (FRONTEND / "lib" / "api.ts").read_text()
    # Every path is relative to the configured backend base URL.
    assert "NEXT_PUBLIC_API_URL" in api
    for path in list(FRONTEND.rglob("*.tsx")):
        text = path.read_text()
        assert "fetch(\"http" not in text, path
        assert "fetch('http" not in text, path
