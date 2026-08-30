# Mai — Stage 2C Acceptance Report

**Date:** 2026-08-30
**Scope:** Relationship System only. No Stage 2D work performed.
**Baseline:** Stage 2B, 406 tests passing.

---

## Stage 2C Status

### PASS

The relationship pipeline was verified by execution, including the
specification's own acceptance scenario against the real Groq API. **522 tests
pass, 0 failed, 0 skipped.** Stage 1 (105), Stage 2A (160) and Stage 2B (143)
all still pass in full.

**Environmental limitation, unchanged from earlier stages and not a defect:**
Docker, the Next.js frontend, and PostgreSQL at runtime could not be exercised
— that tooling is not installed on this machine (Python 3.9.6 and git only).
Runtime testing used SQLite; the PostgreSQL schema was verified by a reflected
schema comparison showing zero drift.

---

## Features Implemented

| Feature | Status |
| --- | --- |
| Relationship extraction from stored memories | **PASS** |
| Fifteen-type controlled vocabulary | **PASS** |
| Directional relationships with verified direction | **PASS** |
| Deterministic type normalization (synonym mapping) | **PASS** |
| Entity resolution reusing the Stage 2B resolver | **PASS** |
| Rejection of relationships naming unknown entities | **PASS** |
| Relationship deduplication, including under concurrency | **PASS** |
| `relationship_evidence` — many memories, one relationship | **PASS** |
| Transactional relationship + evidence | **PASS** |
| Alembic migration `0005`, plus a seeded `User` entity | **PASS** |
| Relationship inspection APIs (5 endpoints) | **PASS** |
| Failure isolation from chat, memory and entities | **PASS** |
| Configuration that actually changes behaviour | **PASS** |
| Basic contradiction awareness (status, no auto-resolution) | **PASS** |

---

## Database Changes

Two new tables, created by `0005_relationships.py` (revising `0004`; no earlier
migration modified).

**`relationships`** — `id`, `source_entity_id`, `relationship_type`,
`target_entity_id`, `confidence_score`, `status`, `created_at`, `updated_at`.

**`relationship_evidence`** — `id`, `relationship_id`, `memory_id`,
`created_at`.

| Constraint / index | Purpose |
| --- | --- |
| `(source, type, target, status)` **UNIQUE** | duplicate prevention, enforced under concurrency |
| `CHECK source_entity_id <> target_entity_id` | no self-relationships |
| `CHECK 0.0 <= confidence_score <= 1.0` | last defence on model output |
| `(relationship_id, memory_id)` **UNIQUE** | one memory supports a claim once |
| `source_entity_id` FK → `entities.id` **CASCADE** | |
| `target_entity_id` FK → `entities.id` **CASCADE** | |
| `relationship_id` FK → `relationships.id` **CASCADE** | no orphan evidence |
| `memory_id` FK → `memories.id` **CASCADE** | no orphan evidence |
| `ix_relationships_source_entity_id` / `_target_entity_id` / `_type` / `(status, created_at)` / `ix_relationship_evidence_memory_id` | lookup and filtering |

**Note on `source_memory_id`:** it is deliberately **not** a column on
`relationships`. The specification prefers an evidence table, and a single
column would force a duplicate relationship row every time another memory
supported the same claim.

**Migration verification — all executed:**

| Check | Result |
| --- | --- |
| Clean database → head | **PASS** — 5 migrations |
| Existing **Stage 2B database with data** → `0005` | **PASS** — conversation, message, memory, entity and link all preserved |
| Downgrade `0005 → 0004` | **PASS** — tables dropped, seeded `User` removed, Stage 2B data intact |
| Earlier migrations modified? | **No** — `0001`–`0004` unchanged |
| Schema drift | **NO DRIFT** — reflected comparison of migration-built vs model-built schema |

---

## Relationship Extraction Verification

**Live, against the real Groq API — the specification's acceptance scenario:**

> "I am building Mai as a personal AI environment. Mai currently uses
> PostgreSQL for local storage and Groq for fast inference. I am interested in
> AI product development."

| Expected | Extracted | Confidence |
| --- | --- | --- |
| `User BUILDS Mai` | **User ──BUILDS──► Mai** | 0.96 |
| `Mai USES PostgreSQL` | **Mai ──USES──► PostgreSQL** | 0.96 |
| `Mai USES Groq` | **Mai ──USES──► Groq** | 0.96 |
| `User INTERESTED_IN AI Product Development` | **User ──INTERESTED_IN──► AI** | 0.95 |

All four, with correct meaning and direction. The fourth targets the entity
`AI` rather than `AI Product Development` because that is the entity Stage 2B
created for that memory — an entity-extraction outcome, not a relationship
error. The specification allows for label variation provided meaning and
direction are correct.

**Automated coverage** (mocked provider) for `USES`, `BUILDS`,
`INTERESTED_IN`, `WORKS_WITH` and `DEPENDS_ON`, plus the "nothing to relate"
case.

---

## Relationship Direction Verification

Direction was tested three ways:

1. **Preservation** — `Mai USES PostgreSQL` stores `source=Mai, target=PostgreSQL`, asserted explicitly not to be the reverse.
2. **Distinctness** — the two directions of the same pair are stored as two separate relationships, not deduplicated into one.
3. **API separation** — `/api/entities/{id}/relationships` returns `outgoing` and `incoming` as distinct lists. Verified live for `Mai`:

```
OUTGOING  Mai  --USES-->    Groq
OUTGOING  Mai  --USES-->    PostgreSQL
INCOMING  User --BUILDS-->  Mai
```

The prompt's worked examples specifically address the trap that a memory's
grammatical subject is often not the relationship's source
("User decided to use PostgreSQL … in Mai" → **Mai** USES PostgreSQL).

---

## Relationship Normalization

Deterministic table-driven mapping into the controlled vocabulary — no model
involved, so the same phrasing always resolves the same way.

| Raw | Normalized |
| --- | --- |
| `UTILIZES`, `runs on`, `powered by`, `built with`, `leverages` | `USES` |
| `is building`, `develops` | `BUILDS` |
| `collaborating with`, `partners with` | `WORKS_WITH` |
| `wants to`, `aims to` | `HAS_GOAL` |
| `likes`, `favours` | `PREFERS` |
| `lives in`, `based in` | `LOCATED_IN` |
| `exploring`, `curious about` | `INTERESTED_IN` |

**Unmappable labels are rejected, not guessed at** — `FROBNICATES`,
`relates_somehow`, empty and non-string values all produce no candidate.

---

## Deduplication

| Scenario | Expected | Result |
| --- | --- | --- |
| Identical triple extracted twice | one relationship | **PASS** |
| `UTILIZES` after `USES` (the spec's example) | normalized, no duplicate | **PASS** |
| Same pair, different type | two relationships | **PASS** |
| Reversed direction | two relationships (distinct claims) | **PASS** |
| Repeats within one batch | collapsed, most confident kept | **PASS** |
| Same memory re-extracted three times | one relationship, one evidence row | **PASS** |
| **Concurrent turns proposing the same triple** | one relationship | **PASS** — 8 consecutive runs |
| Concurrent turns accumulating evidence | no duplicate evidence rows | **PASS** |

---

## Evidence System

**Verified live** — the specification's exact example:

```
Memory 1: "Mai uses PostgreSQL for local storage."
Memory 2: "User selected PostgreSQL as Mai's database."
                    │
                    ▼
        Mai ──USES──► PostgreSQL     evidence_count = 2
```

The relationship count stayed at 4; only the evidence grew. The server log
confirms the mechanism:

```
Relationship reused | relationship_type=USES evidence_added=True
```

`/api/relationships/{id}/evidence` returned both supporting memories with their
types and timestamps.

Automated tests additionally confirm every relationship has at least one
evidence row, and that re-extraction never duplicates evidence.

---

## Failure Isolation

| Induced failure | Result |
| --- | --- |
| `LLMTimeoutError` during relationship extraction | chat **201**, memory + entities intact, 0 relationships |
| `LLMRateLimitError` / `LLMAuthError` / `LLMResponseError` | same |
| `RuntimeError` / `ValueError` (unexpected) | same — caught, never escapes |
| Malformed output (empty, non-JSON, truncated, wrong types) | nothing stored, upstream intact |
| Invalid type / confidence / self-reference | candidate rejected |
| Names an entity that does not exist | rejected; **no entity created** |
| **Database failure during relationship storage** | memory + entities survive, 0 relationships, 0 evidence |
| **Database failure at five different flush points** | never a relationship without evidence, never evidence without a relationship |
| Entity extraction failed upstream | fewer than two entities → extraction skipped |
| Memory extraction failed upstream | nothing runs |
| Chat turn failed | none of the three stages run |
| Chat response with relationships on vs off | identical |

**Orphan check on the live database** after a real delete: 0 evidence pointing
at a missing relationship, 0 at a missing memory, 0 relationships referencing a
missing entity, 0 self-relationships.

---

## APIs

| Endpoint | Verified live |
| --- | --- |
| `GET /api/relationships` | 200 |
| `GET ?relationship_type=USES` | 200 |
| `GET ?source_entity_id=` / `?target_entity_id=` / `?status=` | 200 |
| `GET ?relationship_type=FROBNICATES` (invalid) | **422** |
| `GET ?status=deleted` (invalid) | **422** |
| `GET ?limit=0`, `?source_entity_id=not-a-uuid` | **422** |
| `GET ?limit=2&offset=1` | 200 |
| `GET /api/relationships/not-a-uuid` | **422** `validation_error` |
| `GET /api/relationships/{unknown}` | **404** `relationship_not_found` |
| `GET /api/relationships/{id}` | 200 — with `evidence_count` |
| `GET /api/relationships/{id}/evidence` | 200 — supporting memories |
| `GET /api/relationships/{unknown}/evidence` | **404** |
| `DELETE /api/relationships/{id}` | **204** — entities and memories kept |
| `DELETE /api/relationships/{unknown}` | **404** |
| `GET /api/entities/{id}/relationships` | 200 — `outgoing` / `incoming` separated |

Error envelopes match the Stage 1 format exactly.

---

## Tests

| Metric | Count |
| --- | --- |
| **Total** | **522** |
| **Passed** | **522** |
| **Failed** | **0** |
| **Skipped** | **0** |

| Stage | Tests |
| --- | --- |
| Stage 1 | 105 |
| Stage 2A | 160 |
| Stage 2B | 143 |
| **Stage 2C** | **114** |

| Stage 2C file | Tests |
| --- | --- |
| `test_relationship_extraction.py` | 48 |
| `test_relationship_isolation.py` | 26 |
| `test_relationship_pipeline.py` | 26 |
| `test_relationship_api.py` | 14 |

Groq is mocked throughout; the suite needs no API key and no network. Stability:
repeated full runs and 8 consecutive concurrency-suite runs, all green.

---

## Bugs Found

**C1 — The seeded `User` entity was silently not created.** *(high)*
Migration `0005` inserted the singleton without supplying `created_at` /
`updated_at`, so the column `server_default` fired — and that default is
`now()`, which PostgreSQL provides but **SQLite does not**. The insert failed
with `unknown function: now()`, leaving no `User` entity and therefore no
possible `User BUILDS Mai` or `User INTERESTED_IN …` relationships. Earlier
stages never hit this because the ORM always supplies timestamps in Python;
this was the first raw insert in a migration.

**C2 — Relationship recall was poor on the acceptance scenario.** *(medium)*
The first live run produced only **1 of the 4** expected relationships, plus a
spurious `Groq PART_OF Mai` and a vague `Mai RELATED_TO AI`. The clearest claim
of all (`Mai USES PostgreSQL`) yielded **zero** candidates. Root cause was
prompt quality, not logic: memories are phrased *"User decided to use
PostgreSQL for local storage in Mai"*, where the grammatical subject (User) is
not the relationship's source, and the model declined rather than guess —
correct conservatism, poor recall.

**C3 — A concurrent-insert path could lose its evidence.** *(medium, caught
before it shipped)* The first implementation raised a custom exception on
`IntegrityError` that no caller handled, so a relationship created concurrently
by another task would have had the current memory's evidence dropped.

**C4 — A Stage 2B test assumed a single entity.** *(test-only)* Adding a second
entity to the shared concurrency fixture broke an alias test that asserted
`len(entities) == 1` rather than targeting the entity it cared about.

**C5 — A database-failure test's threshold was too high.** *(test-only)* It
failed after flush 8, but the whole pipeline needs only 7, so nothing was
actually interrupted.

---

## Fixes Applied

| # | Fix |
| --- | --- |
| C1 | The seed insert supplies explicit timestamps and binds the UUID as hex, so it works on both SQLite and PostgreSQL. Verified: clean install, upgrade onto Stage 2B data, and downgrade all seed/remove correctly. |
| C2 | The prompt gained a **worked-examples** section teaching the exact phrasing Stage 2A produces, plus explicit guidance that `RELATED_TO` signals a relationship not worth recording and `PART_OF` is not the inverse of `USES`. Re-running the scenario produced **all four** expected relationships at 0.95–0.96 confidence. |
| C3 | Restructured to catch `IntegrityError`, exit the poisoned savepoint, re-read the winning row and attach evidence to it — so a race costs nothing. |
| C4 | The test now selects the PostgreSQL entity by normalized name instead of assuming it is the only one. |
| C5 | Replaced with a **parametrized invariant test** across five failure points asserting *never a relationship without evidence, never evidence without a relationship* — a stronger check than any single threshold. |

---

## Known Limitations

Confirmed **not** implemented, by design and by scan:

- **No graph database, no Neo4j** — relationships are PostgreSQL rows
- **No multi-hop or graph reasoning** — no traversal beyond one hop
- **No vector embeddings, vector database, or semantic search**
- **No RAG**
- **No web enrichment**
- **No contradiction resolution** — `User PREFERS Groq` and `User PREFERS Claude` coexist; nothing is superseded automatically
- **No temporal relationship reasoning** — no validity periods

Behavioural limitations worth knowing before Stage 2D:

- **Confidence is never revised.** Set once at creation; further evidence adds rows but does not raise it.
- **Extraction quality depends on the model and the prompt.** Direction errors and over-general `RELATED_TO` labels were both observed and reduced by prompt work, not eliminated. Recall is deliberately traded for precision.
- **Relationships inherit Stage 2B's entity granularity.** `AI` versus `AI Product Development` is decided upstream.
- **One extra model call per memory with two or more entities**, on top of the chat, memory and entity calls. The ≥2-entity gate keeps this off most memories.
- **The `User` entity is a single global singleton** — Mai is a single-user system, and nothing distinguishes users.
- **PostgreSQL runtime remains unverified** (no server available); the schema is verified, the behaviour is not.

---

## Stage 2D Readiness

### Is Mai ready to begin Stage 2D — Context Retrieval & Memory Assembly? **YES**

Reasoning:

- **There is now structured knowledge to retrieve, not just text.** Stage 2D assembles context; it needs something better than raw memories to assemble from. Mai can now answer *what matters* (memories), *what exists* (entities) and *how things connect* (relationships), all queryable by type, direction and entity.
- **Every claim is traceable.** Memories carry conversation and message provenance; relationships carry evidence rows pointing back at memories. A retrieval stage that surfaces a fact can show why it is believed — the data supports *"where did you learn this?"* without further schema work.
- **The join patterns Stage 2D needs already exist and are proven.** `/api/entities/{id}/relationships` and `/api/relationships/{id}/evidence` are exactly the traversals a context assembler performs, and both are indexed and tested.
- **Integrity is enforced by the database at every layer.** Memories, entities and relationships all carry UNIQUE constraints; live orphan checks return zero across four tables. Retrieval built on duplicated or orphaned knowledge would silently return contradictory context.
- **Failure isolation now spans four stages**, verified through the real HTTP path at every one. Stage 2D reads rather than writes, which is strictly safer than what is already proven.
- **522 tests, zero failures, zero skips**, with Stages 1, 2A and 2B intact.

**Three things to carry into Stage 2D:**

1. **Retrieval is a read path — keep it out of the background task.** The three write stages must stay sequential in one task; context assembly belongs on the request path, before the model call, where its latency is visible and controllable.
2. **Budget the context, and rank before truncating.** There are now four sources (messages, memories, entities, relationships) competing for one context window. Importance, confidence and recency all already exist as columns — use them rather than adding new scoring.
3. **Watch the per-turn model-call count.** A turn already costs one chat call, one memory call, one entity call per memory, and one relationship call per multi-entity memory. Stage 2D should add none — assembly is a database operation.

---

## Appendix — Reproduction

```bash
# Full suite (no API key or network needed)
cd backend && source .venv/bin/activate && pytest

# Stage 2C suites specifically
pytest tests/test_relationship_*.py

# Concurrency regressions across all stages
pytest tests/test_memory_concurrency.py

# Migrations: clean, onto Stage 2B data, and reversible
alembic upgrade head && alembic current
alembic downgrade 0004

# Live end-to-end (needs GROQ_API_KEY)
uvicorn app.main:app --reload
curl -X POST localhost:8000/api/conversations -H 'Content-Type: application/json' -d '{}'
curl -X POST localhost:8000/api/conversations/<id>/messages \
  -H 'Content-Type: application/json' \
  -d '{"content":"I am building Mai as a personal AI environment. Mai currently uses PostgreSQL for local storage and Groq for fast inference. I am interested in AI product development."}'
curl localhost:8000/api/relationships
curl localhost:8000/api/relationships/<id>/evidence
curl localhost:8000/api/entities/<id>/relationships
```
