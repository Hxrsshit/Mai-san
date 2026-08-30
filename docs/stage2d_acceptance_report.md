# Mai — Stage 2D Acceptance Report

**Date:** 2026-08-30
**Scope:** Context Retrieval & Memory Assembly. No Stage 3 work performed.
**Baseline:** Stage 2C, 522 tests passing.

---

## Stage 2D Status

### PASS

Retrieval was verified by execution, including live cross-conversation recall
against the real Groq API. **613 tests pass, 0 failed, 0 skipped.** Stages 1,
2A, 2B and 2C all still pass in full.

**Environmental limitation, unchanged from earlier stages and not a defect:**
Docker, the Next.js frontend, and PostgreSQL at runtime could not be exercised
— that tooling is not installed on this machine. Runtime testing used SQLite;
Stage 2D adds no schema changes, and a reflected comparison confirms zero drift.

---

## Features Implemented

| Feature | Status |
| --- | --- |
| Query normalization (original message never modified) | **PASS** |
| Deterministic keyword extraction (no NLP dependency) | **PASS** |
| Multi-word phrase (n-gram) generation | **PASS** |
| Entity matching by canonical name, alias, normalized form | **PASS** |
| Bounded memory candidate retrieval (3 sources) | **PASS** |
| One-hop relationship retrieval | **PASS** |
| Deterministic weighted ranking | **PASS** |
| Importance / confidence reused from existing fields | **PASS** |
| Gentle recency decay | **PASS** |
| Deduplication of memories, entities, relationships | **PASS** |
| Two-stage context budget (counts, then characters) | **PASS** |
| Structured `ContextPackage` schema | **PASS** |
| Chat pipeline integration on the request path | **PASS** |
| Retrieval metadata (source, score, rank, signals) | **PASS** |
| `POST /api/retrieval/debug` | **PASS** |
| `GET /api/conversations/{id}/context-preview` | **PASS** |
| Graceful degradation per source | **PASS** |
| **Zero additional model calls** | **PASS** |

---

## Retrieval Architecture

```
user message
  → normalize + keywords + n-gram phrases     (deterministic, no model)
  → entity match (exact, indexed)
  → candidates: memories by entity link, by relationship evidence, by keyword
              + relationships one hop from matched entities
  → rank (weighted, deterministic)
  → deduplicate
  → budget (counts, then characters)
  → ContextPackage → chat prompt → LLM
```

Retrieval is on the **request path**, before the model call. Memory, entity and
relationship *extraction* remain on the background path, untouched.

---

## Ranking Formula

```
FINAL = 0.30 · text_relevance
      + 0.25 · entity_relevance
      + 0.15 · relationship_relevance
      + 0.15 · importance
      + 0.10 · confidence
      + 0.05 · recency
```

All six components are normalised to `[0, 1]` before weighting; all six are
configurable; the weights sum to exactly 1.0 (asserted by test).

**Relevance = 0.70, metadata = 0.30.** A highly important but irrelevant memory
cannot outrank a directly relevant one — verified by the specification's own
scenario: querying *"What database does Mai use?"* ranks
*"Mai uses PostgreSQL as its database"* (importance 5) above
*"User wants to change careers entirely"* (importance 10).

| Component | Definition |
| --- | --- |
| `text_relevance` | share of query keywords present in the memory |
| `entity_relevance` | strongest matched entity linked to it (1.0 / 0.9 / 0.8) |
| `relationship_relevance` | 0.6 when the memory is relationship evidence |
| `importance` | `(importance_score − 1) / 9` — Stage 2A's field, reused |
| `confidence` | `confidence_score` — already 0–1, reused directly |
| `recency` | `0.5 ^ (age_days / 180)` |

**Recency**: 1.00 today, 0.89 at 30 days, 0.50 at 180, 0.25 at a year. At weight
0.05 it only breaks ties — verified that a 400-day-old relevant memory outranks
a same-day irrelevant one.

**Tie-breaking**, in order: final score → entity strength → importance →
confidence → recency → memory id. Ordering is total, so identical input always
produces identical output (asserted).

---

## Entity Matching

Exact lookup of query n-grams against `normalized_name` and `normalized_alias`,
both UNIQUE-indexed. Two queries total regardless of query length.

| Verified | Result |
| --- | --- |
| Canonical name — *"How is Mai progressing?"* | **Mai** matched |
| Alias — *"Should I keep using Postgres?"* | **PostgreSQL** matched via alias |
| Multi-word — *"Tell me about AI product development"* | **AI Product Development** matched |
| Archived entities | not matched |
| `"postgres-like system"` (substring) | **no match** |
| `"Claud is a name"` (near-miss) | **no match** |

**No fuzzy matching.** Documented behaviour: *"Claude Code"* matches a stored
`Claude` entity because the word genuinely appears — mild over-retrieval, not a
fabricated match, and once a `Claude Code` entity exists both match.

---

## Memory Retrieval

Three bounded sources, unioned; a memory found by several keeps all signals.

| Source | Index used |
| --- | --- |
| entity link (`memory_entities`) | `ix_memory_entities_entity_id` |
| relationship evidence | `uq_relationship_evidence_pair` |
| keyword (`normalized_content LIKE`, OR'd into one query) | `ix_memories_status_created_at` |

Verified: a memory sharing **no keywords** with the query is still retrieved via
its entity link.

---

## Relationship Retrieval

One hop, strictly. Verified with a three-link chain:

```
Mai ──USES──► PostgreSQL ──USES──► PostGIS ──DEPENDS_ON──► AWS
```

Querying "Tell me about Mai" retrieves **only** `Mai USES PostgreSQL`. The
two-hop and three-hop links are absent.

Both-endpoints-matched outranks one-endpoint-matched (verified).

---

## Context Assembly

```
PERSONAL KNOWLEDGE CONTEXT

Relevant memories:
- User is building Mai as a personal AI environment.
- User decided to use PostgreSQL for local storage.
- User switched from OpenRouter to Groq for inference.

Relevant entities:
- Mai (project)
- PostgreSQL (technology)

Known connections:
- User BUILDS Mai
- User USES PostgreSQL

This is background knowledge from earlier conversations. Use it only when it is
relevant to the current request. If it conflicts with what the user is saying
now, the user is right -- treat this as possibly out of date. ...
```

**Prompt order** (asserted by test): system prompt → knowledge block → recent
conversation → **user's current message last**, closest to the model's
attention. The block's own text defers to the current message.

---

## Context Budget

| Check | Result |
| --- | --- |
| Count budgets enforced | **PASS** — 10 candidates → 3 with `MAX_MEMORIES=3` |
| Highest-ranked survive truncation | **PASS** — importance-10 memory kept, order descending |
| Character budget never exceeded | **PASS** |
| Lower-ranked dropped first | **PASS** |
| Never a partial section | **PASS** — a budget below the fixed overhead yields an empty context, not a corrupted one |
| Bounded pool at 150 memories | **PASS** — pool capped, context capped |

---

## Zero Additional Model Calls

**Verified three ways.**

1. **Structural.** The retrieval package imports `app.core`, `app.entities`,
   `app.memory`, `app.relationships` — and *nothing* from `app.llm`. It has no
   provider reference and no way to make a model call.
2. **Automated.** A chat turn with retrieval enabled and a seeded knowledge base
   makes exactly **one** chat generation call. Calling `RetrievalService`
   directly makes **zero** calls of any kind.
3. **Live.** Across 10 retrievals in the live run, zero retrieval-triggered
   model calls. The request path remains one chat call; extraction calls come
   from the background task (they share a correlation id because the background
   task inherits it).

---

## Manual End-to-End Verification

Knowledge was built across **five separate conversations**, then queried from a
**sixth, empty one** — against the real Groq API.

Stored: 5 memories, 8 entities, 5 relationships.

> **New conversation, zero prior messages.**
> **USER:** "Remind me — what am I using for storage and inference, and what am
> I building?"
>
> **MAI:**
> - **Storage:** PostgreSQL (local database)
> - **Inference:** Groq (you switched from OpenRouter)
> - **What you're building:** Mai – a personal AI environment/assistant.

Retrieval: 3 memories, 546 context chars, **2.16 ms**, no degraded sources.

Every fact came from a different earlier conversation — including the
OpenRouter→Groq switch. This is the complete loop: **learn → store → structure
→ retrieve → use.**

**Debug endpoint on the same knowledge base:**

```
QUERY: "Am I still using PostgreSQL and Groq?"
  keywords: ['using', 'postgresql', 'groq']
  entities: ['PostgreSQL', 'Groq']
    1. [0.688] User decided to use PostgreSQL for local storage.
    2. [0.671] User switched from OpenRouter to Groq for inference.
       -> User USES Groq
       -> User USES PostgreSQL
```

**One expected result did not occur, and it is worth stating plainly.** The
specification's scenario expects `Mai USES PostgreSQL`. What was stored is
`User USES PostgreSQL`. Stage 2C extracts relationships **per memory**, and the
memory *"I decided to use PostgreSQL for local storage"* never mentions Mai —
so no link between them could be inferred. Consequently, a query phrased
*"What technology stack am I using for Mai?"* reaches Mai's own relationship
(`User BUILDS Mai`) but not the storage/inference ones. Rephrasing to
*"What am I using for storage and inference?"* retrieves both correctly.

This is a **knowledge-structure limitation inherited from Stage 2C**, not a
retrieval defect: retrieval faithfully returns what the graph contains.

---

## Failure Isolation

| Induced failure | Result |
| --- | --- |
| Entity matching raises | chat **201**, other sources still used |
| Memory retrieval raises | chat **201** |
| Relationship retrieval raises | chat **201**, `degraded_sources` records it |
| Entire retrieval subsystem raises | chat **201**, empty context, conversation still reaches the model |
| `RETRIEVAL_ENABLED=false` | chat works, no knowledge block, conversation context intact |
| Empty / stopword-only query | nothing retrieved, no error |

`RetrievalService.retrieve()` never raises; `ChatService` wraps it a second
time as defence in depth.

---

## Performance

| Property | Measured |
| --- | --- |
| SELECTs per retrieval | **8, constant** from 10 → 500 memories |
| Query count growth | **none** — asserted equal at 5 and 205 memories |
| Candidate pool | bounded by `RETRIEVAL_CANDIDATE_POOL_SIZE` |
| Live latency | min 1.31 ms, **median 4.23 ms**, max 830 ms (one cold outlier) |
| Full table load | never |

`noload()` suppresses the `selectin` collections on `Entity.aliases` and
`Relationship.evidence`, which retrieval does not read — 10 queries → 8.

**No schema changes and no migration.** Every query is served by an index that
already exists.

---

## APIs

| Endpoint | Verified |
| --- | --- |
| `POST /api/retrieval/debug` | keywords, matched entities, candidates with full score breakdowns, selections, assembled context, weights |
| `POST /api/retrieval/debug` empty query | **422** `validation_error` |
| `POST /api/retrieval/debug` empty knowledge base | empty selections, empty context |
| Debug output contains no secrets | verified against `gsk_`, `api_key`, `password` |
| `GET /api/conversations/{id}/context-preview` | uses the last user message |
| `…/context-preview?query=Postgres` | explicit query honoured |
| `…/context-preview` on empty conversation | empty result, no error |
| `…/context-preview` unknown / malformed id | **404** / **422** |

---

## Tests

| Metric | Count |
| --- | --- |
| **Total** | **613** |
| **Passed** | **613** |
| **Failed** | **0** |
| **Skipped** | **0** |

| Stage | Tests |
| --- | --- |
| Stage 1 | 105 |
| Stage 2A | 160 |
| Stage 2B | 143 |
| Stage 2C | 114 |
| **Stage 2D** | **91** |

| Stage 2D file | Tests |
| --- | --- |
| `test_retrieval_query.py` | 31 |
| `test_retrieval_pipeline.py` | 29 |
| `test_retrieval_integration.py` | 17 |
| `test_retrieval_api.py` | 14 |

Groq is mocked throughout; the suite needs no API key and no network. Stability:
three consecutive full runs, all green.

---

## Bugs Found

**D1 — Two wasted queries per retrieval.** *(medium)* `Entity.aliases` and
`Relationship.evidence` are `lazy="selectin"`, so loading either emitted an
extra collection query even though retrieval reads neither. Found by
instrumenting query counts rather than by inspection: 10 SELECTs where 8 suffice.

**D2 — A tiny character budget produced an empty context.** *(low, by design
once understood)* The fixed header and footer are ~350 characters, so a budget
below that strips every item. The behaviour is correct — an empty context beats
a corrupted one — but it was undocumented and my first test asserted the wrong
thing.

**D3 — `next()` inside a coroutine masked a real failure.** *(test-only)* A bare
`next(generator)` raising `StopIteration` inside an async function surfaces as
an opaque `RuntimeError`, hiding which assertion actually failed.

**D4 — A test asserted against unrealistic seed data.** *(test-only)* The
long-term-recall test seeded *"long-term goal is to move toward AI product
development"* and queried *"career direction"* — no shared words. It failed for
a real reason, and revealed the recall boundary now documented below.

**D5 — Test fixtures collided with earlier stages' constraints.** *(test-only)*
Stage 2A's `UNIQUE(memory_type, normalized_content)` and Stage 2B's
`UNIQUE(normalized_name)` blocked fixtures that created identical rows —
correct enforcement, wrong fixtures.

---

## Fixes Applied

| # | Fix |
| --- | --- |
| D1 | `noload()` on both collections in the matcher and relationship retriever. 10 → 8 queries, constant across 500 memories. Two tests now guard it: an absolute bound and a growth comparison. |
| D2 | Documented, plus an explicit test that a sub-overhead budget yields an empty (never partial) context. |
| D3 | Replaced with a helper returning `None`, so failures report the real assertion. |
| D4 | Seed data now uses the wording Stage 2A actually produces, and a separate test documents the lexical-overlap boundary. |
| D5 | Fixtures use distinct memory types / entity names, exercising context-level deduplication rather than fighting the constraints. |

---

## Known Limitations

- **No embeddings, vector database, or semantic search.**
- **No RAG framework, no retrieval model, no query rewriting.**
- **Recall is bounded by lexical overlap.** A query sharing no words and no
  entity with a memory will not find it: *"What are my professional
  aspirations?"* does not retrieve *"career toward AI product development"*.
  This is the single biggest limitation and needs embeddings to close. There is
  an explicit test documenting it.
- **No multi-hop graph traversal.** One hop only.
- **No contradiction resolution.** Conflicting memories may both be retrieved;
  the context block warns the model that background knowledge may be stale.
- **No temporal knowledge reasoning.** A superseded fact looks current apart
  from its recency score.
- **No retrieval caching.** Every turn re-runs retrieval (~4 ms).
- **Retrieval quality depends on upstream structure**, as the `Mai USES
  PostgreSQL` case above shows.
- **Stopword list is fixed and English-only.**

---

## Stage 2 Completion Verdict

### Is Stage 2 — Knowledge Foundation — complete? **YES**

The loop closes. Mai now:

| | Capability | Stage |
| --- | --- | --- |
| **Learns** | selectively extracts meaningful memories | 2A |
| **Structures** | identifies the things they refer to | 2B |
| **Connects** | records how those things relate, with evidence | 2C |
| **Retrieves** | surfaces the relevant subset before replying | 2D |
| **Uses** | answers from knowledge gathered in other conversations | 2D |

Demonstrated end to end against a real model: five separate conversations of
knowledge, correctly recalled and synthesised in a sixth with no prior context.

Every layer carries database-level integrity (unique constraints, cascades,
zero orphans verified), full provenance (memory → conversation and message;
relationship → evidence → memory), and failure isolation verified through the
real HTTP path. **613 tests, zero failures, zero skips.**

---

## Stage 3 Readiness

### Is Mai ready for Stage 3 — Personal Intelligence & Proactive Reasoning? **YES**

Reasoning:

- **Knowledge is now actionable, not just stored.** Proactive reasoning needs to
  *read* what Mai knows on demand; that is exactly what Stage 2D provides, in
  4 ms and with a documented, tunable ranking.
- **Retrieval is deterministic and inspectable.** The debug endpoint shows every
  candidate, its score breakdown and whether it was selected. Stage 3 will make
  judgements from retrieved context, and a reasoning layer built on opaque
  retrieval would be impossible to debug.
- **The context budget is already enforced.** Stage 3 will add its own material
  competing for the same window; the ranking-then-budget mechanism extends to it
  without redesign.
- **Failure isolation spans five layers** — chat, memory, entities,
  relationships, retrieval — each verified by induced failure through the real
  HTTP path.
- **Provenance survives end to end**, so a proactive claim can always be traced
  to the memory and conversation behind it.

**Three things to carry into Stage 3:**

1. **Semantic retrieval is now the highest-value next investment.** The lexical
   boundary is real and measured. Proactive reasoning will surface fewer useful
   connections than it could until embeddings close that gap — and Stage 2D's
   ranking already has a natural slot for a semantic component alongside the
   existing six.
2. **Watch the context budget, not just the model-call count.** Retrieval,
   conversation and Stage 3's own reasoning material will compete for one
   window. The budget exists; the weights between *sources* do not yet.
3. **Knowledge quality is upstream of reasoning quality.** The `User USES
   PostgreSQL` versus `Mai USES PostgreSQL` case shows a fragmented graph
   limits what any layer above it can conclude. Cross-memory entity linking is
   worth revisiting before reasoning depends on it.

---

## Appendix — Reproduction

```bash
# Full suite (no API key or network needed)
cd backend && source .venv/bin/activate && pytest

# Stage 2D only
pytest tests/test_retrieval_*.py

# Live (needs GROQ_API_KEY): build knowledge, then ask from a new conversation
uvicorn app.main:app --reload

curl -X POST localhost:8000/api/retrieval/debug \
  -H 'Content-Type: application/json' \
  -d '{"query":"What am I using for storage and inference?"}'

curl "localhost:8000/api/conversations/<id>/context-preview"
```
