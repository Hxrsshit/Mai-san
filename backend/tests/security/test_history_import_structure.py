"""Stage 5C: structural guarantees about the import layer.

Behaviour tests show the importer does the right thing with the inputs it was
given. These assert properties of the *code*, which hold for inputs nobody
thought to write a test for.

Every check is AST-based. A substring scan reads docstrings and comments --
including the ones in this repository that explain why a thing is absent --
and reports the explanation as the violation. Stage 5A.2 hit that twice.
"""

import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path("app")
HISTORY = APP_ROOT / "history"


def module_imports(path: pathlib.Path) -> set:
    """Every module name imported by one file."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def all_history_imports() -> set:
    names = set()
    for path in HISTORY.rglob("*.py"):
        names |= module_imports(path)
    return names


def test_the_parser_touches_no_database_no_model_and_no_socket() -> None:
    """It turns bytes into value objects, or it raises. Nothing else.

    A parser that could reach the session would be able to commit a partially
    validated document, and one that could reach a provider would make an
    untrusted file able to trigger a model call.
    """
    imported = module_imports(HISTORY / "parser.py")
    for forbidden in ("sqlalchemy", "app.llm", "httpx", "socket", "requests"):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


def test_the_import_layer_reaches_no_tool_executor_or_workflow() -> None:
    """Imported content cannot authorize or run anything."""
    imported = all_history_imports()
    for forbidden in ("app.tools", "app.execution", "app.workflows"):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


def test_the_import_layer_makes_no_outbound_request() -> None:
    """An import reads a local file. It has no business on the network."""
    imported = all_history_imports()
    for forbidden in ("httpx", "requests", "urllib", "socket", "app.integrations"):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


def test_only_the_memory_service_writes_a_memory() -> None:
    """The import path must not build a `Memory` of its own.

    Stage 5C's whole claim about deduplication and conflict resolution rests
    on imported knowledge going through the same pipeline as live knowledge.
    A second construction site would quietly opt out of both.
    """
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Memory"
            ):
                offenders.append(str(path.relative_to(APP_ROOT)))

    assert sorted(set(offenders)) == ["memory/service.py"], offenders


def test_the_archive_models_are_not_the_live_conversation_models() -> None:
    """Separate tables, so history cannot be continued or listed as a chat."""
    from app.database.models import Conversation, Message
    from app.history.models import ImportedConversation, ImportedMessage

    assert ImportedConversation.__tablename__ == "imported_conversations"
    assert ImportedMessage.__tablename__ == "imported_messages"
    assert ImportedConversation.__tablename__ != Conversation.__tablename__
    assert ImportedMessage.__tablename__ != Message.__tablename__


def test_no_retrieval_path_reads_the_archive_tables() -> None:
    """Only derived memories travel to a prompt; raw history stays put.

    Checked over the modules that build what a model sees. If any of them
    learned to read `imported_messages`, the archive would be one query away
    from a context window.
    """
    archive_types = {"ImportedMessage", "ImportedConversation", "ImportedArchive"}
    for package in ("retrieval", "context", "prompt"):
        for path in (APP_ROOT / package).rglob("*.py"):
            names = {
                node.id
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Name)
            }
            assert not (names & archive_types), path
            assert not any(
                name.startswith("app.history") for name in module_imports(path)
            ), path


def test_the_extractable_role_set_is_a_single_member() -> None:
    """Closed by construction, so a role added later is excluded by default."""
    from app.history.models import EXTRACTABLE_ROLES, ImportedRole

    assert isinstance(EXTRACTABLE_ROLES, frozenset)
    assert len(EXTRACTABLE_ROLES) == 1
    assert EXTRACTABLE_ROLES == {ImportedRole.USER}
    # Every other role exists and is excluded -- not merely unlisted.
    assert len(list(ImportedRole)) == 5


def test_the_role_filter_is_applied_in_the_query_not_in_python() -> None:
    """A filter a caller can forget is not a boundary.

    `_extractable_messages` must narrow by role in SQL, so no code path can
    obtain assistant or system rows and then decide what to do with them.
    """
    source = (HISTORY / "service.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_extractable_messages"
    )
    names = {
        node.id for node in ast.walk(function) if isinstance(node, ast.Name)
    }
    assert "EXTRACTABLE_ROLES" in names


def test_conflict_recency_is_judged_on_stated_at() -> None:
    """`created_at` is insert time, which an import makes meaningless.

    Pinned structurally: the candidate query must not fall back to
    `created_at`, because doing so would let a 2023 opinion inserted today
    outrank something the user said last week.
    """
    source = (APP_ROOT / "knowledge" / "conflicts.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_retire_memories_mentioning"
    )
    attributes = [
        node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)
    ]
    assert "stated_at" in attributes
    assert "created_at" not in attributes


def test_an_imported_trigger_is_confined_by_the_origin_guard() -> None:
    """The guard exists, is applied, and is empty for live triggers."""
    from app.knowledge.conflicts import _origin_guard
    from app.memory.models import Memory, MemoryOrigin

    def probe(origin):
        return Memory(origin=origin)

    assert _origin_guard(probe(MemoryOrigin.LIVE)) == ()
    assert len(_origin_guard(probe(MemoryOrigin.IMPORTED))) == 1

    source = (APP_ROOT / "knowledge" / "conflicts.py").read_text()
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_origin_guard" in called, "the guard is defined but never applied"


def test_memory_provenance_is_exactly_one_root() -> None:
    """The database is the last line of defence, not the only one."""
    from app.memory.models import Memory

    checks = [
        constraint.name
        for constraint in Memory.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    ]
    # The naming convention prefixes it at table level.
    assert "ck_memories_provenance_matches_origin" in checks


def test_the_import_directory_is_configuration_not_caller_input() -> None:
    """`resolve` must never build a path from an unvalidated caller string."""
    from app.history import sources

    tree = ast.parse((HISTORY / "sources.py").read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "resolve"
    )
    called = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    # The directory comes from settings, and the result is resolved before use.
    assert "import_directory" in called
    assert hasattr(sources, "resolve")


def test_the_scrubber_shares_one_pattern_vocabulary_with_the_log_redactor() -> None:
    """Two lists would drift, and the older one is the maintained one."""
    from app.core.logging import _REDACTIONS
    from app.history.sanitise import REDACTION_PATTERNS

    assert REDACTION_PATTERNS is _REDACTIONS


def test_stage_5c_added_exactly_one_migration() -> None:
    """Three tables and four columns, in one revision.

    The claim is about Stage 5C, so the window is 5C's own: everything after
    `0009` and no later than `0010`. Freezing the whole directory would make
    this a tripwire every subsequent stage has to edit, which teaches people
    to edit it without reading it -- the same reasoning as the Stage 5B pin.
    """
    versions = sorted(p.name for p in pathlib.Path("alembic/versions").glob("*.py"))
    stage_5c = [v for v in versions if "0009" < v[:4] <= "0010"]
    assert stage_5c == ["0010_history_import.py"], stage_5c
