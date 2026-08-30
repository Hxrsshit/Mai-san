# Mai — Stage 2B Acceptance Report

**Date:** 2026-08-30
**Scope:** Entity System only. No Stage 2C work performed.
**Baseline:** Stage 2A, 261 tests passing.

---

## Stage 2B Status

### PASS

The entity pipeline was verified by execution, including live extraction
against the real Groq API for the specification's own acceptance scenario.
**406 tests pass, 0 failed, 0 skipped.** Stage 1 (105) and Stage 2A (158) both
still pass in full.

**Environmental limitation, unchanged from earlier stages and not a defect:**
Docker, the Next.js frontend, and PostgreSQL at runtime could not be exercised
— that tooling is not installed on this machine (Python 3.9.6 and git only).
Runtime testing used SQLite; the PostgreSQL schema was verified by a reflected
schema comparison showing zero drift. Stage 2B adds no frontend code.

---

## Features Implemented

| Feature | Status |
| --- | --- |
| Entity extraction from stored memories | **PASS** |
| Ten-type controlled classification enum | **PASS** |
| Documented, consistent classification rules | **PASS** |
| Deterministic name normalization | **PASS** |
| Entity resolution (exact / normalized / alias / compact) | **PASS** |
| Entity deduplication, including under concurrency | **PASS** |
| Entity aliases with ambiguity refusal | **PASS** |
| Memory ↔ entity many-to-many linking | **PASS** |
| Alembic migration `0004` | **PASS** |
| Entity inspection APIs (4 endpoints) | **PASS** |
| Failure isolation from chat and memory | **PASS** |
| Per-candidate transaction integrity (savepoints) | **PASS** |
| Configuration that actually changes behaviour | **PASS** |
| Structured logging of every pipeline decision | **PASS** |

---

## Database Changes

Three new tables, created by `0004_entities.py` (revising `0003`; no earlier
migration modified).

**`entities`** — `id`, `canonical_name`, `normalized_name`, `entity_type`,
`status`, `description`, `created_at`, `updated_at`.

**`entity_aliases`** — `id`, `entity_id`, `alias`, `normalized_alias`,
`created_at`.

**`memory_entities`** — `memory_id`, `entity_id`, `mention_text`, `created_at`.

| Constraint / index | Purpose |
| --- | --- |
| `entities.normalized_name` **UNIQUE** | resolution + the concurrency guarantee |
| `entity_aliases.normalized_alias` **UNIQUE** | prevents an ambiguous alias |
| `memory_entities` composite PK `(memory_id, entity_id)` | link uniqueness |
| `entity_aliases.entity_id` FK → `entities.id` **CASCADE** | aliases die with the entity |
| `memory_entities.entity_id` FK → `entities.id` **CASCADE** | links die with the entity |
| `memory_entities.memory_id` FK → `memories.id` **CASCADE** | links die with the memory |
| `ix_entities_entity_type`, `ix_entities_(status, created_at)`, `ix_entity_aliases_entity_id`, `ix_memory_entities_entity_id` | filtering and lookup |

**Migration verification — all executed:**

| Check | Result |
| --- | --- |
| Clean database → head | **PASS** — 4 migrations, tables present |
| Existing **Stage 2A database with data** → `0004` | **PASS** — conversation, message and memory all preserved |
| Downgrade `0004 → 0003` | **PASS** — entity tables dropped, Stage 2A data intact |
| Earlier migrations modified? | **No** — `0001`, `0002`, `0003` unchanged |
| Schema drift | **NO DRIFT** — schema built by migrations compared column-by-column, index-by-index and FK-by-FK against the schema built from models; identical |

---

## Entity Extraction Verification

**Live, against the real Groq API — the specification's acceptance scenario:**

> "I am building Mai as a personal AI environment. I want to use PostgreSQL
> locally and Groq for fast inference."

Memories stored (3), then entities extracted from each:

| Entity | Type | Description (from the memory only) |
| --- | --- | --- |
| **Mai** | `project` | Personal AI environment the user is building. |
| **PostgreSQL** | `technology` | Database technology used locally by the user for the Mai project. |
| **Groq** | `company` | Company used for fast inference in the Mai project. |
| AI | `concept` | Artificial Intelligence referenced in the personal AI environment. |

All three expected entities were extracted, typed exactly per the documented
classification rules, with no fabricated enrichment.

**Entity reuse across memories — the core Stage 2B objective — verified live:**

```
Entity created  | entity_type=project        <- memory 1
Entity reused   | match=exact  project       <- memory 2
Entity reused   | match=exact  project       <- memory 3
Entity extraction completed | entities_created=1 entities_reused=1 linked=2
```

`Mai` was created **once** and reused twice, ending with `memory_count=3`
linking it to all three memories. `PostgreSQL` reached `memory_count=2` after a
later turn, again with no duplicate created.

**Automated type coverage** (mocked provider): technology, project, company,
person, concept all verified, plus the "extract nothing" case for
*"User likes working on interesting projects."*

---

## Entity Normalization

Fully deterministic — no model involved, so resolution cannot drift between
runs.

| Rule | Example |
| --- | --- |
| Unicode NFKC, trim, collapse whitespace | `"  PostgreSQL  "` → `PostgreSQL` |
| Strip edge punctuation | `"postgresql."` → `postgresql` |
| Lowercase (matching form only) | `POSTGRESQL` → `postgresql` |
| Drop leading article | `"the Mai project"` → `mai` |
| Drop trailing descriptor noun | `"PostgreSQL database"` → `postgresql` |
| Drop possessive | `"Mai's"` → `mai` |

**Canonical case is preserved.** `canonical_name` stays `PostgreSQL`; only
`normalized_name` is lowercased. Verified by test.

A descriptor that *is* the whole name survives — the entity "Database" does not
normalize to nothing.

---

## Entity Resolution

| Step | Match | Verified |
| --- | --- | --- |
| 1 | exact canonical name | **PASS** |
| 2 | normalized name (`postgresql`, `PostgreSQL database`) | **PASS** |
| 3 | alias (`postgres` → PostgreSQL) | **PASS** |
| 4 | compact form (`Postgre-SQL`, `A.I.`) | **PASS** |

**There is no fuzzy or similarity step**, deliberately. Every similarity rule
considered would also merge `Claude` with `Claude Code`, which the
specification names as a pair that must stay separate.

**First classification wins.** When the model relabels a known entity (`Groq`
as `company`, later as `technology`), the existing entity is reused and keeps
its original type rather than forking. Verified by test.

---

## Deduplication

| Scenario | Expected | Result |
| --- | --- | --- |
| Exact duplicate (`PostgreSQL` twice) | one entity | **PASS** |
| Case variation (`postgresql`, `POSTGRESQL`) | one entity | **PASS** |
| Descriptor variation (`PostgreSQL database`) | one entity | **PASS** |
| Alias variation (`postgres` after alias registered) | reuse | **PASS** |
| Punctuation variation (`Postgre-SQL`) | reuse | **PASS** |
| **`Claude` vs `Claude Code`** | **two entities** | **PASS** |
| `Mai` vs `Mai Chen` | two entities | **PASS** |
| `Groq` vs `Grok`, `AI` vs `API`, `PostgreSQL` vs `MySQL` | two entities | **PASS** |
| **Concurrent turns proposing the same entity** | one entity | **PASS** — 8 consecutive runs |
| Concurrent turns proposing the same alias | one alias | **PASS** |

The concurrency cases matter specifically because the Stage 2A audit found that
application-level uniqueness checks lose races. Entities carry a UNIQUE
constraint from day one for that reason, and `IntegrityError` is caught and
treated as a duplicate.

### Aliases

Refused when the alias equals the entity's own name, already names a different
entity, or is already registered elsewhere — verified by test. `Anthropic`
could not claim `Claude` as an alias while an entity named `Claude` existed.

---

## Memory Linking

| Property | Result |
| --- | --- |
| One memory → many entities | **PASS** — 3 entities from one memory |
| One entity → many memories | **PASS** — `Mai` linked to 3, live |
| Links are unique | **PASS** — composite primary key |
| Re-extracting a memory does not duplicate links | **PASS** — 3 runs, 1 link |
| Deleting an **entity** keeps the memories | **PASS** — live: 4 memories before and after |
| Deleting a **memory** keeps the entity | **PASS** |
| Deleting a conversation cascades memories, entities survive | **PASS** |
| Orphan check (raw SQL, live) | **PASS** — 0 dangling links, 0 dangling aliases |

---

## Failure Isolation

| Induced failure | Result |
| --- | --- |
| `LLMTimeoutError` during entity extraction | chat **201**, memory intact, 0 entities, logged |
| `LLMRateLimitError` / `LLMAuthError` / `LLMResponseError` | same |
| `RuntimeError` / `ValueError` (unexpected) | same — caught, never escapes |
| Malformed output (empty, non-JSON, truncated, wrong types) | chat **201**, memory intact, 0 entities |
| Invalid candidate (`entity_type="not_a_type"`, `confidence=5.0`) | rejected, memory intact |
| **Database failure during entity storage** | memory survives, 0 entities, 0 links, 0 aliases |
| Memory extraction failed | no entity extraction attempted |
| Trivial turn → no memory | no entity extraction attempted |
| Chat turn failed | neither extraction attempted |
| Chat response shape with entities on vs off | identical |

`run_entity_extraction` never raises and contains failures **per memory**, so
one bad memory does not stop the others from the same turn.

---

## APIs

| Endpoint | Verified live |
| --- | --- |
| `GET /api/entities` | 200 |
| `GET /api/entities?entity_type=technology` | 200 |
| `GET /api/entities?status=active` | 200 |
| `GET /api/entities?entity_type=database` (invalid) | **422** |
| `GET /api/entities?status=deleted` (invalid) | **422** |
| `GET /api/entities?limit=0` / `limit=9999` | **422** |
| `GET /api/entities?limit=2&offset=1` | 200 |
| `GET /api/entities/not-a-uuid` | **422** `validation_error` |
| `GET /api/entities/{unknown}` | **404** `entity_not_found` |
| `GET /api/entities/{id}` | 200 — aliases + `memory_count` |
| `GET /api/entities/{id}/memories` | 200 |
| `GET /api/entities/{unknown}/memories` | **404** |
| `DELETE /api/entities/{id}` | **204** — memories preserved |
| `DELETE /api/entities/{unknown}` | **404** |

Error envelopes match the Stage 1 format exactly. **Delete behaviour:**
permanent deletion of the entity, its aliases and its links; the underlying
memories are never touched.

---

## Provider Abstraction

**Confirmed provider agnostic.**

| Check | Result |
| --- | --- |
| Groq/SDK references in entity module *code* | **none** (the string "Groq" appears only as an example entity name in prompt text) |
| API key or `os.environ` access in entity modules | **none** |
| Imports | only `app.llm.base` — the ABC. No provider implementation. |
| Interface changes required | **none** — the `json_mode` argument added in Stage 2A was sufficient |

Stage 1 chat and Stage 2A memory behaviour are unchanged, confirmed by their
full suites passing.

---

## Regression Testing

| Suite | Result |
| --- | --- |
| Stage 1 (conversations, chat, providers, config, errors, health) | **105/105 PASS** |
| Stage 2A (memory extraction, dedup, API, isolation, concurrency, audit) | **158/158 PASS** |
| Stage 2B (entities) | **143/143 PASS** |
| Stage 1 files tracked in git | `chat_service.py`, `conversation_service.py`, `groq.py`, `factory.py`, `0001` — **all unchanged** |
| Live Stage 1 check | conversation context preserved: *"Your name is EntityUser."* |
| Live persistence across restart | 3 entities, 4 memories, 7 links, all intact |

---

## Tests

| Metric | Count |
| --- | --- |
| **Total** | **406** |
| **Passed** | **406** |
| **Failed** | **0** |
| **Skipped** | **0** |

| File | Tests |
| --- | --- |
| `test_entity_normalization.py` | 43 |
| `test_entity_extraction.py` | 42 |
| `test_memory_dedup.py` | 40 |
| `test_llm_provider.py` | 35 |
| `test_memory_extraction.py` | 35 |
| `test_entity_pipeline.py` | 25 |
| `test_memory_pipeline.py` | 24 |
| `test_memory_audit.py` | 21 |
| `test_memory_isolation.py` | 20 |
| `test_entity_isolation.py` | 19 |
| `test_config.py` | 19 |
| `test_entity_api.py` | 14 |
| `test_error_handling.py` | 14 |
| `test_chat_flow.py` | 13 |
| `test_conversations.py` | 12 |
| `test_memory_api.py` | 12 |
| `test_providers.py` | 9 |
| `test_memory_concurrency.py` | 6 |
| `test_health.py` | 3 |

Groq is mocked throughout; the suite needs no API key and no network.
Stability: repeated full runs and 8 consecutive concurrency-suite runs, all
green. Entity code: 1,313 LOC; entity tests: 1,288 LOC.

---

## Bugs Found

**B1 — Reserved `LogRecord` attribute silently discarded every entity.**
*(high — found only by live execution)*

`EntityService` logged its summary with `extra={"created": ..., "reused": ...}`.
`created` is a reserved `LogRecord` attribute, so Python's logging raises
`KeyError: "Attempt to overwrite 'created' in LogRecord"`. The exception fired
*after* the entity, alias and link writes but *before* the commit, so the task
caught it, rolled back, and produced **zero entities** — while the log showed
"Entity created" and "Memory linked to entity" moments earlier.

The failure isolation worked correctly (memory and chat were unaffected), but
the entity system produced nothing at all.

**The test suite could not have caught it:** the test settings used
`LOG_LEVEL="WARNING"`, so `logger.info()` short-circuited via `isEnabledFor()`
and never built a record. The bug was invisible until a real server ran at INFO.

**B2 — Request-session teardown began an empty transaction.** *(medium)*
`get_db_session` committed unconditionally at teardown. When the handler had
already committed (the chat route does, so the turn is durable before
background work), that autobegan an empty transaction. Harmless in production,
but on SQLite — where pooled sessions can share one connection — it collided
with concurrent work.

**B3 — Test helper held a transaction open across API calls.** *(test-only)*
A helper querying the `db_session` fixture left its transaction open, and with
`StaticPool` sharing one connection this blocked the next request from starting
one (`cannot start a transaction within a transaction`). A fixture artifact,
not a production bug — production pools hand out separate connections — but it
masked what was really happening until traced.

---

## Fixes Applied

| # | Fix |
| --- | --- |
| B1 | Renamed the colliding keys to `entities_created` / `entities_reused`. **Test settings now run at `LOG_LEVEL=INFO`**, so logging paths actually execute in tests. Added a test that scans every `extra={}` in `app/` for reserved `LogRecord` attributes, and a test asserting each success-path log line is emitted. |
| B2 | Teardown commits only when a transaction is actually open. |
| B3 | The test helper releases its transaction after counting, with a comment explaining the `StaticPool` interaction. |

**Tests added for the bugs:** the reserved-attribute scan, the INFO-level
logging assertion, and two entity-concurrency regressions.

---

## Known Limitations

Confirmed **not** implemented, by design and by scan (no entity-to-entity
foreign keys exist; only `entity_aliases` and `memory_entities` reference
`entities`):

- **No entity-to-entity relationships** — that is Stage 2C
- **No relationship graph or knowledge graph**, no Neo4j
- **No vector embeddings, vector database, or semantic entity search**
- **No RAG**
- **No web enrichment** — descriptions come only from the source memory
- **No advanced entity resolution** — no fuzzy matching, coreference, or
  cross-type disambiguation
- **No knowledge-graph reasoning**

Behavioural limitations worth knowing before Stage 2C:

- **Same-name collisions merge.** Resolution is by name alone, so a person
  named "Mai" and the project "Mai" would become one entity. This is the
  accepted cost of not forking entities every time the model relabels one — the
  far more common failure.
- **First classification wins permanently.** A mislabelled entity keeps its
  original type; there is no reclassification path.
- **Descriptions are never updated** once an entity exists; later mentions only
  add links.
- **No entity lifecycle.** Everything is `active`; `archived` is schema-only.
- **Extraction costs one model call per stored memory**, on top of the chat and
  memory calls. On Groq's free tier this was the practical bottleneck during
  live testing.
- **PostgreSQL runtime remains unverified** (no server available); the schema is
  verified, the behaviour is not.

---

## Stage 2C Readiness

### Is Mai ready to begin Stage 2C — Relationship System? **YES**

Reasoning:

- **Entities are stable, deduplicated identities.** `Mai` is one row referenced
  by three memories, verified live — not three near-duplicate rows. Relationships
  join entities, so a relationship system built on unstable identities would
  multiply every duplicate into duplicated edges. That risk is closed at the
  database level, not merely intended.
- **The join-table pattern Stage 2C needs already exists and is proven.**
  `memory_entities` demonstrates many-to-many linking with a composite primary
  key preventing duplicates, cascade semantics that delete links without
  deleting what they point at, and correct behaviour under concurrent writes.
  An `entity_relationships` table is the same shape.
- **Resolution is conservative and deterministic.** `Claude`/`Claude Code`,
  `Groq`/`Grok` and `Mai`/`Mai Chen` all stay separate. Stage 2C can trust that
  two different entity ids really are two different things.
- **Failure isolation now spans three layers** — chat, memory, entities — with
  ten distinct failure modes verified through the real HTTP path. Stage 2C adds
  a fourth analysis step onto a pattern that is demonstrably safe.
- **406 tests, zero failures, zero skips**, with Stage 1 and Stage 2A intact.

**Three things to carry into Stage 2C:**

1. **Extend the existing background task again — do not add a third writer.**
   The task already runs memory then entities in sequence; relationships belong
   in the same sequence. A concurrent writer would race the other two.
2. **Give `entity_relationships` a UNIQUE constraint from day one**, on
   `(source_entity_id, target_entity_id, relationship_type)`. Both Stage 2A and
   Stage 2B needed this and only Stage 2B had it up front.
3. **Watch the per-turn model-call count.** A turn already costs one chat call,
   one memory call, and one entity call per stored memory. Relationship
   extraction should reuse the entity-extraction call or run on a batch, not add
   another call per entity pair.

---

## Appendix — Reproduction

```bash
# Full suite (no API key or network needed)
cd backend && source .venv/bin/activate && pytest

# Entity suites specifically
pytest tests/test_entity_*.py

# Concurrency regressions
pytest tests/test_memory_concurrency.py

# Migrations: clean, onto Stage 2A data, and reversible
alembic upgrade head && alembic current
alembic downgrade 0003

# Live end-to-end (needs GROQ_API_KEY)
uvicorn app.main:app --reload
curl -X POST localhost:8000/api/conversations -H 'Content-Type: application/json' -d '{}'
curl -X POST localhost:8000/api/conversations/<id>/messages \
  -H 'Content-Type: application/json' \
  -d '{"content":"I am building Mai as a personal AI environment. I want to use PostgreSQL locally and Groq for fast inference."}'
curl localhost:8000/api/entities
curl localhost:8000/api/entities/<entity-id>/memories
```
