"""Stage 5A.2 §: structural audit of the conversation-history boundary.

Properties asserted over the source. Every one is about an *absence* -- a path
that must not exist -- and the only way to test an absence is to look for it.

AST wherever an identifier matters; a substring scan reads docstrings, and
this stage's docstrings necessarily quote the very JSON they exist to refuse.
"""

import ast
import pathlib

import pytest

CONTRACT = "app/synthesis/contract.py"
CHAT = "app/services/chat_service.py"


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


# --- Recognising a tool call cannot run one -----------------------------------


def test_the_contract_reaches_no_execution_path() -> None:
    """§: a model's malformed output cannot grant itself tool authority.

    The module that *recognises* a tool call must have no way to dispatch
    one -- otherwise the recognition itself becomes the vulnerability.
    """
    tree = parse(CONTRACT)

    for name in imported_names(tree):
        for forbidden in ("app.execution", "app.tools", "app.integrations",
                          "app.llm", "app.research", "app.calendar", "app.mail",
                          "app.workflows", "httpx", "requests", "sqlalchemy",
                          "subprocess", "socket"):
            assert not name.startswith(forbidden), name

    for call in called_names(tree):
        base = call.split(".")[-1]
        assert base not in {
            "create", "approve", "run", "execute", "dispatch", "handle",
            "generate_response", "add_message", "commit",
        }, call


def test_the_contract_has_no_dynamic_code_execution() -> None:
    """Parsing untrusted model output must not be able to run it."""
    # `re.compile` and `json.loads` are named exemptions. Matching on the
    # bare suffix flagged `re.compile` -- the pattern builder this module is
    # made of -- which is the recurring trap of suffix matching.
    permitted = {"re.compile", "re.match", "re.sub", "json.loads"}
    for call in called_names(parse(CONTRACT)):
        if call in permitted:
            continue
        base = call.split(".")[-1]
        assert base not in {
            "eval", "exec", "compile", "getattr", "setattr",
            "__import__", "import_module", "literal_eval",
        }, call


def test_json_is_parsed_not_evaluated() -> None:
    """`json.loads`, never `eval`. Stated because the temptation exists."""
    source = pathlib.Path(CONTRACT).read_text()
    assert "json.loads" in source
    assert "eval(" not in source


# --- The history boundary ---------------------------------------------------------


def test_assistant_history_is_written_in_one_module_only() -> None:
    """§: audit exactly where assistant messages are written."""
    writers = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if "role=MessageRole.ASSISTANT" in path.read_text():
            writers.append(str(path))

    assert writers == [CHAT], writers


def test_there_are_exactly_two_assistant_write_sites() -> None:
    """One application-written, one validated. A third needs an argument."""
    source = pathlib.Path(CHAT).read_text()
    assert source.count("role=MessageRole.ASSISTANT") == 2


def test_the_model_output_is_never_stored_without_validation() -> None:
    """`llm_response.content` reaches exactly one place: the validator."""
    # By AST: a text count reads the comment above the call site, which
    # quotes `llm_response.content` while explaining why it is no longer
    # stored directly.
    tree = parse(CHAT)
    reads = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "content"
        and isinstance(node.value, ast.Name)
        and node.value.id == "llm_response"
    ]
    assert len(reads) == 1, f"{len(reads)} reads of llm_response.content"

    source = pathlib.Path(CHAT).read_text()
    assert "validate_response(llm_response.content)" in source
    # And never straight into the reply.
    assert "reply = llm_response.content" not in source


def test_no_provider_envelope_can_reach_history() -> None:
    """§: do not reintroduce raw provider output to solve this."""
    import dataclasses

    from app.llm.base import LLMResponse

    assert {f.name for f in dataclasses.fields(LLMResponse)} == {
        "content", "model", "finish_reason", "usage",
    }

    source = pathlib.Path(CHAT).read_text()
    for forbidden in ("llm_response.raw", "response.raw", ".to_dict()["):
        assert forbidden not in source, forbidden


# --- No new capability ---------------------------------------------------------------


def test_the_stage_added_no_tool_integration_or_destination() -> None:
    """§: this is a response-contract stage, not a capability stage."""
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


def test_only_the_chat_service_consumes_the_contract() -> None:
    """One consumer, so there is one place the boundary is enforced."""
    consumers = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if str(path).startswith("app/synthesis/"):
            continue
        for name in imported_names(ast.parse(path.read_text())):
            if "synthesis" in name:
                consumers.append(str(path))

    assert sorted(set(consumers)) == [CHAT], consumers


# --- Recovery is bounded ----------------------------------------------------------------


def test_recovery_cannot_recurse() -> None:
    """§: do not recursively call synthesis indefinitely.

    The recovery method must not call itself. Checked structurally because a
    behavioural test can only prove it did not recurse *this time*.
    """
    tree = parse(CHAT)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_recover_synthesis":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                target = inner.func
                name = (
                    target.attr if isinstance(target, ast.Attribute)
                    else getattr(target, "id", "")
                )
                assert name != "_recover_synthesis", "recovery calls itself"
        break
    else:
        pytest.fail("_recover_synthesis not found")


def test_the_chat_turn_generates_at_most_twice() -> None:
    """One synthesis plus one recovery. Never a third call site."""
    source = pathlib.Path(CHAT).read_text()
    assert source.count("generate_response(") == 2
