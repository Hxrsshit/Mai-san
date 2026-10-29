"""Stage 7B L0: the knowledge-scope foundation is present and inert.

Pins what L0 promises:
- exactly the ten audited tables carry the scope columns, all nullable and
  with **no default** (nothing is attributed by accident);
- one CHECK per table, identical in the models and in migration 0021;
- the global unique indexes that remain are untouched; since migration 0022
  the memory, entity and alias ones are per scope instead (two partial
  indexes each: one per scope, one among unattributed legacy rows);
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
#: Global unique indexes that must stay exactly as they were (units 8-9 own
#: these tables).
GLOBAL_UNIQUES = {
    "imported_archives": ("uq_imported_archives_source_sha256", ["source_sha256"]),
    "executions": ("uq_executions_idempotency_key", ["idempotency_key"]),
}
#: Migration 0022: uniqueness per scope. (table, stem, key columns).
SCOPED_UNIQUES = {
    "memories": ("type_normalized_content", ["memory_type", "normalized_content"]),
    "entities": ("normalized_name", ["normalized_name"]),
    "entity_aliases": ("normalized_alias", ["normalized_alias"]),
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


@pytest.mark.parametrize("table_name", sorted(SCOPED_UNIQUES))
def test_knowledge_uniqueness_is_per_scope(table_name) -> None:
    """No global unique index on the key; one partial unique index per scope
    and one among unattributed rows, exactly as migration 0022 creates them."""
    stem, columns = SCOPED_UNIQUES[table_name]
    indexes = {i.name: i for i in Base.metadata.tables[table_name].indexes}
    assert not any(i.unique and [c.name for c in i.columns] == columns
                   and i.dialect_options["postgresql"]["where"] is None
                   for i in indexes.values()), "a global unique index survived"
    scoped = indexes[f"uq_{table_name}_scoped_{stem}"]
    legacy = indexes[f"uq_{table_name}_legacy_{stem}"]
    assert scoped.unique and [c.name for c in scoped.columns] == ["scope_id", *columns]
    assert legacy.unique and [c.name for c in legacy.columns] == columns
    for index, where in ((scoped, "scope_id IS NOT NULL"), (legacy, "scope_id IS NULL")):
        for dialect in ("sqlite", "postgresql"):
            assert str(index.dialect_options[dialect]["where"]) == where


def test_the_migration_creates_the_model_indexes() -> None:
    path = BACKEND / "alembic" / "versions" / "0022_scoped_uniqueness.py"
    spec = importlib.util.spec_from_file_location("migration_0022", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {t: (stem, list(cols)) for t, _old, stem, cols in module.INDEXES} == SCOPED_UNIQUES


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
    APP / "memory" / "service.py",                  # unit 5: memories
    APP / "entities" / "service.py",                # unit 5: entities, aliases, the scope's User
    APP / "relationships" / "service.py",           # unit 5: relationships
    APP / "knowledge" / "lifecycle.py",             # unit 5: conflict links
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


# --- Every knowledge query is scope-bound, statement by statement (units 5-6) --

#: Scoped knowledge models, and the child tables read only through them.
KNOWLEDGE_MODELS = {"Memory", "Entity", "EntityAlias", "Relationship", "KnowledgeConflict"}
KNOWLEDGE_CHILDREN = {"MemoryEntity", "RelationshipEvidence"}
#: Reviewed child-table statements (memory links, evidence) that need no
#: clause of their own, keyed by (path relative to app/, function). The
#: exemption covers only statements naming child tables alone; any statement
#: in the same function that names a scoped model still needs its clause.
REVIEWED_KNOWLEDGE_STATEMENTS = {
    # A subquery of memory ids for one entity, used only inside a query that
    # is itself filtered by `scope.clause(Memory)`.
    ("knowledge/conflicts.py", "_retire_memories_mentioning"),
    # The evidence pair is checked only after `scope.owns` has verified both
    # the relationship and the memory.
    ("relationships/service.py", "_add_evidence"),
}

_SIMPLE_STATEMENTS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Return)
_QUERY_CALLS = ("select", "delete", "update")


def _statement_expressions(function) -> list:
    """Every expression a query could sit in: whole simple statements, and
    the header of each compound statement (`for x in <expr>`, `if <expr>`,
    `while <expr>`, `with <expr>`) -- not its body, whose statements are
    visited on their own (review M2)."""
    found = []
    for node in ast.walk(function):
        if isinstance(node, _SIMPLE_STATEMENTS):
            found.append(node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            found.append(node.iter)
        elif isinstance(node, (ast.If, ast.While)):
            found.append(node.test)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            found.extend(item.context_expr for item in node.items)
    return found


def _call_name(call: ast.Call):
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)


def _unbound_knowledge_statements(source: str, relative: str) -> list:
    """Query statements that name a knowledge model without binding *that*
    model to the scope.

    A statement is a query when it calls `select`, `delete` or `update`.
    Every knowledge model it names anywhere -- the select list,
    `select_from`, `join`, `where`, a subquery -- needs its own
    `clause(<Model>)` in the same statement: a clause on a joined entity
    does not filter the memories (review M2). A statement naming only child
    tables (memory links, evidence) is allowed in a reviewed function alone;
    beside a scoped model that is bound, a child table is fine."""
    problems = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reviewed = (relative, function.name) in REVIEWED_KNOWLEDGE_STATEMENTS
        for expression in _statement_expressions(function):
            calls = [n for n in ast.walk(expression) if isinstance(n, ast.Call)]
            if not any(_call_name(c) in _QUERY_CALLS for c in calls):
                continue
            named = {n.id for n in ast.walk(expression)
                     if isinstance(n, ast.Name) and n.id in KNOWLEDGE_MODELS | KNOWLEDGE_CHILDREN}
            if not named:
                continue
            bound = {c.args[0].id for c in calls
                     if _call_name(c) == "clause" and c.args and isinstance(c.args[0], ast.Name)}
            scoped = named & KNOWLEDGE_MODELS
            if scoped:
                if scoped <= bound:
                    continue
            elif reviewed:
                continue
            problems.append((relative, function.name, expression.lineno, sorted(named - bound)))
    return problems


def test_every_knowledge_query_is_scope_bound() -> None:
    """A select, update or delete of memories, entities, aliases,
    relationships or conflict links -- or of memory links and evidence, which
    are read through them -- must carry `scope.clause(...)` in the same
    statement, or be a reviewed one. A new unscoped `select(Memory)` anywhere
    in the application fails here."""
    problems = []
    for path in sorted(APP.rglob("*.py")):
        relative = str(path.relative_to(APP))
        problems += _unbound_knowledge_statements(path.read_text(encoding="utf-8"), relative)
    assert problems == []


def test_the_reviewed_knowledge_statements_still_exist() -> None:
    for relative, name in REVIEWED_KNOWLEDGE_STATEMENTS:
        tree = ast.parse((APP / relative).read_text(encoding="utf-8"))
        assert any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
                   for n in ast.walk(tree)), (relative, name)


@pytest.mark.parametrize("source, flagged", [
    ("async def f(s): return await s.execute(select(Memory))", True),
    ("async def f(s, i): await s.execute(delete(Entity).where(Entity.id == i))", True),
    ("async def f(s): await s.execute(update(Relationship).values(status=1))", True),
    ("async def f(s, i): return select(MemoryEntity.memory_id).where(MemoryEntity.entity_id == i)", True),
    ("async def f(s, sc):\n    q = select(Memory)\n    q = q.where(sc.clause(Memory))", True),  # split: flagged
    ("async def f(s, sc): return await s.execute(select(Memory).where(sc.clause(Memory)))", False),
    ("async def f(self, s): return select(Entity).join(EntityAlias).where(self._scope.clause(Entity))", True),
    ("async def f(self, s): return select(Entity).join(EntityAlias)"
     ".where(self._scope.clause(Entity), self._scope.clause(EntityAlias))", False),
    ("async def f(s): return await s.execute(select(Conversation))", False),  # the F2 checker's job
    # Review M2: shapes the first checker missed.
    ("async def f(s): return await s.execute(select(func.count()).select_from(Memory))", True),
    ("async def f(s, sc): return select(func.count()).select_from(Memory).where(sc.clause(Memory))", False),
    ("async def f(s, sc):\n    for m in (await s.execute(select(Memory))).scalars():\n        pass", True),
    ("async def f(s, sc):\n    if (await s.execute(select(Entity.id))).first():\n        pass", True),
    ("async def f(s, sc):\n    while (await s.execute(select(Relationship))).first():\n        pass", True),
    ("async def f(s, sc, i):\n    async with s.begin():\n        await s.execute(delete(Memory).where(Memory.id == i))", True),
    # A clause on the joined entity does not filter the memories.
    ("async def f(s, sc): return select(Memory).join(MemoryEntity).join(Entity).where(sc.clause(Entity))", True),
    ("async def f(s, sc): return select(Memory).join(MemoryEntity).join(Entity)"
     ".where(sc.clause(Entity), sc.clause(Memory))", False),
    ("async def f(s, sc): return select(MemoryEntity.memory_id).join(Memory).where(sc.clause(Memory))", False),
])
def test_the_knowledge_query_checker(source, flagged) -> None:
    assert bool(_unbound_knowledge_statements(source, "x.py")) is flagged


@pytest.mark.parametrize("source, flagged", [
    # A reviewed function's child-only subquery passes ...
    ("def r(i): return select(MemoryEntity.memory_id).where(MemoryEntity.entity_id == i)", False),
    # ... but a scoped model in the same function still needs its clause.
    ("def r(i):\n    q = select(MemoryEntity.memory_id)\n    return select(Memory).where(Memory.id.in_(q))", True),
])
def test_a_reviewed_function_is_exempt_only_for_child_tables(source, flagged) -> None:
    globals()["REVIEWED_KNOWLEDGE_STATEMENTS"].add(("x.py", "r"))
    try:
        assert bool(_unbound_knowledge_statements(source, "x.py")) is flagged
    finally:
        globals()["REVIEWED_KNOWLEDGE_STATEMENTS"].discard(("x.py", "r"))


def _identity_map_reads(source: str) -> list:
    """`get`/`get_one` calls whose first argument names a knowledge model,
    bare (`Memory`) or qualified (`models.Memory`) -- review M2."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "get_one") and node.args):
            first = node.args[0]
            name = first.id if isinstance(first, ast.Name) else getattr(first, "attr", None)
            if name in KNOWLEDGE_MODELS:
                found.append(name)
    return found


def test_no_identity_map_read_of_a_knowledge_row_anywhere() -> None:
    """`session.get(Memory|Entity|..., ...)` would bypass the scope filter.
    Child links are composite-key reads after `scope.owns` and are allowed.
    Not caught: a model held in a variable (`session.get(model, i)`)."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for name in _identity_map_reads(path.read_text(encoding="utf-8")):
            offenders.append((str(path.relative_to(BACKEND)), name))
    assert offenders == []


@pytest.mark.parametrize("source, flagged", [
    ("async def f(s, i): return await s.get(Memory, i)", True),
    ("async def f(s, i): return await s.get(models.Entity, i)", True),
    ("async def f(s, i): return await s.get_one(Relationship, i)", True),
    ("async def f(s, i, j): return await s.get(MemoryEntity, (i, j))", False),
    ("def f(d): return d.get(\"Memory\")", False),
])
def test_the_identity_map_checker(source, flagged) -> None:
    assert bool(_identity_map_reads(source)) is flagged
