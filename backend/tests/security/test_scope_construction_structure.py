"""Stage 7B L0 (security review M1): where a `KnowledgeScope` and a
`Principal` may be made, by AST.

`KnowledgeScope` is meant to come only from `KnowledgeScope.personal(p)` /
`.organisation(p)` on a `Principal`, and a `Principal` only from the
membership reads in `app.identity.principal` and `app.identity.service`.
Python cannot enforce that -- the constructors are public and the objects
are ordinary dataclasses -- so these are **repository rules**, checked over
every file in `app/`. They do not make either object unforgeable: code that
sets out to defeat them (`exec`, `ctypes`, `gc`, pickling) is a review
matter. What they do is make every ordinary shortcut fail the suite:

- outside its construction sites, a protected class may be named only as a
  type (annotations, `Optional[...]`/`Annotated[...]`, `isinstance`), as a
  plain import, and -- for `KnowledgeScope` -- as the receiver of its two
  trusted factories. Calling it, aliasing it, rebinding it, subclassing it,
  passing it around or fetching it by name are all refused;
- nowhere in `app/` may a frozen value be copied with changes or mutated in
  place: `dataclasses.replace`, `copy.replace`, `__replace__`, `__new__`,
  `object.__setattr__`, `__dict__` / `vars()` writes, `type(x)(...)`,
  `x.__class__(...)` and re-running `x.__init__(...)` (only `super()`).

The checker is tested against the bypasses it exists to catch, so a rule
that silently stops matching fails here.
"""

import ast
from pathlib import Path
from typing import Dict, FrozenSet, List, Set

import pytest

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"

#: Where each protected class is defined; nowhere else may declare a class of that name.
DEFINED_IN: Dict[str, str] = {
    "KnowledgeScope": "app/identity/scope.py",
    "Principal": "app/identity/principal.py",
}
#: Where each protected class may be constructed (and so named freely).
CONSTRUCTION_SITES: Dict[str, FrozenSet[str]] = {
    "KnowledgeScope": frozenset({"app/identity/scope.py"}),
    "Principal": frozenset({"app/identity/principal.py", "app/identity/service.py"}),
}
#: The trusted factories a caller elsewhere may call on the class.
FACTORIES: Dict[str, FrozenSet[str]] = {
    "KnowledgeScope": frozenset({"personal", "organisation"}),
    "Principal": frozenset(),
}
PROTECTED = frozenset(DEFINED_IN)
#: Generic aliases whose subscript is a type position (`Optional[Principal]`).
TYPE_WRAPPERS = frozenset({
    "Annotated", "ClassVar", "Dict", "FrozenSet", "Iterable", "List", "Optional",
    "Sequence", "Set", "Tuple", "Type", "Union",
})
#: Modules whose `replace` builds a changed copy of a frozen dataclass.
COPY_MODULES = frozenset({"dataclasses", "copy"})
#: Attributes that rebuild or mutate a frozen value. Banned everywhere in app/.
MUTATING_ATTRIBUTES = frozenset({"__replace__", "__new__", "__setattr__"})
#: Names that, fetched by string, reach the same thing.
BANNED_BY_NAME = frozenset({"replace", "__replace__", "__new__", "__setattr__",
                            "__dict__", "__class__"}) | PROTECTED
#: The one reader of an object's `__dict__`: the log formatter, over LogRecord.
DICT_READERS = frozenset({"app/core/logging.py"})


def _parents(tree: ast.AST) -> Dict[int, ast.AST]:
    parents: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _type_positions(tree: ast.AST) -> Set[int]:
    """ids of every node in a position that names a type and calls nothing."""
    roots: List[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            for arg in (arguments.posonlyargs + arguments.args + arguments.kwonlyargs
                        + [a for a in (arguments.vararg, arguments.kwarg) if a]):
                if arg.annotation is not None:
                    roots.append(arg.annotation)
            if node.returns is not None:
                roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, ast.Subscript) and _name_of(node.value) in TYPE_WRAPPERS:
            roots.append(node.slice)
        elif (isinstance(node, ast.Call) and _name_of(node.func) in ("isinstance", "issubclass")
              and len(node.args) == 2):
            roots.append(node.args[1])
    positions: Set[int] = set()
    for root in roots:
        positions.update(id(n) for n in ast.walk(root))
    return positions


def offences(source: str, path: str) -> List[str]:
    """Every construction-rule breach in one module. `path` is `app/...`."""
    tree = ast.parse(source)
    parents = _parents(tree)
    typed = _type_positions(tree)
    found: List[str] = []

    def breach(node: ast.AST, what: str) -> None:
        found.append(f"{path}:{getattr(node, 'lineno', 0)}: {what}")

    # Module names bound to `dataclasses` / `copy` (import x / import x as y).
    copy_modules: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in COPY_MODULES:
                    copy_modules.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        # --- the protected classes -------------------------------------------
        if isinstance(node, ast.ClassDef):
            if node.name in PROTECTED and path != DEFINED_IN[node.name]:
                breach(node, f"a second class named {node.name}")
        if isinstance(node, ast.ImportFrom):
            if (node.module in ("app.identity.scope", "app.identity.principal")
                    and any(alias.name == "*" for alias in node.names)):
                breach(node, f"star import from {node.module}")
            for alias in node.names:
                if (alias.name in PROTECTED and alias.asname not in (None, alias.name)
                        and path not in CONSTRUCTION_SITES[alias.name]):
                    breach(node, f"{alias.name} imported as {alias.asname}")
        named = _name_of(node) if isinstance(node, (ast.Name, ast.Attribute)) else ""
        if named in PROTECTED and path not in CONSTRUCTION_SITES[named] and id(node) not in typed:
            parent = parents.get(id(node))
            factory = (
                isinstance(parent, ast.Attribute) and parent.value is node
                and parent.attr in FACTORIES[named]
                and isinstance(parents.get(id(parent)), ast.Call)
                and parents[id(parent)].func is parent
            )
            if not factory:
                breach(node, f"{named} used other than as a type or through its factories")
        if (isinstance(node, ast.Constant) and node.value in PROTECTED
                and path not in CONSTRUCTION_SITES[node.value] and id(node) not in typed):
            breach(node, f"{node.value!r} named by string")

        # --- copying or mutating a frozen value, anywhere ---------------------
        if isinstance(node, ast.ImportFrom) and node.module in COPY_MODULES:
            for alias in node.names:
                if alias.name in ("replace", "*"):
                    breach(node, f"from {node.module} import {alias.name}")
        if (isinstance(node, ast.Attribute) and node.attr == "replace"
                and isinstance(node.value, ast.Name) and node.value.id in copy_modules):
            breach(node, f"{node.value.id}.replace")
        if isinstance(node, ast.Attribute) and node.attr in MUTATING_ATTRIBUTES:
            breach(node, f".{node.attr}")
        if isinstance(node, ast.Attribute) and node.attr == "__dict__" and path not in DICT_READERS:
            breach(node, ".__dict__")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "vars":
                breach(node, "vars()")
            if isinstance(func, ast.Attribute) and func.attr == "__class__":
                breach(node, "x.__class__(...)")
            # A frozen dataclass's __init__ assigns through object.__setattr__,
            # so calling it again rewrites the value in place. super() only.
            if (isinstance(func, ast.Attribute) and func.attr == "__init__"
                    and not (isinstance(func.value, ast.Call)
                             and _name_of(func.value.func) == "super")):
                breach(node, "x.__init__(...) outside super()")
            if (isinstance(func, ast.Call) and isinstance(func.func, ast.Name)
                    and func.func.id == "type" and len(func.args) == 1):
                breach(node, "type(x)(...)")
            if (_name_of(func) in ("getattr", "setattr", "delattr") and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in BANNED_BY_NAME):
                breach(node, f"{_name_of(func)}(..., {node.args[1].value!r})")
            if (_name_of(func) in ("__import__", "import_module") and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in COPY_MODULES):
                breach(node, f"dynamic import of {node.args[0].value}")
    return sorted(set(found))


def app_sources():
    for path in sorted(APP.rglob("*.py")):
        yield str(path.relative_to(BACKEND)), path.read_text(encoding="utf-8")


# ============================================================================
# The repository
# ============================================================================


def test_no_module_in_app_breaks_the_construction_rules() -> None:
    found = [o for path, source in app_sources() for o in offences(source, path)]
    assert found == []


def test_the_walk_is_not_vacuous() -> None:
    sources = dict(app_sources())
    assert len(sources) > 100
    for path in DEFINED_IN.values():
        assert path in sources
    # The construction sites really construct: moved anywhere else, each one
    # would be refused. (So the rule is about *where*, and it sees the call.)
    for site, protected in (("app/identity/service.py", "Principal"),
                            ("app/identity/principal.py", "Principal"),
                            ("app/identity/scope.py", "KnowledgeScope")):
        moved = offences(sources[site], "app/elsewhere.py")
        assert any(protected in o for o in moved), (site, moved)
    # And the module that only *uses* Principal as a type is clean as it is.
    assert offences(sources["app/api/deps.py"], "app/api/deps.py") == []
    assert "Principal" in sources["app/api/deps.py"]


# ============================================================================
# The checker refuses the bypasses it exists for
# ============================================================================

BYPASSES = {
    "direct scope": "from app.identity.scope import KnowledgeScope\nKnowledgeScope(o, s, v)\n",
    "aliased import": "from app.identity.scope import KnowledgeScope as KS\nKS.personal(p)\n",
    "module attribute": "from app.identity import scope\nscope.KnowledgeScope(o, s, v)\n",
    "rebinding": "from app.identity.scope import KnowledgeScope\nK = KnowledgeScope\nK(o, s, v)\n",
    "partial": "import functools\nfunctools.partial(KnowledgeScope, o)\n",
    "subclass": "from app.identity.scope import KnowledgeScope\nclass Mine(KnowledgeScope):\n    pass\n",
    "shadow class": "class KnowledgeScope:\n    pass\n",
    "__new__": "KnowledgeScope.__new__(KnowledgeScope)\n",
    "unbound __init__": "KnowledgeScope.__init__(scope, o, s, v)\n",
    "getattr by name": "getattr(scope_module, 'KnowledgeScope')(o, s, v)\n",
    "star import": "from app.identity.scope import *\n",
    "dataclasses.replace": "import dataclasses\ndataclasses.replace(scope, scope_id=other)\n",
    "aliased dataclasses": "import dataclasses as dc\ndc.replace(scope, scope_id=other)\n",
    "from dataclasses import replace": "from dataclasses import replace\nreplace(scope, scope_id=x)\n",
    "replace aliased": "from dataclasses import replace as r\nr(scope, scope_id=x)\n",
    "copy.replace": "import copy\ncopy.replace(scope, scope_id=other)\n",
    "getattr replace": "import dataclasses\ngetattr(dataclasses, 'replace')(scope, scope_id=x)\n",
    "dynamic import": "import importlib\nimportlib.import_module('dataclasses').replace(scope)\n",
    "__replace__": "scope.__replace__(scope_id=other)\n",
    "object.__setattr__": "object.__setattr__(scope, 'scope_id', other)\n",
    "__dict__ write": "scope.__dict__['scope_id'] = other\n",
    "vars write": "vars(scope)['scope_id'] = other\n",
    "re-init in place": "scope.__init__(o, other, v)\n",
    "type(x)(...)": "type(scope)(o, s, v)\n",
    "__class__(...)": "scope.__class__(o, s, v)\n",
    "direct principal": ("from app.identity.principal import Principal\n"
                         "Principal(member_id=m, organisation_id=o, role=r)\n"),
    "principal via module": "from app.identity import principal\nprincipal.Principal(m, o, r)\n",
    "principal factory-style": "Principal.personal(m)\n",
    "principal rebinding": "P = Principal\n",
}


@pytest.mark.parametrize("source", list(BYPASSES.values()), ids=list(BYPASSES))
def test_each_bypass_is_refused(source) -> None:
    assert offences(source, "app/services/elsewhere.py"), source


ALLOWED = {
    "factory and annotations": (
        "from app.identity.scope import KnowledgeScope\n"
        "def f(p) -> KnowledgeScope:\n"
        "    scope: KnowledgeScope = KnowledgeScope.personal(p)\n"
        "    return scope\n"
    ),
    "the reserved factory": "KnowledgeScope.organisation(p)\n",
    "principal as a type": (
        "from typing import Annotated, Optional\n"
        "from app.identity.principal import Principal\n"
        "Current = Annotated[Principal, Depends(get)]\n"
        "def f(p: Optional[Principal]) -> 'Principal':\n"
        "    return isinstance(p, Principal)\n"
    ),
    "an unrelated str.replace": "name = title.replace('a', 'b')\n",
    "an exception's super().__init__": (
        "class Refused(Exception):\n"
        "    def __init__(self, reason):\n"
        "        super().__init__(reason)\n"
    ),
}


@pytest.mark.parametrize("source", list(ALLOWED.values()), ids=list(ALLOWED))
def test_the_intended_uses_are_allowed(source) -> None:
    assert offences(source, "app/services/elsewhere.py") == []


# ============================================================================
# Import direction (alongside test_identity_structure's existing rules)
# ============================================================================


def _imported(source: str) -> List[str]:
    modules = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_the_scope_columns_module_imports_nothing_from_app() -> None:
    """The database-layer half must stay importable by every model package,
    including execution, which may never import identity."""
    source = (APP / "database" / "models" / "scope.py").read_text(encoding="utf-8")
    modules = _imported(source)
    assert modules and not [m for m in modules if m == "app" or m.startswith("app.")]


def test_no_model_takes_the_trusted_scope_value() -> None:
    """Models get the columns from the database layer, never `app.identity.scope`."""
    checked = 0
    for path, source in app_sources():
        if not (path.endswith("/models.py") or path.startswith("app/database/")):
            continue
        checked += 1
        assert not [m for m in _imported(source) if m.startswith("app.identity.scope")], path
    assert checked > 10
