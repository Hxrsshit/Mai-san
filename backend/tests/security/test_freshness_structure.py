"""Stage 5A.1 §: structural audit.

Properties asserted over the source itself. They are here because some of the
guarantees are about what the code *cannot* do, and the only way to test an
absence is to go and look for it.

AST wherever an identifier or a string constant is what matters -- a substring
scan reads docstrings and comments, and the freshness module's docstring
necessarily quotes the very sentences it exists to route.
"""

import ast
import pathlib

import pytest

FRESHNESS = "app/orchestration/freshness.py"


def parse(relative: str) -> ast.Module:
    return ast.parse(pathlib.Path(relative).read_text())


def imported_names(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def called_names(tree: ast.Module):
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


def code_strings(tree: ast.Module):
    """Every string constant that is not a docstring."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                yield node.value


# --- Freshness executes nothing ------------------------------------------------


def test_freshness_executes_no_tool() -> None:
    """§: freshness logic must not directly execute tools."""
    tree = parse(FRESHNESS)

    for name in imported_names(tree):
        for forbidden in ("app.execution", "app.tools", "app.integrations",
                          "app.llm", "app.research.service", "app.calendar",
                          "app.mail"):
            assert not name.startswith(forbidden), name

    for call in called_names(tree):
        base = call.split(".")[-1]
        assert base not in {
            "create", "approve", "run", "execute", "dispatch", "handle",
            "propose_current_information", "run_returning_outcome",
        }, call


def test_freshness_reaches_no_network_and_no_database() -> None:
    """§: no direct HTTP client, no new network destination, no DB call."""
    tree = parse(FRESHNESS)

    for name in imported_names(tree):
        for forbidden in ("httpx", "requests", "aiohttp", "socket", "urllib",
                          "http.client", "sqlalchemy", "asyncpg", "psycopg"):
            assert not (name == forbidden or name.startswith(forbidden + ".")), name

    for value in code_strings(tree):
        for forbidden in ("http://", "https://", "tavily", "googleapis",
                          "gmail.googleapis", "api.groq", "anthropic"):
            assert forbidden not in value.lower(), value[:60]


def test_freshness_runs_no_model_call() -> None:
    """§: do not add an LLM call to decide whether someone said "latest"."""
    tree = parse(FRESHNESS)

    for name in imported_names(tree):
        assert "llm" not in name.split("."), name
        assert "provider" not in name, name

    for call in called_names(tree):
        base = call.split(".")[-1]
        assert base not in {"complete", "chat", "generate", "invoke"}, call


def test_freshness_has_no_dynamic_code_or_attribute_lookup() -> None:
    """The string-to-code primitives, absent from a routing layer."""
    for call in called_names(parse(FRESHNESS)):
        base = call.split(".")[-1]
        if call.startswith("re."):
            continue
        assert base not in {
            "eval", "exec", "compile", "getattr", "setattr", "delattr",
            "globals", "locals", "vars", "import_module", "__import__",
        }, call


# --- No new capability surface ---------------------------------------------------


def test_freshness_names_no_tool_and_no_integration() -> None:
    """§: no new unrestricted capability surface."""
    source = pathlib.Path(FRESHNESS).read_text()
    for forbidden in ("web_search", "calendar_list_events", "gmail_list_messages",
                      "gmail_get_message", "create_text_file",
                      "ExecutionRequest", "ExecutionService", "SecureHttpClient",
                      "NetworkPolicy", "register"):
        assert forbidden not in source, forbidden


def test_the_registries_are_unchanged_by_this_stage() -> None:
    """§: no new network destination, no new tool, no new integration."""
    from app.execution.tools import get_executable_registry
    from app.integrations.registry import get_integration_registry

    assert get_integration_registry().names() == (
        "google_calendar", "google_gmail", "web_search",
    )
    assert get_executable_registry().names() == (
        "calendar_list_events", "create_text_file", "gmail_get_message",
        "gmail_list_messages", "list_workspace_files", "read_text_file",
        "web_search",
    )


def test_the_permitted_outbound_hosts_are_unchanged() -> None:
    """Every destination Stage 5A.1 may reach already existed."""
    from app.integrations.google_calendar import api_policy as calendar_policy
    from app.integrations.google_gmail import api_policy as gmail_policy
    from app.integrations.oauth import token_policy
    from app.integrations.web_search import PROVIDERS

    hosts = set()
    for policy in (calendar_policy(), gmail_policy(), token_policy()):
        hosts |= set(policy.allowed_hosts)
    hosts |= {descriptor.host for descriptor in PROVIDERS.values()}

    assert hosts == {
        "www.googleapis.com", "gmail.googleapis.com", "oauth2.googleapis.com",
        "api.tavily.com", "api.search.brave.com",
    }, sorted(hosts)


# --- The assessment is advisory ------------------------------------------------------


def test_the_assessment_type_carries_no_authorization_field() -> None:
    """§: freshness must not grant anything, and cannot express a grant."""
    from app.orchestration.freshness import FreshnessAssessment

    assert set(FreshnessAssessment._fields) == {
        "requirement", "reason", "source", "subject",
    }


def test_only_the_chat_layer_reads_a_freshness_assessment() -> None:
    """One consumer, so there is one place where routing is decided."""
    consumers = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if str(path) == FRESHNESS:
            continue
        for name in imported_names(ast.parse(path.read_text())):
            if "freshness" in name:
                consumers.append(str(path))

    assert sorted(set(consumers)) == ["app/services/chat_service.py"], consumers


def test_no_other_module_reimplements_freshness_keywords() -> None:
    """§: do not scatter `if latest` / `if today` through unrelated tools.

    The concept lives in one module. A second place testing for "latest"
    would be a second policy, drifting from the first, in a layer that has no
    business making routing decisions.
    """
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        relative = str(path)
        if relative in {FRESHNESS, "app/language/normalise.py"}:
            continue
        # The calendar and mail grammars legitimately resolve *time windows*;
        # what must not spread is the freshness *decision*.
        if relative in {
            "app/orchestration/calendar_language.py",
            "app/orchestration/mail_language.py",
            "app/workflows/briefing.py",
            "app/research/language.py",
        }:
            continue
        for value in code_strings(ast.parse(path.read_text())):
            lowered = value.lower()
            if "latest" in lowered and ("search" in lowered or "web" in lowered):
                offenders.append((relative, value[:50]))

    assert offenders == [], offenders


# --- No research recursion ------------------------------------------------------------


def test_nothing_assesses_freshness_on_a_tool_result() -> None:
    """§: no automatic research loop.

    The loop would need an edge from tool output back into intent assessment.
    There is one call site and it passes the user's message, so the edge does
    not exist -- asserted here rather than argued.
    """
    import inspect

    from app.services.chat_service import ChatService

    source = inspect.getsource(ChatService)
    assert source.count("assess_freshness(") == 1
    assert "assess_freshness(reading.text)" in source


def test_the_research_service_never_assesses_freshness() -> None:
    """The layer that *has* tool output must not be able to ask."""
    for name in imported_names(parse("app/research/service.py")):
        assert "freshness" not in name, name
