"""Stage 4H §22 -- structural audit.

Properties asserted over the source itself, by AST where a name is what
matters and by text where a literal is. They are here because some guarantees
are about what the code *cannot* do, and the only way to test an absence is to
look for it.

AST rather than substring search wherever an identifier is involved: a
substring scan reads docstrings and comments, and this file is full of both.
That lesson cost several false failures in earlier stages.
"""

import ast
import pathlib

import pytest

#: The modules Stage 4H added or changed in a security-relevant way.
COMPOSITION_MODULES = (
    "app/workflows/briefing.py",
    "app/workflows/service.py",
    "app/workflows/plans.py",
    "app/workflows/schemas.py",
    "app/workflows/limits.py",
    "app/workflows/states.py",
)

#: Modules that may legitimately open a socket, and nothing else may.
NETWORK_OWNERS = (
    "app/integrations/http_client.py",
    "app/llm/gateway.py",
)


def parse(relative: str) -> ast.Module:
    return ast.parse(pathlib.Path(relative).read_text())


def imported_names(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def called_names(tree: ast.Module):
    """Every dotted callee name, as written."""
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


# --- No execution primitives -------------------------------------------------


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_no_shell_or_subprocess_anywhere_in_composition(module) -> None:
    tree = parse(module)
    forbidden = {
        "subprocess", "os.system", "pty", "shlex", "commands",
        "multiprocessing", "ctypes",
    }
    for name in imported_names(tree):
        assert name not in forbidden, (module, name)

    # Bare names only. An earlier version used `endswith`, which flagged
    # `re.compile` -- the pattern-compiling call this whole layer is built
    # from. A test that cannot tell `re.compile` from `compile` is a test
    # that will be silenced rather than fixed.
    for call in called_names(tree):
        assert call not in {
            "system", "popen", "exec", "eval", "compile", "__import__",
            "spawn", "os.system", "os.popen", "os.spawnl",
        }, (module, call)


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_no_dynamic_code_or_attribute_lookup(module) -> None:
    """§2: nothing here turns a string into behaviour.

    `getattr` is included deliberately. It is the string-to-code primitive,
    and a layer that decides *what to run* must not have it -- the dispatcher
    makes the same argument about itself.
    """
    tree = parse(module)
    #: `re.compile` is the pattern builder these grammars are made of, and is
    #: not the `compile` builtin. Named explicitly rather than matched by
    #: suffix, so the exemption is one call and not a whole family.
    permitted = {"re.compile", "re.sub", "re.split", "re.escape", "re.search"}
    for call in called_names(tree):
        if call in permitted:
            continue
        assert call not in {
            "eval", "exec", "compile", "getattr", "setattr", "delattr",
            "globals", "locals", "vars", "import_module", "__import__",
        }, (module, call)


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_no_network_library_reaches_the_composition_layer(module) -> None:
    """§13/§22: no direct provider call, no arbitrary endpoint."""
    tree = parse(module)
    for name in imported_names(tree):
        for banned in ("httpx", "requests", "aiohttp", "socket",
                       "urllib.request", "urllib3", "http.client"):
            assert not (name == banned or name.startswith(banned + ".")), (
                module, name
            )


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_the_composition_layer_names_no_destination(module) -> None:
    """A URL in this layer would be a destination nobody reviewed."""
    for node in ast.walk(parse(module)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for banned in ("http://", "https://", "googleapis.com",
                           "tavily.com", "api.groq.com", "api.anthropic.com",
                           "accounts.google.com"):
                assert banned not in node.value, (module, banned, node.value[:60])


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_no_http_verb_is_chosen_in_the_composition_layer(module) -> None:
    """Methods are the network policy's business."""
    tree = parse(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in {
                "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
            }, (module, node.value)


# --- No filesystem or credential access --------------------------------------


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_no_filesystem_access_outside_the_executor(module) -> None:
    """§6: no unrestricted filesystem access.

    The artifact is written by `create_text_file` through the Stage 4E
    dispatcher, which resolves every path inside the workspace. The
    composition layer names a path and never touches a file.
    """
    tree = parse(module)
    for name in imported_names(tree):
        assert name not in {"shutil", "tempfile", "glob", "io"}, (module, name)

    for call in called_names(tree):
        base = call.split(".")[-1]
        assert base not in {
            "open", "unlink", "rmtree", "mkdir", "makedirs", "remove",
            "write_text", "read_text", "write_bytes", "read_bytes",
        }, (module, call)


@pytest.mark.parametrize("module", COMPOSITION_MODULES)
def test_no_credential_is_read_in_the_composition_layer(module) -> None:
    """§14: credentials live behind the integration boundary."""
    tree = parse(module)
    for name in imported_names(tree):
        assert "token_store" not in name, (module, name)
        assert "credentials" not in name, (module, name)

    # String *constants*, by AST. A raw text scan flagged the legitimate
    # import of `AuthorizationStatus` for containing "Authorization" -- the
    # recurring lesson that substring scans read identifiers and prose.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for banned in ("access_token", "refresh_token", "client_secret",
                           "Authorization", "Bearer", "SEARCH_API_KEY",
                           "GOOGLE_OAUTH_CLIENT_SECRET"):
                assert banned not in node.value, (module, banned)


# --- No new authority --------------------------------------------------------


def test_no_calendar_write_operation_exists_anywhere() -> None:
    """§6/§9: calendar access is read-only, by absence."""
    from app.execution.tools import get_executable_registry
    from app.integrations.google_calendar import GoogleCalendarIntegration
    from app.tools.registry import get_registry

    executable = get_executable_registry()
    registered = get_registry()
    for write in ("calendar_create_event", "calendar_update_event",
                  "calendar_delete_event", "calendar_insert",
                  "calendar_patch", "calendar_write"):
        assert executable.get(write) is None, write
        assert registered.get(write) is None, write

    # The integration declares exactly one operation, and it has no effect.
    integration = GoogleCalendarIntegration()
    operations = integration.declare_operations()
    assert [op.name for op in operations] == ["calendar_list_events"]
    assert all(op.has_side_effect is False for op in operations)


def test_the_composition_registers_no_tool_and_no_integration() -> None:
    """§2: the model may not create capabilities -- nor may this layer."""
    for module in COMPOSITION_MODULES:
        for call in called_names(parse(module)):
            base = call.split(".")[-1]
            assert base != "register", (module, call)


def test_composition_adds_no_api_endpoint() -> None:
    """§6: no generic agent or workflow execution endpoint."""
    import app.main as main_module
    from app.main import create_app

    paths = {route.path for route in create_app().routes}
    for banned in ("/api/agent", "/api/execute", "/api/run", "/api/compose",
                   "/api/workflows", "/api/workflow", "/api/tools/run"):
        assert banned not in paths, banned


def test_the_step_kinds_and_their_tools_are_a_closed_declared_set() -> None:
    """§3: composition composes; it does not extend."""
    from app.tools.registry import get_registry
    from app.workflows.schemas import StepKind, TOOL_FOR_KIND

    assert set(StepKind) == {
        StepKind.CALENDAR, StepKind.RESEARCH, StepKind.SYNTHESISE,
        StepKind.ARTIFACT,
    }
    registry = get_registry()
    for kind, tool in TOOL_FOR_KIND.items():
        assert registry.get(tool) is not None, (kind, tool)

    # The one kind with no tool cannot be dispatched.
    assert StepKind.SYNTHESISE not in TOOL_FOR_KIND


def test_provider_selection_is_not_reachable_from_the_composition_layer() -> None:
    """§13: provider choice stays application-controlled."""
    for module in COMPOSITION_MODULES:
        tree = parse(module)
        for name in imported_names(tree):
            assert "llm" not in name.split("."), (module, name)
            assert "gateway" not in name, (module, name)

        # By AST. A text scan flagged the word "groq" inside a comment
        # giving an example request -- prose about a provider is not a
        # provider selection.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for banned in ("LLM_PROVIDER", "ProviderMode"):
                    assert banned not in node.value, (module, banned)
            if isinstance(node, ast.Name):
                assert node.id not in {
                    "LLM_PROVIDER", "ProviderMode", "LLMGateway", "get_provider",
                }, (module, node.id)


def test_only_the_owning_modules_open_a_socket() -> None:
    """The network boundary, asserted over the whole application.

    Not scoped to Stage 4H: the property worth holding is that nothing
    anywhere has quietly acquired its own HTTP client, and a composition
    stage is a natural moment for that to happen.
    """
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        relative = str(path)
        if relative in NETWORK_OWNERS:
            continue
        for name in imported_names(ast.parse(path.read_text())):
            if name in {"httpx", "aiohttp", "requests"} or name.startswith(
                ("httpx.", "aiohttp.", "requests.")
            ):
                offenders.append((relative, name))

    # `app/integrations/*` build requests through SecureHttpClient and may
    # reference httpx types; anything outside that is a finding.
    unexpected = [
        (module, name) for module, name in offenders
        if not module.startswith("app/integrations/")
    ]
    assert unexpected == [], unexpected
