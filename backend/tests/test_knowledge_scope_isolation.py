"""Stage 7B units 5-6: knowledge is bounded by a knowledge scope.

Memories, entities, aliases, relationships, evidence and conflict links each
belong to the scope that wrote them. Another member of the same organisation,
a member of another organisation, and unattributed legacy rows are invisible
to a scope -- for listing, reading, deleting, deduplication, entity
resolution, relationship extraction, conflict detection and retrieval -- and
one scope's statement never changes another's knowledge. All rows are
synthetic.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, insert, select

from app.core.errors import MemoryNotFoundError
from app.entities.models import Entity, EntityAlias, EntityStatus, EntityType, MemoryEntity
from app.entities.service import EntityNotFoundError, EntityService
from app.identity.scope import InvalidScope
from app.knowledge.service import KnowledgeService
from app.memory.models import Memory, MemoryStatus, MemoryType
from app.memory.service import MemoryService
from app.memory.tasks import run_entity_extraction
from app.relationships.models import (
    Relationship,
    RelationshipEvidence,
    RelationshipStatus,
    RelationshipType,
)
from app.relationships.service import RelationshipNotFoundError, RelationshipService
from app.retrieval.retrievers import evidence_map
from app.retrieval.service import RetrievalService
from app.services.conversation_service import ConversationService
from tests.scopes import ADMIN_SCOPE, COLLEAGUE_SCOPE, OUTSIDER_SCOPE

pytestmark = pytest.mark.anyio

OTHERS = [pytest.param(COLLEAGUE_SCOPE, id="colleague"), pytest.param(OUTSIDER_SCOPE, id="outsider")]
SECRET = "Mai uses PostgreSQL for storage"


async def _seed(session, scope, content=SECRET):
    """One scope's knowledge: a conversation, a memory, two entities (one
    with an alias), a relationship with the memory as its evidence."""
    conversation = await ConversationService(session, scope).create_conversation()
    memory = Memory(
        content=content + ".", normalized_content=content.lower(),
        memory_type=MemoryType.DECISION, status=MemoryStatus.ACTIVE,
        importance_score=8, confidence_score=0.95,
        source_conversation_id=conversation.id, **scope.write_columns(),
    )
    mai = Entity(canonical_name="Mai", normalized_name="mai", entity_type=EntityType.PROJECT,
                 status=EntityStatus.ACTIVE, **scope.write_columns())
    pg = Entity(canonical_name="PostgreSQL", normalized_name="postgresql",
                entity_type=EntityType.TECHNOLOGY, status=EntityStatus.ACTIVE,
                **scope.write_columns())
    session.add_all([memory, mai, pg])
    await session.flush()
    session.add(EntityAlias(entity_id=pg.id, alias="Postgres", normalized_alias="postgres",
                            **scope.write_columns()))
    session.add_all([MemoryEntity(memory_id=memory.id, entity_id=mai.id),
                     MemoryEntity(memory_id=memory.id, entity_id=pg.id)])
    relationship = Relationship(
        source_entity_id=mai.id, relationship_type=RelationshipType.USES,
        target_entity_id=pg.id, confidence_score=0.9, status=RelationshipStatus.ACTIVE,
        **scope.write_columns(),
    )
    session.add(relationship)
    await session.flush()
    session.add(RelationshipEvidence(relationship_id=relationship.id, memory_id=memory.id))
    await session.commit()
    return {"conversation": conversation.id, "memory": memory.id, "mai": mai.id,
            "pg": pg.id, "relationship": relationship.id}


def _services(session, scope, provider=None, settings=None):
    return (MemoryService(session=session, provider=provider, settings=settings, scope=scope),
            EntityService(session=session, provider=provider, settings=settings, scope=scope),
            RelationshipService(session=session, provider=provider, settings=settings, scope=scope))


# --- Construction ------------------------------------------------------------


@pytest.mark.parametrize("build", [
    lambda s: MemoryService(session=object(), scope=s),
    lambda s: EntityService(session=object(), scope=s),
    lambda s: RelationshipService(session=object(), scope=s),
    lambda s: KnowledgeService(object(), scope=s),
    lambda s: RetrievalService(object(), scope=s),
])
def test_every_knowledge_service_refuses_anything_but_a_scope(build) -> None:
    for bogus in (None, "personal", ADMIN_SCOPE.write_columns(), uuid.uuid4()):
        with pytest.raises(InvalidScope):
            build(bogus)


# --- Reads, enumeration and deletes ------------------------------------------


@pytest.mark.parametrize("other", OTHERS)
async def test_another_scope_cannot_read_list_or_delete_the_knowledge(session_factory, other) -> None:
    async with session_factory() as session:
        ids = await _seed(session, ADMIN_SCOPE)
    async with session_factory() as session:
        memories, entities, relationships = _services(session, other)
        with pytest.raises(MemoryNotFoundError):
            await memories.get_memory(ids["memory"])
        with pytest.raises(MemoryNotFoundError):
            await memories.delete_memory(ids["memory"])
        assert await memories.list_memories() == [] and await memories.count_memories() == 0
        with pytest.raises(EntityNotFoundError):
            await entities.get_entity(ids["pg"])
        with pytest.raises(EntityNotFoundError):
            await entities.delete_entity(ids["pg"])
        assert await entities.list_entities() == [] and await entities.count_entities() == 0
        assert await entities.get_linked_memories(ids["pg"]) == []
        assert await entities.count_linked_memories(ids["pg"]) == 0
        with pytest.raises(RelationshipNotFoundError):
            await relationships.get_relationship(ids["relationship"])
        with pytest.raises(RelationshipNotFoundError):
            await relationships.delete_relationship(ids["relationship"])
        assert await relationships.list_relationships() == []
        assert await relationships.count_relationships() == 0
        assert await relationships.get_evidence_memories(ids["relationship"]) == []
        assert await relationships.count_evidence(ids["relationship"]) == 0
        with pytest.raises(MemoryNotFoundError):
            await KnowledgeService(session, scope=other).lifecycle_for_memory(ids["memory"])
        await session.commit()
    async with session_factory() as session:        # nothing was deleted
        for model, key in ((Memory, "memory"), (Entity, "pg"), (Relationship, "relationship")):
            assert (await session.execute(select(model.id).where(model.id == ids[key]))).first()


async def test_rows_already_in_the_identity_map_are_still_refused(db_session) -> None:
    ids = await _seed(db_session, ADMIN_SCOPE)
    for model, key in ((Memory, "memory"), (Entity, "pg"), (Relationship, "relationship")):
        assert await db_session.get(model, ids[key]) is not None    # loaded
    memories, entities, relationships = _services(db_session, COLLEAGUE_SCOPE)
    with pytest.raises(MemoryNotFoundError):
        await memories.get_memory(ids["memory"])
    with pytest.raises(EntityNotFoundError):
        await entities.get_entity(ids["pg"])
    with pytest.raises(RelationshipNotFoundError):
        await relationships.get_relationship(ids["relationship"])


async def test_each_scope_sees_only_its_own_knowledge(session_factory) -> None:
    async with session_factory() as session:
        admin = await _seed(session, ADMIN_SCOPE)
        colleague = await _seed(session, COLLEAGUE_SCOPE)
    async with session_factory() as session:
        for scope, own in ((ADMIN_SCOPE, admin), (COLLEAGUE_SCOPE, colleague)):
            memories, entities, relationships = _services(session, scope)
            assert [m.id for m in await memories.list_memories()] == [own["memory"]]
            assert {e.id for e in await entities.list_entities()} == {own["mai"], own["pg"]}
            assert [r.id for r in await relationships.list_relationships()] == [own["relationship"]]


async def test_legacy_unattributed_knowledge_is_quarantined(db_session) -> None:
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    db_session.add(Memory(content="Legacy fact.", normalized_content="legacy fact",
                          memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                          importance_score=8, confidence_score=0.9,
                          source_conversation_id=conversation.id))
    db_session.add(Entity(canonical_name="Legacy", normalized_name="legacy",
                          entity_type=EntityType.TECHNOLOGY, status=EntityStatus.ACTIVE))
    await db_session.flush()
    memories, entities, relationships = _services(db_session, ADMIN_SCOPE)
    assert await memories.count_memories() == 0
    assert await entities.count_entities() == 0
    package = await RetrievalService(db_session, scope=ADMIN_SCOPE).retrieve("legacy fact")
    assert package.memories == [] and package.matched_entities == []


# --- Deduplication and uniqueness are per scope -------------------------------


async def test_the_same_fact_is_stored_once_per_scope_not_once_globally(
    db_session, fake_provider, settings
) -> None:
    """Before unit 5, a colleague's identical fact was a "duplicate" of the
    Admin's and was silently dropped."""
    reply = json.dumps({"should_store_memory": True, "memories": [{
        "content": "User prefers jasmine tea.", "memory_type": "preference",
        "importance_score": 8, "confidence_score": 0.95}]})
    for scope in (ADMIN_SCOPE, COLLEAGUE_SCOPE, ADMIN_SCOPE):
        conversation = await ConversationService(db_session, scope).create_conversation()
        fake_provider.extraction_reply = reply
        await MemoryService(session=db_session, provider=fake_provider, settings=settings,
                            scope=scope).extract_and_store(conversation.id, "I like jasmine tea", "ok")
    rows = (await db_session.execute(select(Memory.scope_id))).scalars().all()
    assert sorted(map(str, rows)) == sorted(map(str, [ADMIN_SCOPE.scope_id, COLLEAGUE_SCOPE.scope_id]))


async def test_entity_resolution_never_reuses_another_scopes_entity(
    db_session, fake_provider, settings
) -> None:
    ids = await _seed(db_session, ADMIN_SCOPE)
    conversation = await ConversationService(db_session, COLLEAGUE_SCOPE).create_conversation()
    memory = Memory(content="User uses Postgres.", normalized_content="user uses postgres",
                    memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                    importance_score=8, confidence_score=0.9,
                    source_conversation_id=conversation.id, **COLLEAGUE_SCOPE.write_columns())
    db_session.add(memory)
    await db_session.flush()
    fake_provider.entity_reply = json.dumps({"entities": [
        {"name": "Postgres", "entity_type": "technology", "confidence_score": 0.95,
         "aliases": ["PostgreSQL"]}]})
    [linked] = await EntityService(session=db_session, provider=fake_provider, settings=settings,
                                   scope=COLLEAGUE_SCOPE).extract_for_memory(memory)
    assert linked.id not in (ids["pg"], ids["mai"])
    assert COLLEAGUE_SCOPE.owns(linked)
    # The admin's alias "postgres" did not block the colleague's own name,
    # and the colleague's alias did not attach to the admin's entity.
    admin_aliases = (await db_session.execute(
        select(EntityAlias.normalized_alias).where(EntityAlias.entity_id == ids["pg"]))).scalars().all()
    assert admin_aliases == ["postgres"]


# --- A memory handed in from another scope is refused --------------------------


@pytest.mark.parametrize("other", OTHERS)
async def test_extraction_refuses_another_scopes_memory(db_session, fake_provider, settings, other) -> None:
    ids = await _seed(db_session, ADMIN_SCOPE)
    memory = (await db_session.execute(select(Memory).where(Memory.id == ids["memory"]))).scalar_one()
    fake_provider.entity_reply = json.dumps({"entities": [
        {"name": "Redis", "entity_type": "technology", "confidence_score": 0.95}]})
    _, entities, relationships = _services(db_session, other, fake_provider, settings)
    assert await entities.extract_for_memory(memory) == []
    assert await relationships.extract_for_memory(memory) == []
    report = await KnowledgeService(db_session, settings, scope=other).evaluate_memory(memory)
    assert report.links_created == 0
    assert fake_provider.entity_calls == [] and fake_provider.relationship_calls == []


async def test_a_background_step_never_loads_another_scopes_memory(
    session_factory, fake_provider, settings
) -> None:
    async with session_factory() as session:
        ids = await _seed(session, ADMIN_SCOPE)
    fake_provider.entity_reply = json.dumps({"entities": [
        {"name": "Redis", "entity_type": "technology", "confidence_score": 0.95}]})
    await run_entity_extraction([ids["memory"]], settings=settings, provider=fake_provider,
                                session_factory=session_factory, scope=COLLEAGUE_SCOPE)
    assert fake_provider.entity_calls == []
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Entity)
                                      .where(Entity.normalized_name == "redis"))).scalar_one() == 0


async def test_a_mismatched_turn_scope_stores_no_memory(db_session, fake_provider, settings) -> None:
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    service = MemoryService(session=db_session, provider=fake_provider, settings=settings,
                            scope=ADMIN_SCOPE)
    assert await service.extract_and_store(conversation.id, "x", "y", scope=COLLEAGUE_SCOPE) == []
    assert fake_provider.extraction_calls == []


# --- Conflicts never cross a scope ---------------------------------------------


async def test_one_members_replacement_never_supersedes_anothers_knowledge(
    db_session, settings
) -> None:
    """The Admin "switched from PostgreSQL to SQLite"; the colleague's
    PostgreSQL memory and relationship must stay ACTIVE."""
    colleague = await _seed(db_session, COLLEAGUE_SCOPE)
    await _seed(db_session, ADMIN_SCOPE)
    conversation = await ConversationService(db_session, ADMIN_SCOPE).create_conversation()
    trigger = Memory(content="Mai switched from PostgreSQL to SQLite.",
                     normalized_content="mai switched from postgresql to sqlite",
                     memory_type=MemoryType.DECISION, status=MemoryStatus.ACTIVE,
                     importance_score=9, confidence_score=0.95,
                     source_conversation_id=conversation.id, **ADMIN_SCOPE.write_columns())
    db_session.add(trigger)
    await db_session.flush()
    report = await KnowledgeService(db_session, settings, scope=ADMIN_SCOPE).evaluate_memory(trigger)
    assert report.memories_superseded >= 1          # the Admin's own did move
    status = (await db_session.execute(
        select(Memory.status).where(Memory.id == colleague["memory"]))).scalar_one()
    assert status is MemoryStatus.ACTIVE
    rel_status = (await db_session.execute(
        select(Relationship.status).where(Relationship.id == colleague["relationship"]))).scalar_one()
    assert rel_status is RelationshipStatus.ACTIVE
    # Not even a conflict link may name the colleague's knowledge.
    from app.knowledge.models import KnowledgeConflict
    links = (await db_session.execute(select(KnowledgeConflict).where(
        (KnowledgeConflict.older_memory_id == colleague["memory"])
        | (KnowledgeConflict.newer_memory_id == colleague["memory"])
        | (KnowledgeConflict.older_relationship_id == colleague["relationship"])
        | (KnowledgeConflict.newer_relationship_id == colleague["relationship"])))).scalars().all()
    assert links == []


# --- Retrieval ------------------------------------------------------------------


@pytest.mark.parametrize("other", OTHERS)
async def test_retrieval_never_returns_another_scopes_knowledge(session_factory, settings, other) -> None:
    async with session_factory() as session:
        ids = await _seed(session, ADMIN_SCOPE)
    async with session_factory() as session:
        package = await RetrievalService(session, settings, scope=other).retrieve(
            "What does Mai use? Postgres?")
        assert package.memories == [] and package.matched_entities == []
        assert package.relationships == []
        mapping, memory_ids = await evidence_map(session, [ids["relationship"]], other)
        assert mapping == {} and memory_ids == set()
        own = await RetrievalService(session, settings, scope=ADMIN_SCOPE).retrieve(
            "What does Mai use? Postgres?")
        assert [m.id for m in own.memories] == [ids["memory"]]


# --- The API, as the authenticated Admin --------------------------------------


async def test_the_api_never_reaches_a_colleagues_knowledge(client: AsyncClient, session_factory) -> None:
    async with session_factory() as session:
        theirs = await _seed(session, COLLEAGUE_SCOPE)
    for path in (f"/api/memories/{theirs['memory']}",
                 f"/api/entities/{theirs['pg']}",
                 f"/api/entities/{theirs['pg']}/memories",
                 f"/api/entities/{theirs['pg']}/relationships",
                 f"/api/relationships/{theirs['relationship']}",
                 f"/api/relationships/{theirs['relationship']}/evidence",
                 f"/api/knowledge/debug/{theirs['memory']}"):
        assert (await client.get(path)).status_code == 404, path
    for path in (f"/api/memories/{theirs['memory']}", f"/api/entities/{theirs['pg']}",
                 f"/api/relationships/{theirs['relationship']}"):
        assert (await client.delete(path)).status_code == 404, path
    for path in ("/api/memories", "/api/entities", "/api/relationships"):
        assert (await client.get(path)).json()["total"] == 0, path
    debug = await client.post("/api/retrieval/debug", json={"query": "What does Mai use? Postgres?"})
    assert debug.status_code == 200
    assert SECRET.lower() not in json.dumps(debug.json()).lower()
    assert "postgresql" not in json.dumps(debug.json()).lower()
    async with session_factory() as session:        # nothing was deleted
        assert (await session.execute(select(Memory.id).where(Memory.id == theirs["memory"]))).first()


async def test_a_chat_turn_never_sees_a_colleagues_knowledge(
    client: AsyncClient, session_factory, fake_provider
) -> None:
    async with session_factory() as session:
        await _seed(session, COLLEAGUE_SCOPE)
    conversation = (await client.post("/api/conversations", json={})).json()["id"]
    fake_provider.reply = "ok"
    response = await client.post(f"/api/conversations/{conversation}/messages",
                                 json={"content": "What does Mai use for storage? Postgres?"})
    assert response.status_code == 201
    sent = " ".join(m.content for m in fake_provider.last_call).lower()
    assert "uses postgresql for storage" not in sent


# --- The scope's own User entity ------------------------------------------------


async def test_each_scope_gets_its_own_user_entity(db_session, fake_provider, settings) -> None:
    for scope in (ADMIN_SCOPE, COLLEAGUE_SCOPE):
        conversation = await ConversationService(db_session, scope).create_conversation()
        memory = Memory(content="User is building Mai.", normalized_content="user is building mai",
                        memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                        importance_score=8, confidence_score=0.9,
                        source_conversation_id=conversation.id, **scope.write_columns())
        db_session.add(memory)
        await db_session.flush()
        fake_provider.entity_reply = json.dumps({"entities": [
            {"name": "Mai", "entity_type": "project", "confidence_score": 0.95}]})
        await EntityService(session=db_session, provider=fake_provider, settings=settings,
                            scope=scope).extract_for_memory(memory)
    users = (await db_session.execute(
        select(Entity.scope_id).where(Entity.normalized_name == "user"))).scalars().all()
    assert sorted(map(str, users)) == sorted(map(str, [ADMIN_SCOPE.scope_id, COLLEAGUE_SCOPE.scope_id]))


async def test_the_legacy_user_entity_is_never_offered(db_session, fake_provider, settings) -> None:
    """The migration-seeded singleton is unattributed: a scope's relationship
    extraction must not see it."""
    await db_session.execute(insert(Entity).values(
        id=uuid.uuid4(), canonical_name="User", normalized_name="user",
        entity_type=EntityType.PERSON, status=EntityStatus.ACTIVE))
    ids = await _seed(db_session, ADMIN_SCOPE)
    memory = (await db_session.execute(select(Memory).where(Memory.id == ids["memory"]))).scalar_one()
    fake_provider.relationship_reply = json.dumps({"relationships": []})
    await RelationshipService(session=db_session, provider=fake_provider, settings=settings,
                              scope=ADMIN_SCOPE).extract_for_memory(memory)
    offered = fake_provider.last_relationship_call[1].content
    assert "Mai" in offered and "PostgreSQL" in offered
    assert "- User" not in offered and "User\n" not in offered


# --- One scope per turn (review L1) ---------------------------------------------


async def test_a_turn_refuses_a_context_service_of_another_scope(db_session, fake_provider, settings) -> None:
    from app.context.service import ContextService
    from app.services.chat_service import ChatService

    other_context = ContextService(db_session, settings, scope=COLLEAGUE_SCOPE)
    assert other_context.scope == COLLEAGUE_SCOPE
    with pytest.raises(InvalidScope):
        ChatService(session=db_session, provider=fake_provider, settings=settings,
                    scope=ADMIN_SCOPE, context_service=other_context)
    with pytest.raises(InvalidScope):        # history and retrieval must agree as well
        ContextService(db_session, settings, scope=ADMIN_SCOPE,
                       retrieval_service=RetrievalService(db_session, settings, scope=COLLEAGUE_SCOPE))


async def test_the_lifecycle_writer_refuses_a_decision_naming_another_scopes_rows(db_session) -> None:
    """Review L2: `apply` is public; a decision naming a foreign row writes
    no conflict link at all, even though the status UPDATE is scoped too."""
    from app.knowledge.lifecycle import LifecycleWriter
    from app.knowledge.models import ConflictReason, ConflictResolution, KnowledgeConflict
    from app.knowledge.schemas import ConflictOutcome

    mine = await _seed(db_session, ADMIN_SCOPE)
    theirs = await _seed(db_session, COLLEAGUE_SCOPE, "Mai uses Redis for caching")
    report = await LifecycleWriter(db_session, ADMIN_SCOPE).apply([
        ConflictOutcome(resolution=ConflictResolution.SUPERSEDED,
                        reason=list(ConflictReason)[0],
                        older_memory_id=theirs["memory"], newer_memory_id=mine["memory"]),
        ConflictOutcome(resolution=ConflictResolution.SUPERSEDED,
                        reason=list(ConflictReason)[0],
                        older_relationship_id=theirs["relationship"]),
    ], triggering_memory_id=mine["memory"])
    assert report.links_created == 0 and report.status_updates_failed == 2
    assert (await db_session.execute(select(func.count()).select_from(KnowledgeConflict))).scalar_one() == 0
    assert (await db_session.execute(select(Memory.status).where(Memory.id == theirs["memory"]))
            ).scalar_one() is MemoryStatus.ACTIVE


async def _fresh_memory(session, scope, content="User is building Mai."):
    conversation = await ConversationService(session, scope).create_conversation()
    memory = Memory(content=content, normalized_content=content.lower().rstrip("."),
                    memory_type=MemoryType.SEMANTIC, status=MemoryStatus.ACTIVE,
                    importance_score=8, confidence_score=0.9,
                    source_conversation_id=conversation.id, **scope.write_columns())
    session.add(memory)
    await session.flush()
    return memory


async def test_the_user_entity_cannot_be_captured_by_a_model_alias(db_session, fake_provider, settings) -> None:
    """Review L4: a model-proposed alias "User" on another entity is refused,
    and the scope still gets -- and keeps -- its own User entity."""
    memory = await _fresh_memory(db_session, ADMIN_SCOPE)
    fake_provider.entity_reply = json.dumps({"entities": [
        {"name": "Harshit", "entity_type": "person", "confidence_score": 0.95, "aliases": ["User"]}]})
    await EntityService(session=db_session, provider=fake_provider, settings=settings,
                        scope=ADMIN_SCOPE).extract_for_memory(memory)
    aliases = (await db_session.execute(select(EntityAlias.normalized_alias))).scalars().all()
    assert "user" not in aliases
    [user] = (await db_session.execute(select(Entity).where(
        Entity.normalized_name == "user", ADMIN_SCOPE.clause(Entity)))).scalars().all()
    assert user.canonical_name == "User" and user.entity_type is EntityType.PERSON


async def test_a_model_proposed_user_reuses_the_applications_user(db_session, fake_provider, settings) -> None:
    """The User is provisioned before candidates are stored, so a candidate
    named "User" resolves to it rather than defining it."""
    memory = await _fresh_memory(db_session, ADMIN_SCOPE)
    fake_provider.entity_reply = json.dumps({"entities": [
        {"name": "User", "entity_type": "concept", "description": "model text",
         "confidence_score": 0.95},
        {"name": "Mai", "entity_type": "project", "confidence_score": 0.95}]})
    await EntityService(session=db_session, provider=fake_provider, settings=settings,
                        scope=ADMIN_SCOPE).extract_for_memory(memory)
    [user] = (await db_session.execute(select(Entity).where(
        Entity.normalized_name == "user", ADMIN_SCOPE.clause(Entity)))).scalars().all()
    assert user.entity_type is EntityType.PERSON and user.description != "model text"


# --- History import is Admin-only until unit 9 (review M1) -----------------------


async def test_history_import_is_admin_only_until_archives_are_scoped(client: AsyncClient) -> None:
    """Archives, runs and the duplicate-file check are still global (unit 9),
    so a member could take the operator's export into their own scope or list
    every run. Until then the whole history API refuses a non-Admin."""
    from app.api.deps import get_principal
    from app.identity.constants import Role
    from app.identity.principal import Principal

    app = client._transport.app
    paths = [("GET", "/api/history/sources"), ("GET", "/api/history/imports"),
             ("GET", f"/api/history/imports/{uuid.uuid4()}"), ("POST", "/api/history/imports")]
    assert (await client.get("/api/history/imports")).status_code == 200      # the Admin
    member = Principal(member_id=COLLEAGUE_SCOPE.scope_id,
                       organisation_id=COLLEAGUE_SCOPE.organisation_id, role=Role.MEMBER)
    app.dependency_overrides[get_principal] = lambda: member
    try:
        for method, path in paths:
            response = await client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 403, (path, response.status_code)
    finally:
        del app.dependency_overrides[get_principal]


# --- Direct checks under the defence in depth (mutation M3, M5, M8) ------------


async def test_an_exact_name_resolves_only_within_the_scope(db_session) -> None:
    from app.entities.resolver import EntityResolver

    ids = await _seed(db_session, ADMIN_SCOPE)
    assert (await EntityResolver(db_session, ADMIN_SCOPE).resolve("PostgreSQL")).entity.id == ids["pg"]
    for other in (COLLEAGUE_SCOPE, OUTSIDER_SCOPE):
        resolver = EntityResolver(db_session, other)
        assert not (await resolver.resolve("PostgreSQL")).is_existing
        assert not (await resolver.resolve("Postgres")).is_existing          # the alias
        assert await resolver.alias_conflict("postgresql") is None


async def test_the_conflict_detector_names_only_rows_of_its_scope(db_session) -> None:
    """Below the writer's own check: detection itself never proposes another
    scope's memory or relationship."""
    from app.knowledge.conflicts import ConflictDetector

    colleague = await _seed(db_session, COLLEAGUE_SCOPE)
    mine = await _seed(db_session, ADMIN_SCOPE)
    trigger = await _fresh_memory(db_session, ADMIN_SCOPE, "Mai switched from PostgreSQL to SQLite.")
    outcomes = await ConflictDetector(db_session, ADMIN_SCOPE).detect(trigger)
    named = {i for o in outcomes for i in (o.older_memory_id, o.newer_memory_id,
                                           o.older_relationship_id, o.newer_relationship_id)}
    assert mine["memory"] in named                       # not vacuous
    assert not named & {colleague["memory"], colleague["relationship"]}


async def test_a_link_to_another_scopes_memory_is_never_followed(db_session) -> None:
    """A memory link with no scope of its own is read only through a memory
    and an entity both in the scope -- even if a foreign link exists."""
    mine = await _seed(db_session, ADMIN_SCOPE)
    theirs = await _seed(db_session, COLLEAGUE_SCOPE)
    db_session.add(MemoryEntity(memory_id=theirs["memory"], entity_id=mine["pg"]))  # cross-scope
    await db_session.flush()
    entities = EntityService(session=db_session, scope=ADMIN_SCOPE)
    assert [m.id for m in await entities.get_linked_memories(mine["pg"])] == [mine["memory"]]
    assert await entities.count_linked_memories(mine["pg"]) == 1
