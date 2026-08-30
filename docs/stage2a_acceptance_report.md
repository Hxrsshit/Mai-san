# Mai — Stage 2A Acceptance Report

**Date:** 2026-08-29
**Scope:** Verification audit of the Stage 2A Memory Foundation. No Stage 2B work performed.
**Baseline:** Stage 1 at commit `4e26dfe` (105 tests).

> This is the post-audit report. It supersedes the implementation-time report:
> the audit found and fixed four defects that the original Stage 2A test suite
> did not catch, three of them concurrency- or persistence-related.

---

## Stage 2A Status

### PASS

The memory pipeline was verified by execution, including live extraction
against the real Groq API for all five memory types. **261 tests pass, 0
failed, 0 skipped**, stable across five consecutive full runs and ten
consecutive runs of the concurrency suite. All 105 Stage 1 tests still pass and
every Stage 1 core file is byte-for-byte unchanged.

**Environmental limitation, unchanged from Stage 1 and not a defect:** Docker,
the Next.js frontend, and PostgreSQL at runtime could not be exercised —
that tooling is not installed on this machine (Python 3.9.6 and git only).
Runtime testing used SQLite; the PostgreSQL schema was verified by offline DDL
rendering and a reflected-schema comparison showing zero drift. Stage 2A adds
no frontend code.

---

## Requirements Checklist

### Memory extraction

| Requirement | Status | Evidence |
| --- | --- | --- |
| Meaningful information is extracted | **PASS** | 5/5 types extracted live by the real model |
| Trivial messages do not become memories | **PASS** | "Hello, how are you?", "Thanks!", "Hello!" → `candidates=0` |
| Extraction focuses on user-provided information | **PASS** | Prompt labels roles; assistant reply marked context-only |
| Assistant claims cannot become user memories | **PASS** | Verified by prompt-content test; hedged case produced nothing |
| Content is understandable standalone | **PASS** | Every live memory is a third-person standalone statement |

### Memory types

| Requirement | Status |
| --- | --- |
| `semantic`, `preference`, `goal`, `decision`, `episodic` implemented | **PASS** |
| All five verified **live** with the real model | **PASS** |
| Invalid types cannot be stored | **PASS** — rejected at validation; enum enforced in the database |

### Importance / confidence / status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Importance constrained 1–10 | **PASS** | Pydantic + `CHECK` constraint; 0, 11, −3, "high" all rejected |
| Importance threshold configurable and effective | **PASS** | Values 1–4 rejected at default; lowering to 3 admits a 4 |
| Confidence constrained 0.0–1.0 | **PASS** | −0.1, 1.5, "very sure" rejected |
| Confidence threshold configurable and effective | **PASS** | 0.0/0.3/0.5/0.69 rejected; lowering to 0.5 admits 0.6 |
| Status limited to `active`/`superseded`/`archived` | **PASS** | Enum asserted exactly |
| New memories default to `active` | **PASS** | Verified in storage and via API |

---

## Memory Pipeline Verification

The verified pipeline:

```
turn completed → response returned to user → turn COMMITTED
   → background task (own session)
   → extraction via LLMProvider (json_mode)
   → JSON recovery → Pydantic validation
   → importance/confidence thresholds
   → dedup: verb normalisation → exact (indexed, unbounded)
            → conflict guard → similarity → restatement
   → savepoint-wrapped insert (unique constraint enforces the invariant)
```

---

## Manual Memory Tests (live, real Groq `openai/gpt-oss-120b`)

| Test | Input | Result | Type | Imp | Conf |
| --- | --- | --- | --- | --- | --- |
| **A — Goal** | "I want to transition my career toward AI product development." | "User wants to transition their career toward AI product development." | `goal` | 8 | 0.95 |
| **B — Preference** | "I prefer concise answers with practical examples." | "User prefers concise answers with practical examples." | `preference` | 6 | 1.00 |
| **C — Decision** | "I have decided to use PostgreSQL as the initial database for Mai." | "User decided to use PostgreSQL as the initial database for Mai." | `decision` | 7 | 0.97 |
| **D — Semantic** | "I am currently building a personal AI project called Mai." | "User is building a personal AI project called Mai." | `semantic` | 7 | 1.00 |
| **E — Episodic** | "I completed the first stage of building Mai today." | "User completed the first stage of building Mai." | `episodic` | 7 | 0.97 |
| **F — Trivial** | "Hello, how are you?" | **no memory** | — | — | — |
| **F — Trivial** | "Thanks!" | **no memory** | — | — | — |
| **F — Trivial** | "Hello!" | **no memory** | — | — | — |

Every stored memory carried a resolvable `source_conversation_id` **and**
`source_message_id`.

### Memory quality

Judged against the specification's criteria:

- **Standalone** — all five read correctly with no conversation context.
- **Accurate, no invention** — none added facts the user did not state.
- **No conversational wording** — no "user said", no pronouns without referent.
- **Sensible generalisation** — the episodic memory dropped "today", which
  would not age well, while keeping the milestone.
- **Uncertainty preserved.** The critical case:
  *"I think I might want to explore AI product development."* produced
  **no memory at all** — the system did not inflate a hedge into a confident
  goal. The specification's suggested ideal was the softer *"User is
  interested in exploring…"*; storing nothing is **more** conservative than
  that, consistent with "better to miss some memories than to pollute".
  Worth noting as a behavioural tendency: hedged statements are under-extracted
  rather than softened.

---

## Database Verification

| Check | Result |
| --- | --- |
| `memories` table exists with all 11 columns | **PASS** |
| UUID primary keys | **PASS** |
| FK → `conversations.id` `ON DELETE CASCADE` | **PASS** |
| FK → `messages.id` `ON DELETE SET NULL` | **PASS** |
| Status defaults to `active` | **PASS** |
| Timestamps populated | **PASS** |
| Indexes present | **PASS** — `(status, created_at)`, `(memory_type)`, `(source_conversation_id)`, **UNIQUE** `(memory_type, normalized_content)` |
| **Persistence across backend restart** | **PASS** — 4 memories intact after full restart |
| Cascade delete | **PASS** — deleting a conversation removed its 3 memories |
| Orphan check (raw SQL) | **PASS** — 0 dangling conversation refs, 0 dangling message refs |
| FK integrity enforced | **PASS** — inserting a memory with a nonexistent conversation raises |

### Migrations

| Check | Result |
| --- | --- |
| Stage 1 migration `0001` unmodified | **PASS** — unchanged since the Stage 1 commit |
| Stage 2A migrations are separate | **PASS** — `0002` (table), `0003` (uniqueness, from this audit) |
| Clean database → head | **PASS** — 3 migrations, `current: 0003 (head)` |
| Existing Stage 1 database **with data** → head | **PASS** — conversation and message preserved |
| Downgrade → `0001` | **PASS** — reversible, Stage 1 data intact |
| **Schema drift** | **NO DRIFT** — schema built by migrations compared column-by-column, index-by-index and FK-by-FK against the schema built from models; identical |

---

## Deduplication Verification

Verified against 16 hand-built pairs, all classified correctly, plus live.

**Exact duplicates** — caught, unbounded, and now enforced by a database unique
constraint. Live: a restatement produced identical canonical content and was
rejected (`reason=exact similarity=1.0`).

**Near duplicates** — caught:

| Existing | Candidate | Caught via |
| --- | --- | --- |
| "User prefers practical explanations." | "The user likes practical explanations." | similarity 0.83 |
| "User prefers practical explanations." | "User likes explanations that are practical." *(spec's example)* | verb normalisation → 1.00 |
| "…concise answers with practical examples." | "…practical examples and short answers." | restatement (containment 0.80) |
| "…transition their career toward…" | "…move their career towards…" | similarity 0.90 |
| "User decided to use Postgres." | "User chose to use Postgres." | verb normalisation |

**False-positive protection** — these must NOT merge, and do not:

| Pair | Prevented by |
| --- | --- |
| "Stage 1 of Mai" / "Stage 2 of Mai" *(similarity 0.97 — higher than any true duplicate)* | conflict guard (number) |
| "lives in Bangalore" / "lives in Berlin" | conflict guard (proper noun) |
| "prefers dark mode" / "prefers light mode" | below threshold |
| "prefers concise explanations" / "prefers practical examples" | below threshold |
| "likes coffee" / "likes coffee in the morning before work" | length ratio 0.40 |
| "wants to learn Rust" / "decided to learn Rust" | different verb strength, not synonyms |

The bias is deliberate: wrongly merging destroys a fact, wrongly keeping a
near-duplicate only adds noise.

---

## Failure Isolation

| Induced failure | Result |
| --- | --- |
| `LLMTimeoutError` during extraction | chat **201**, both messages stored, 0 memories, logged |
| `LLMRateLimitError` | same |
| `LLMAuthError` | same |
| `LLMResponseError` | same |
| `RuntimeError` / `ValueError` (unexpected) | same — caught, never escapes the task |
| Malformed output (empty, non-JSON, truncated, wrong types) | chat **201**, 0 memories |
| Invalid candidate (`memory_type="entity"`, `importance=99`, `confidence=7.5`) | chat **201**, 0 memories |
| **Database failure during memory storage** | chat unaffected, **0 memories persisted**, logged |
| **Mid-batch database failure** | savepoint rolled back; **no half-written batch** |
| Chat turn itself fails | **no extraction attempted** — nothing to derive from |
| Response shape with memory on vs off | identical |

Live: with an invalid API key the server stayed healthy (`/health` 200) and
created no memories. Note that an invalid key breaks chat too, since both share
the provider — the isolated case (extraction failing while chat succeeds) is
covered by the 20 automated isolation tests.

---

## Provider Abstraction

**Confirmed provider agnostic.**

| Check | Result |
| --- | --- |
| Groq/SDK references in `app/memory/` | **none** |
| API keys read directly by memory modules | **none** — resolved through `Settings.active_*` |
| Memory imports | only `app.llm.base` (the ABC) and `app.llm.factory` (registry) |
| Provider-specific response assumptions | none — the extractor parses generic JSON text |

The interface change Stage 2A required was one optional argument,
`json_mode`, on `generate_response()`. It is a capability request, not a vendor
format: a provider without native JSON mode may ignore it, and callers validate
regardless. Default `False`, so Stage 1 chat behaviour is unchanged — confirmed
by all 105 Stage 1 tests passing untouched.

---

## Regression Testing

| Stage 1 behaviour | Result |
| --- | --- |
| Create conversation | **PASS** |
| Send message, receive response | **PASS** |
| Second message preserves context | **PASS** — "Your name is **AuditUser**." |
| Context window grows correctly | **PASS** — `context_messages=2` → `4` |
| Retrieve history | **PASS** — 4 messages |
| Persist across backend restart | **PASS** |
| Delete conversation | **PASS** — 204, cascade correct |
| Stage 1 core files unmodified | **PASS** — `0001`, `chat_service`, `conversation_service`, both models, Groq provider, factory all unchanged |
| Stage 1 test suite | **PASS** — 105/105 |

---

## Tests

| Metric | Count |
| --- | --- |
| **Total** | **261** |
| **Passed** | **261** |
| **Failed** | **0** |
| **Skipped** | **0** |

Stage 1: 105. Stage 2A: 156 (126 at implementation + 30 added by this audit).

| File | Tests |
| --- | --- |
| `test_memory_dedup.py` | 40 |
| `test_llm_provider.py` | 35 |
| `test_memory_extraction.py` | 35 |
| `test_memory_pipeline.py` | 24 |
| `test_memory_audit.py` | 21 |
| `test_memory_isolation.py` | 20 |
| `test_config.py` | 19 |
| `test_error_handling.py` | 14 |
| `test_chat_flow.py` | 13 |
| `test_conversations.py` | 12 |
| `test_memory_api.py` | 12 |
| `test_providers.py` | 9 |
| `test_memory_concurrency.py` | 4 |
| `test_health.py` | 3 |

Groq is mocked throughout; the suite needs no API key and no network.
Stability: 5 consecutive full runs and 10 consecutive concurrency-suite runs,
all green.

---

## Bugs Found

Four defects, all found by execution rather than review.

**A1 — Exact duplicates escaped the deduplication window.** *(medium)*
`normalized_content` was written and indexed but **never read**: dedup
normalised `memory.content` at runtime and compared only against the 50 most
recent same-type memories. An identical memory older than that window was
stored again. Reproduced: with the window set to 5, re-submitting identical
content produced **2 copies**. The architecture doc's claim that the column
gave "an indexed read instead of a table scan" was inaccurate.

**A2 — Deduplication raced under concurrent turns.** *(high)* Concurrent
extractions in the same conversation each open their own session; each ran the
dedup query before any committed, so none saw the others. Reproduced at roughly
a **50% failure rate**: `deduplication raced: 2 copies stored`. Application-level
checks cannot see another transaction's uncommitted insert — no amount of
application logic fixes this.

**A3 — SAVEPOINT silently broken on SQLite.** *(high)* Introduced while fixing
A2. Wrapping each insert in `begin_nested()` so one rejected candidate cannot
abort the batch does not work on the pysqlite driver, which emits its own
implicit `BEGIN` at the wrong moments. A savepoint that should have been
discarded was instead **persisted**: a failed batch left its first memory
behind. This is a documented SQLAlchemy pitfall, and it was invisible until a
test compared what actually survived a mid-batch failure.

**A4 — The test fixture did not match production configuration.** *(medium)*
`conftest` built its engine with only two pragmas while production applied a
different set. That divergence is what allowed A3 to hide, and is the same
class of gap that hid the commit-ordering bug during implementation.

**Also assessed and found NOT to be bugs:** intra-batch fuzzy deduplication
(works — a flushed candidate is visible to the next candidate's query);
`MEMORY_ENABLED=false` (chat, context, persistence and deletion all fine, routes
correctly unregistered, zero extraction calls); logging privacy (only ids,
scores, counts and types — no message content, no memory content, no keys).

---

## Fixes Applied

| # | Fix |
| --- | --- |
| A1 | Exact-duplicate lookup is now an **indexed, unbounded** query on `normalized_content`, scoped to the same type, with full-text confirmation to guard against truncation collisions. The column is no longer dead weight. |
| A2 | Migration `0003` adds a **UNIQUE index on `(memory_type, normalized_content)`**, widens the column to 1000 chars so the constraint cannot collide distinct long memories, and de-duplicates any existing rows first. `IntegrityError` is caught and treated as a duplicate. |
| A3 | SQLite connections now disable the driver's implicit transaction handling and emit `BEGIN IMMEDIATE` explicitly, which makes SAVEPOINT behave correctly *and* keeps `busy_timeout` effective on write-lock contention. Serialises SQLite transactions; PostgreSQL is untouched. |
| A4 | `configure_sqlite()` is now shared: the test fixture applies **exactly** the production configuration. |
| — | Deduplication additionally gained **verb-synonym normalisation**, which fixes the specification's own near-duplicate example (0.52 → 1.00) with zero regressions across all 16 cases. |

**Tests added:** 30 (`test_memory_audit.py` plus additions to
`test_memory_dedup.py` and `test_memory_concurrency.py`), covering every bug
above as a regression.

---

## Remaining Issues

**Could not be tested — tooling absent on this machine** (no Docker, Node, npm,
`psql`, or Python 3.12):

1. **Docker / `docker compose up`** — never executed.
2. **Next.js frontend** — never built or run. Stage 2A adds no frontend code.
3. **PostgreSQL at runtime** — SQLite used instead. Schema verified by offline
   DDL rendering and reflected-schema comparison (zero drift), but no
   PostgreSQL server was exercised. **In particular, the unique-constraint and
   SAVEPOINT behaviour from fixes A2/A3 was verified on SQLite only.** Both are
   standard PostgreSQL features and PostgreSQL is the easier case, but this
   should be confirmed once a server is available.

**Known behavioural notes, not defects:**

4. Hedged statements tend to be dropped entirely rather than stored with
   softened wording — more conservative than the specification's ideal.
5. Extraction adds one model call per turn, which on Groq's free tier
   (30 req/min, 6,000 tokens/min) roughly halves effective throughput. Hit
   during live testing.
6. SQLite transactions are serialised as a consequence of fix A3.

---

## Known Stage 2A Limitations

Confirmed **not** implemented, by design and by scan:

- No entity system, entity tables, aliases, or entity resolution
- No relationship extraction or knowledge graph (no Neo4j)
- No vector database, embeddings, or semantic retrieval
- No RAG
- No contradiction resolution — conflicting memories coexist; nothing is superseded
- No memory lifecycle intelligence — everything stays `active`
- No memory retrieval into chat — memories are stored and inspectable, not yet fed back
- No ranking beyond `importance_score` — no recency, decay, or reinforcement
- No autonomous planning, agents, or personality learning

A scan for 13 future-stage markers across `app/` returned only two benign
matches: SQLAlchemy's `relationship()` in unchanged Stage 1 models, and
FastAPI's `Query(alias="status")` in the memory route.

**Deduplication is lexical.** Two memories with identical meaning but no shared
vocabulary will both be stored. Verb synonyms are a fixed list, not
lemmatisation. Closing this properly requires embeddings, which are out of
scope.

---

## Stage 2B Readiness

### Is Mai Stage 2A stable enough to begin Stage 2B — Entity System? **YES**

Reasoning:

- **The memory store now has an integrity guarantee, not just an intention.**
  Before this audit, deduplication was advisory: it lost races and missed
  duplicates outside its window. Entities will hang off memories, so duplicate
  memories would have multiplied into duplicate entity links. That is fixed at
  the database level.
- **The trust boundary is proven.** Nothing the model emits reaches the
  database without passing Pydantic validation — tested against 12
  malformed-output shapes and 13 invalid field values. Stage 2B can reuse the
  pattern rather than reinvent it.
- **Provenance resolves.** Every memory traces to a real conversation and a
  real *user* message, verified by following the foreign keys through the API.
  Entity extraction can attach to memories that already answer "where did this
  come from?".
- **Failure isolation is demonstrated, not assumed.** Ten distinct failure
  modes verified through the real HTTP path, including database failure and
  mid-batch failure. Stage 2B adds another post-turn analysis step onto a
  pattern that is now known-good.
- **The provider abstraction absorbed a second, quite different workload**
  (structured extraction) via one optional argument. Good evidence it will
  absorb entity extraction too.
- **261 tests, zero failures, zero skips, stable across repeated runs**, with
  Stage 1 untouched.

**Two things to carry into Stage 2B:**

1. **Extend the existing background task; do not add a second one.** Two
   concurrent post-turn writers would recreate A2-style races on a different
   table. Entity extraction should run inside the task that already exists.
2. **Give entity tables the same database-level uniqueness from day one.**
   A1 and A2 were both cases of application-level checks being insufficient.
   Entity resolution has exactly the same shape, and will have the same
   problem if it relies on application checks alone.

---

## Appendix — Reproduction

```bash
# Full suite (no API key or network needed)
cd backend && source .venv/bin/activate && pytest

# Concurrency regressions specifically
pytest tests/test_memory_concurrency.py tests/test_memory_audit.py

# Migrations: clean, onto Stage 1 data, and reversible
alembic upgrade head && alembic current
alembic downgrade 0001

# PostgreSQL DDL without a server
DATABASE_URL="postgresql+asyncpg://mai:mai@localhost:5432/mai" \
  alembic upgrade head --sql

# Live extraction (needs GROQ_API_KEY)
uvicorn app.main:app --reload
curl -X POST localhost:8000/api/conversations -H 'Content-Type: application/json' -d '{}'
curl -X POST localhost:8000/api/conversations/<id>/messages \
  -H 'Content-Type: application/json' \
  -d '{"content":"I want to transition my career toward AI product development."}'
curl localhost:8000/api/memories
```
