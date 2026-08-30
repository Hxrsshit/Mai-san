"""Relationship inspection endpoints, driven through real chat turns."""

import json
import uuid

from httpx import AsyncClient


def memory_payload(*memories) -> str:
    return json.dumps({"should_store_memory": bool(memories), "memories": list(memories)})


def memory(content):
    return {"content": content, "memory_type": "decision",
            "importance_score": 8, "confidence_score": 0.95}


def entity_payload(*entities) -> str:
    return json.dumps({"entities": list(entities)})


def entity(name, kind="technology"):
    return {"name": name, "entity_type": kind, "confidence_score": 0.95}


def relationship_payload(*relationships) -> str:
    return json.dumps({"relationships": list(relationships)})


def relationship(source, kind, target, confidence=0.93):
    return {"source_entity": source, "relationship_type": kind,
            "target_entity": target, "confidence_score": confidence}


async def seed(client, conversation_id, provider, memories, entities, relationships,
               text="Something worth remembering."):
    provider.extraction_reply = memory_payload(*memories)
    provider.entity_reply = entity_payload(*entities)
    provider.relationship_reply = relationship_payload(*relationships)
    response = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"content": text}
    )
    assert response.status_code == 201


DEFAULT = dict(
    memories=[memory("Mai uses PostgreSQL for storage.")],
    entities=[entity("Mai", "project"), entity("PostgreSQL", "technology")],
    relationships=[relationship("Mai", "USES", "PostgreSQL")],
)


# --- Listing ----------------------------------------------------------------


async def test_list_is_empty_initially(client: AsyncClient) -> None:
    response = await client.get("/api/relationships")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_relationship_appears_after_a_turn(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(client, conversation_id, fake_provider, **DEFAULT)

    body = (await client.get("/api/relationships")).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["source_entity"]["canonical_name"] == "Mai"
    assert item["relationship_type"] == "USES"
    assert item["target_entity"]["canonical_name"] == "PostgreSQL"
    assert item["status"] == "active"
    assert item["evidence_count"] == 1
    assert item["source_entity"]["entity_type"] == "project"


async def test_filter_by_type_and_endpoints(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        memories=[memory("Mai uses PostgreSQL and depends on Groq.")],
        entities=[entity("Mai", "project"), entity("PostgreSQL"), entity("Groq", "company")],
        relationships=[relationship("Mai", "USES", "PostgreSQL"),
                       relationship("Mai", "DEPENDS_ON", "Groq")],
    )

    uses = (await client.get("/api/relationships?relationship_type=USES")).json()
    assert uses["total"] == 1
    assert uses["items"][0]["target_entity"]["canonical_name"] == "PostgreSQL"

    mai_id = uses["items"][0]["source_entity"]["id"]
    outgoing = (await client.get(f"/api/relationships?source_entity_id={mai_id}")).json()
    assert outgoing["total"] == 2
    incoming = (await client.get(f"/api/relationships?target_entity_id={mai_id}")).json()
    assert incoming["total"] == 0


async def test_invalid_filters_are_rejected(client: AsyncClient) -> None:
    assert (await client.get("/api/relationships?relationship_type=FROBNICATES")).status_code == 422
    assert (await client.get("/api/relationships?status=deleted")).status_code == 422
    assert (await client.get("/api/relationships?limit=0")).status_code == 422
    assert (await client.get("/api/relationships?source_entity_id=not-a-uuid")).status_code == 422


async def test_pagination(client: AsyncClient, conversation_id, fake_provider) -> None:
    await seed(
        client, conversation_id, fake_provider,
        memories=[memory("Mai uses several things.")],
        entities=[entity("Mai", "project"), entity("PostgreSQL"), entity("Groq", "company"),
                  entity("Python")],
        relationships=[relationship("Mai", "USES", "PostgreSQL"),
                       relationship("Mai", "USES", "Groq"),
                       relationship("Mai", "USES", "Python")],
    )
    page = (await client.get("/api/relationships?limit=2&offset=0")).json()
    assert len(page["items"]) == 2
    assert page["total"] == 3


# --- Detail and evidence ----------------------------------------------------


async def test_get_one_relationship(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(client, conversation_id, fake_provider, **DEFAULT)
    rid = (await client.get("/api/relationships")).json()["items"][0]["id"]

    body = (await client.get(f"/api/relationships/{rid}")).json()
    assert body["id"] == rid
    assert body["relationship_type"] == "USES"
    assert body["evidence_count"] == 1


async def test_evidence_endpoint_returns_supporting_memories(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(client, conversation_id, fake_provider, **DEFAULT)
    rid = (await client.get("/api/relationships")).json()["items"][0]["id"]

    evidence = (await client.get(f"/api/relationships/{rid}/evidence")).json()

    assert len(evidence) == 1
    assert evidence[0]["content"] == "Mai uses PostgreSQL for storage."
    assert evidence[0]["memory_type"] == "decision"
    assert evidence[0]["linked_at"]


async def test_unknown_relationship_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/relationships/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "relationship_not_found"
    assert (await client.get(f"/api/relationships/{uuid.uuid4()}/evidence")).status_code == 404


async def test_malformed_relationship_id_returns_422(client: AsyncClient) -> None:
    response = await client.get("/api/relationships/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- Entity relationships ---------------------------------------------------


async def test_entity_relationships_distinguish_direction(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(
        client, conversation_id, fake_provider,
        memories=[memory("User builds Mai which uses PostgreSQL.")],
        entities=[entity("User", "person"), entity("Mai", "project"), entity("PostgreSQL")],
        relationships=[relationship("User", "BUILDS", "Mai"),
                       relationship("Mai", "USES", "PostgreSQL")],
    )
    entities = (await client.get("/api/entities")).json()["items"]
    mai_id = next(e["id"] for e in entities if e["canonical_name"] == "Mai")

    body = (await client.get(f"/api/entities/{mai_id}/relationships")).json()

    assert body["entity"]["canonical_name"] == "Mai"
    assert [r["relationship_type"] for r in body["outgoing"]] == ["USES"]
    assert [r["relationship_type"] for r in body["incoming"]] == ["BUILDS"]
    assert body["incoming"][0]["source_entity"]["canonical_name"] == "User"
    assert body["total"] == 2


async def test_entity_relationships_for_unknown_entity_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.get(f"/api/entities/{uuid.uuid4()}/relationships")
    assert response.status_code == 404


# --- Deletion ---------------------------------------------------------------


async def test_delete_relationship_keeps_entities_and_memories(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(client, conversation_id, fake_provider, **DEFAULT)
    rid = (await client.get("/api/relationships")).json()["items"][0]["id"]
    entities_before = (await client.get("/api/entities")).json()["total"]
    memories_before = (await client.get("/api/memories")).json()["total"]

    assert (await client.delete(f"/api/relationships/{rid}")).status_code == 204

    assert (await client.get(f"/api/relationships/{rid}")).status_code == 404
    assert (await client.get("/api/relationships")).json()["total"] == 0
    assert (await client.get("/api/entities")).json()["total"] == entities_before
    assert (await client.get("/api/memories")).json()["total"] == memories_before


async def test_delete_unknown_relationship_returns_404(client: AsyncClient) -> None:
    response = await client.delete(f"/api/relationships/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "relationship_not_found"


async def test_deleting_an_entity_removes_its_relationships(
    client: AsyncClient, conversation_id, fake_provider
) -> None:
    await seed(client, conversation_id, fake_provider, **DEFAULT)
    entities = (await client.get("/api/entities")).json()["items"]
    pg_id = next(e["id"] for e in entities if e["canonical_name"] == "PostgreSQL")

    await client.delete(f"/api/entities/{pg_id}")

    assert (await client.get("/api/relationships")).json()["total"] == 0
    # Memories are untouched.
    assert (await client.get("/api/memories")).json()["total"] == 1
