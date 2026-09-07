"""The application network boundary, asserted across the whole repository.

Stage 4F-B established `SecureHttpClient` and left one deliberate exception:
the LLM provider held its own `httpx.AsyncClient` and never consulted
`NetworkPolicy`. That exception was documented, and a test enumerated it.

A documented exception and an invariant are different things, and only one of
them survives a future developer. Stage 4F-C removed the exception, and this
file replaces the test that recorded it with the stronger claim:

    **There are zero application-level unpoliced outbound HTTP paths.**

The scan is repository-wide and AST-based. It fails if anyone adds a second
network client anywhere under `app/`, which is the point -- the objective is
not "the provider currently uses the secure client" but "a future developer
should have difficulty accidentally creating an unpoliced path".
"""

import ast
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]
APP = BACKEND / "app"

#: The one module permitted to import an HTTP client library.
#:
#: Not a list that grows. A second entry here is the thing this file exists to
#: prevent, and adding one should require arguing for it in review rather than
#: appending a path.
THE_BOUNDARY = "app/integrations/http_client.py"

#: Libraries that can open an outbound connection.
_HTTP_LIBRARIES = frozenset({
    "httpx", "requests", "aiohttp", "urllib3", "http", "httplib2",
    "treq", "tornado", "pycurl", "websockets", "websocket",
})

#: Modules that can reach the network by other means.
_NETWORK_ADJACENT = frozenset({
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "nntplib",
    "xmlrpc", "asyncio.streams",
})

#: Intentional exclusions, each with the reason it is safe.
#:
#: Written as data rather than as special cases inside the loop, so the set of
#: things this test tolerates is readable in one place and a new one has to be
#: added deliberately.
_ALLOWED_MODULE_USES = {
    # Splits a URL into parts so the policy can inspect it. Opens nothing.
    ("urllib.parse", None),
    # `socket.getaddrinfo` asks DNS where a name points -- exactly what the
    # rebinding check needs -- and creates no connection. `policy.py` is the
    # only user, and a separate test asserts it never connects.
    ("socket", "app/integrations/policy.py"),
}


def _modules_imported(path: pathlib.Path):
    """Every module a file imports, by AST rather than by substring.

    Parsed, not grepped: several of these modules discuss the libraries they
    must not use, and a text scan would fail on their own documentation.
    """
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return found


def _application_files():
    for path in sorted(APP.rglob("*.py")):
        yield path, str(path.relative_to(BACKEND))


def _is_allowed(module: str, relative: str) -> bool:
    for allowed_module, allowed_path in _ALLOWED_MODULE_USES:
        if module == allowed_module or module.startswith(allowed_module + "."):
            if allowed_path is None or allowed_path == relative:
                return True
    return False


# --- The invariant ----------------------------------------------------------


def test_exactly_one_module_imports_an_http_client() -> None:
    """Repository-wide. One boundary, and this names it."""
    importers = {}

    for path, relative in _application_files():
        for module in _modules_imported(path):
            root = module.split(".")[0]
            if root in _HTTP_LIBRARIES and not _is_allowed(module, relative):
                importers.setdefault(relative, set()).add(module)

    assert set(importers) == {THE_BOUNDARY}, importers


def test_no_application_module_imports_a_network_adjacent_library() -> None:
    """Mail, FTP, telnet and friends. None of them has a caller, and none may."""
    offenders = {}

    for path, relative in _application_files():
        for module in _modules_imported(path):
            root = module.split(".")[0]
            if root in _NETWORK_ADJACENT and not _is_allowed(module, relative):
                offenders.setdefault(relative, set()).add(module)

    assert offenders == {}, offenders


def test_only_the_policy_resolves_names_and_it_never_connects() -> None:
    """`socket` appears once, for DNS, and opens nothing.

    The distinction matters: `getaddrinfo` is what the rebinding check needs,
    and `socket.socket(...).connect(...)` is a second network path wearing a
    lower-level disguise.
    """
    users = [
        relative for path, relative in _application_files()
        if "socket" in {m.split(".")[0] for m in _modules_imported(path)}
    ]
    assert users == ["app/integrations/policy.py"], users

    source = (APP / "integrations" / "policy.py").read_text()
    assert "socket.getaddrinfo" in source
    for connecting in ("socket.socket", ".connect(", "create_connection"):
        assert connecting not in source, connecting


def test_no_application_module_constructs_its_own_http_client() -> None:
    """Importing is one route in; constructing is the one that matters.

    A module could receive `httpx` through an argument and still build a
    client. This looks for the construction itself, anywhere under `app/`.
    """
    offenders = []

    for path, relative in _application_files():
        if relative == THE_BOUNDARY:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.attr if isinstance(target, ast.Attribute)
                else getattr(target, "id", "")
            )
            if name in {"AsyncClient", "Client", "ClientSession", "Session"}:
                offenders.append(f"{relative}:{name}")

    assert offenders == [], offenders


def test_no_application_module_shells_out_to_a_network_tool() -> None:
    """`subprocess` plus `curl` is an outbound path with extra steps."""
    offenders = []

    for path, relative in _application_files():
        modules = {m.split(".")[0] for m in _modules_imported(path)}
        if modules & {"subprocess", "os"} and relative not in {
            # `os.open` with O_EXCL|O_NOFOLLOW, and workspace path handling.
            # Neither reaches the network; a separate Stage 4E test pins them.
            "app/execution/tools.py", "app/execution/workspace.py",
        }:
            source = path.read_text()
            for tool in ("curl", "wget", "nc ", "netcat", "Popen", "system("):
                if tool in source:
                    offenders.append(f"{relative}:{tool}")

    assert offenders == [], offenders


# --- The boundary actually does its job -------------------------------------


def test_the_boundary_enforces_the_policy_on_every_request() -> None:
    """The complement. Confinement is meaningless if the client is inert.

    Without this, deleting the check would leave every test above green.
    """
    source = (APP / "integrations" / "http_client.py").read_text()

    assert "self._policy.check(" in source
    assert "self._policy.permits(" in source
    # httpx's own redirect handling stays off: it would follow a hop the
    # policy never saw.
    assert "follow_redirects=False" in source


def test_the_boundary_never_disables_tls_verification() -> None:
    """No `verify=False`, and no way for a caller to ask for one."""
    source = (APP / "integrations" / "http_client.py").read_text()

    for disabling in ("verify=False", "verify = False", "VERIFY_NONE",
                      "CERT_NONE", "ssl._create_unverified"):
        assert disabling not in source, disabling


def test_the_boundary_offers_no_unused_write_verb() -> None:
    """GET and POST exist because both have a caller. Nothing else does."""
    from app.integrations.http_client import SecureHttpClient

    assert hasattr(SecureHttpClient, "get")
    assert hasattr(SecureHttpClient, "post_json")
    for absent in ("put", "patch", "delete", "head", "options", "request",
                   "send", "post", "stream"):
        assert not hasattr(SecureHttpClient, absent), absent


# --- Both callers are narrower than the boundary ----------------------------


def test_each_caller_permits_exactly_one_verb_and_one_host() -> None:
    """Method capability is per-policy, so each caller has only its own.

    Stage 4F-B expressed "research cannot submit a form" as the *absence* of a
    `post` method on the client. Stage 4F-C needed POST for the LLM provider,
    and moving the capability onto the policy kept the guarantee while making
    it checkable per instance.

    Stage 4F-D's Tavily support then required POST for research too, because
    Tavily's search API is POST-only. That is a genuine widening and is worth
    stating rather than hiding: a Tavily deployment's research client can
    POST.

    What makes it safe is the second half of this test. Each policy is locked
    to exactly one host, and that host is a constant in application code --
    so the danger POST represented, submitting to *arbitrary* destinations,
    has no destination to reach. A Brave deployment's client still cannot
    POST at all.
    """
    from app.integrations.web_search import PROVIDERS, WebSearchIntegration
    from app.llm.transport import provider_policy

    llm = provider_policy("https://api.groq.com/openai/v1", 30.0)
    assert llm.allowed_methods == frozenset({"POST"})
    assert llm.allowed_hosts == frozenset({"api.groq.com"})

    for name, descriptor in PROVIDERS.items():
        research = WebSearchIntegration._policy(descriptor)
        # Exactly one verb -- never a set that happens to include what it
        # needs alongside what it does not.
        assert research.allowed_methods == frozenset({descriptor.method}), name
        assert research.allowed_hosts == frozenset({descriptor.host}), name
        # And no research policy can reach the model provider, or vice versa.
        assert "api.groq.com" not in research.allowed_hosts, name
        assert descriptor.host not in llm.allowed_hosts, name


def test_the_brave_research_client_still_cannot_write() -> None:
    """The GET-only guarantee survives for the provider it was written for."""
    from app.integrations.web_search import PROVIDERS, WebSearchIntegration

    brave = WebSearchIntegration._policy(PROVIDERS["brave"])

    assert brave.allowed_methods == frozenset({"GET"})
    assert not brave.permits("POST")


def test_neither_caller_permits_a_verb_nothing_uses() -> None:
    from app.integrations.web_search import WebSearchIntegration
    from app.llm.transport import provider_policy

    from app.integrations.web_search import PROVIDERS

    policies = [
        WebSearchIntegration._policy(descriptor)
        for descriptor in PROVIDERS.values()
    ] + [provider_policy("https://api.groq.com/openai/v1", 30.0)]

    for policy in policies:
        for verb in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT",
                     "TRACE"):
            assert not policy.permits(verb), verb


def test_a_policy_that_says_nothing_about_methods_is_read_only() -> None:
    """The default fails closed: silence means GET, never anything."""
    from app.integrations.policy import NetworkPolicy

    policy = NetworkPolicy()

    assert policy.allowed_methods == frozenset({"GET"})
    assert not policy.permits("POST")
