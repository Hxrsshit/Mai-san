"""Stage 7B L0: the knowledge-scope foundation is present and inert.

Pins what L0 promises:
- exactly the ten audited tables carry the scope columns, all nullable and
  with **no default** (nothing is attributed by accident);
- one CHECK per table, identical in the models and in migration 0021;
- the existing global unique indexes are untouched (scoped uniqueness needs
  NOT NULL scopes first);
- personal is the only writable visibility;
- only the approved services write a scope, and only through
  `KnowledgeScope.write_columns()`; nothing names the scope columns directly
  (unit 4 onwards: reads go through `KnowledgeScope.clause()`);
- the CHECK refuses incomplete or inconsistent scopes.
"""

import ast
import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, insert
from sqlalchemy.exc import IntegrityError

from app.database.metadata import Base
from app.database.models.scope import (
    KNOWLEDGE_SCOPE_CHECK_SQL,
    KNOWLEDGE_SCOPE_VISIBILITIES,
)
from app.identity.constants import DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID
from app.identity.scope import WRITABLE_VISIBILITIES, ScopeVisibility

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
SCOPE_COLUMNS = ("organisation_id", "scope_id", "scope_visibility")
SCOPED_TABLES = {
    "conversations", "memories", "entities", "entity_aliases", "relationships",
    "knowledge_conflicts", "reminders", "executions", "workflows", "imported_archives",
}
#: Global unique indexes that L0 must leave exactly as they were.
GLOBAL_UNIQUES = {
    "memories": ("uq_memories_type_normalized_content", ["memory_type", "normalized_content"]),
    "entities": ("uq_entities_normalized_name", ["normalized_name"]),
    "entity_aliases": ("uq_entity_aliases_normalized_alias", ["normalized_alias"]),
    "imported_archives": ("uq_imported_archives_source_sha256", ["source_sha256"]),
    "executions": ("uq_executions_idempotency_key", ["idempotency_key"]),
}


def _migration_0021():
    path = BACKEND / "alembic" / "versions" / "0021_knowledge_scope.py"
    spec = importlib.util.spec_from_file_location("migration_0021", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exactly_the_audited_tables_are_scoped() -> None:
    scoped = {t.name for t in Base.metadata.sorted_tables if "scope_id" in t.c}
    assert scoped == SCOPED_TABLES
    assert set(_migration_0021().SCOPED_TABLES) == SCOPED_TABLES
    for table in Base.metadata.sorted_tables:
        present = [c for c in SCOPE_COLUMNS if c in table.c]
        assert present in ([], list(SCOPE_COLUMNS)) or table.name in {
            "organisation_members"}, table.name


@pytest.mark.parametrize("table_name", sorted(SCOPED_TABLES))
def test_scope_columns_are_nullable_with_no_default(table_name) -> None:
    table = Base.metadata.tables[table_name]
    for name in SCOPE_COLUMNS:
        column = table.c[name]
        assert column.nullable, (table_name, name)
        assert column.default is None, (table_name, name)
        assert column.server_default is None, (table_name, name)
        assert not column.foreign_keys, (table_name, name)   # enforcement is a later migration
    checks = [c for c in table.constraints if isinstance(c, CheckConstraint)
              and c.name == f"ck_{table_name}_knowledge_scope_valid"]
    assert len(checks) == 1 and str(checks[0].sqltext) == KNOWLEDGE_SCOPE_CHECK_SQL
    assert [i.name for i in table.indexes if [c.name for c in i.columns] == ["scope_id"]] == [
        f"ix_{table_name}_scope_id"]


def test_the_migration_check_is_the_model_check() -> None:
    assert _migration_0021().CHECK_SQL == KNOWLEDGE_SCOPE_CHECK_SQL


@pytest.mark.parametrize("table_name", sorted(GLOBAL_UNIQUES))
def test_the_global_unique_indexes_are_unchanged(table_name) -> None:
    name, columns = GLOBAL_UNIQUES[table_name]
    [index] = [i for i in Base.metadata.tables[table_name].indexes if i.name == name]
    assert index.unique and [c.name for c in index.columns] == columns


def test_personal_is_the_only_writable_visibility() -> None:
    assert tuple(v.value for v in ScopeVisibility) == KNOWLEDGE_SCOPE_VISIBILITIES
    assert WRITABLE_VISIBILITIES == frozenset({ScopeVisibility.PERSONAL})


#: Names whose appearance outside the two defining modules is a scope write
#: (or a step towards one). `organisation_id` is not here: Stage 7A's identity
#: tables use it legitimately.
SCOPE_WRITE_NAMES = ("scope_id", "scope_visibility", "write_columns")
_SCOPE_WORD = re.compile(r"\b(scope_id|scope_visibility|write_columns)\b")


def _scope_write_offenders(source: str) -> list:
    """Every way the source names a scope column or `write_columns`.

    Keywords (`scope_id=`) and attributes (`row.scope_id`), and -- review L1 --
    string constants anywhere, including f-string parts and raw SQL: a
    `values(**{"scope_id": ...})`, `setattr(row, "scope_id", ...)`,
    `table.c["scope_visibility"]`, `text("UPDATE ... SET scope_id ...")` or
    `getattr(scope, "write_columns")` is still a scope write. Not caught, and
    out of reach of any static check: a name assembled at run time
    (`"scope" + "_id"`).
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg in SCOPE_WRITE_NAMES:
            found.append(node.arg)
        elif isinstance(node, ast.Attribute) and node.attr in SCOPE_WRITE_NAMES:
            found.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(_SCOPE_WORD.findall(node.value))
    return found


#: The services that write a scope (Stage 7B), each through
#: `write_columns()` only. Widening this list is a reviewed change.
SCOPE_WRITERS = {
    APP / "services" / "conversation_service.py",   # unit 4: conversations
}


def test_only_the_approved_services_write_a_scope() -> None:
    """Naming scope_id or scope_visibility anywhere but the two defining
    modules is forbidden outright: reads use `KnowledgeScope.clause()`,
    writes `write_columns()`. `write_columns` itself may appear only in the
    approved writers (see `_scope_write_offenders`)."""
    defining = {APP / "identity" / "scope.py", APP / "database" / "models" / "scope.py"}
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path in defining:
            continue
        for name in _scope_write_offenders(path.read_text(encoding="utf-8")):
            if name == "write_columns" and path in SCOPE_WRITERS:
                continue
            offenders.append((str(path.relative_to(BACKEND)), name))
    assert offenders == []


def test_every_scope_writer_still_writes_one() -> None:
    """The allowlist is not stale: each listed writer really calls
    `write_columns()`."""
    for path in SCOPE_WRITERS:
        assert "write_columns" in _scope_write_offenders(path.read_text(encoding="utf-8")), path


def test_the_conversation_service_never_uses_the_identity_map() -> None:
    """`session.get(Conversation, ...)` would return an already-loaded row
    without the scope filter; the service reads with a filtered SELECT."""
    source = (APP / "services" / "conversation_service.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"):
            names = [a.id for a in node.args if isinstance(a, ast.Name)]
            assert "Conversation" not in names and "Message" not in names


@pytest.mark.parametrize("bypass", [
    "Memory(scope_id=s)",                                         # keyword
    "row.scope_visibility = v",                                   # attribute
    "scope.write_columns()",
    "stmt.values(**{'scope_id': s})",                             # review L1: string keys
    "row = {'scope_visibility': 'personal'}",
    "setattr(row, 'scope_id', s)",
    "column = table.c['scope_id']",
    "getattr(scope, 'write_columns')()",
    "text('UPDATE memories SET scope_id = :s WHERE id = :i')",    # raw SQL
    "text(f'UPDATE {t} SET scope_visibility = :v')",              # f-string part
])
def test_the_scope_write_tripwire_catches(bypass) -> None:
    assert _scope_write_offenders(bypass), bypass


@pytest.mark.parametrize("innocent", [
    "Member(organisation_id=o)",          # Stage 7A identity: not a scope column
    "x = 'scope_identifier'",              # whole words only
    "note = 'the knowledge scope'",
    "def scope_ids(): pass",
])
def test_the_scope_write_tripwire_ignores(innocent) -> None:
    assert _scope_write_offenders(innocent) == [], innocent


NIL_SAFE_CASES = [  # (organisation, scope, visibility, accepted)
    (None, None, None, True),
    (DEFAULT_ORGANISATION_ID, None, None, True),
    (DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID, "personal", True),
    (DEFAULT_ORGANISATION_ID, DEFAULT_ORGANISATION_ID, "organisation", True),
    (DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID, None, False),
    (None, None, "personal", False),
    (None, LEGACY_ADMIN_MEMBER_ID, "personal", False),
    (DEFAULT_ORGANISATION_ID, DEFAULT_ORGANISATION_ID, "personal", False),
    (DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID, "organisation", False),
    (DEFAULT_ORGANISATION_ID, LEGACY_ADMIN_MEMBER_ID, "shared", False),
]


@pytest.mark.parametrize("organisation_id, scope_id, visibility, accepted", NIL_SAFE_CASES)
async def test_the_check_refuses_incomplete_or_inconsistent_scopes(
    session_factory, organisation_id, scope_id, visibility, accepted
) -> None:
    conversations = Base.metadata.tables["conversations"]
    row = {"id": uuid.uuid4(), "title": "scope check", "organisation_id": organisation_id,
           "scope_id": scope_id, "scope_visibility": visibility}
    async with session_factory() as session:
        if accepted:
            await session.execute(insert(conversations).values(**row))
            await session.commit()
        else:
            with pytest.raises(IntegrityError):
                await session.execute(insert(conversations).values(**row))
            await session.rollback()


# --- Every conversation or message query is scope-bound (review F2) -----------

#: Models whose queries must be scope-bound in application code.
SCOPED_QUERY_MODELS = {"Conversation", "Message"}
#: Calls that bind a function's queries to a scope.
SCOPE_BINDERS = {"clause", "_in_scope", "_require_conversation"}
#: Reviewed child queries keyed by a conversation already verified in scope
#: earlier in the same turn. Each entry is (path relative to app/, function).
REVIEWED_CHILD_QUERIES = {
    # The proposed message must belong to the turn's conversation, which
    # `_turn_in_scope` has verified before anything is extracted.
    ("memory/service.py", "_resolve_source_message"),
}


def _query_models(call: ast.Call) -> set:
    """Scoped model names used as arguments of a select/delete/update call."""
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name not in ("select", "delete", "update"):
        return set()
    found = set()
    for arg in call.args:
        for node in ast.walk(arg):
            if isinstance(node, ast.Name) and node.id in SCOPED_QUERY_MODELS:
                found.add(node.id)
    return found


def _unbound_scoped_queries(source: str, relative: str) -> list:
    problems = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
        models = set().union(*(_query_models(c) for c in calls)) if calls else set()
        if not models:
            continue
        binders = {getattr(c.func, "attr", None) for c in calls}
        if binders & SCOPE_BINDERS or (relative, function.name) in REVIEWED_CHILD_QUERIES:
            continue
        problems.append((relative, function.name, sorted(models)))
    return problems


def test_every_conversation_and_message_query_is_scope_bound() -> None:
    """A function that selects, updates or deletes conversations or messages
    must bind the query to a scope -- `scope.clause(...)`, the service's
    `_in_scope()`, or a prior `_require_conversation()` -- or be a reviewed
    child query. A new unscoped `select(Conversation)` anywhere fails here."""
    problems = []
    for path in sorted(APP.rglob("*.py")):
        relative = str(path.relative_to(APP))
        problems += _unbound_scoped_queries(path.read_text(encoding="utf-8"), relative)
    assert problems == []


def test_the_reviewed_child_queries_still_exist() -> None:
    for relative, name in REVIEWED_CHILD_QUERIES:
        tree = ast.parse((APP / relative).read_text(encoding="utf-8"))
        assert any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
                   for n in ast.walk(tree)), (relative, name)


@pytest.mark.parametrize("source, flagged", [
    ("async def f(s): return await s.execute(select(Conversation))", True),
    ("async def f(s, i): return await s.execute(select(Message).where(Message.conversation_id == i))", True),
    ("async def f(s): await s.execute(delete(Conversation))", True),
    ("async def f(self, s): return await s.execute(select(Conversation).where(self._in_scope()))", False),
    ("async def f(s, sc): return await s.execute(select(Message.id).join(Conversation).where(sc.clause(Conversation)))", False),
    ("async def f(s): return await s.execute(select(Memory))", False),   # units 5-6
])
def test_the_scoped_query_checker(source, flagged) -> None:
    assert bool(_unbound_scoped_queries(source, "x.py")) is flagged


def test_no_identity_map_read_of_a_conversation_or_message_anywhere() -> None:
    """`session.get(Conversation|Message, ...)` would bypass the scope filter."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in SCOPED_QUERY_MODELS):
                offenders.append((str(path.relative_to(BACKEND)), node.args[0].id))
    assert offenders == []
