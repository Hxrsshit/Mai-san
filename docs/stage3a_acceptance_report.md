# Mai — Stage 3A Acceptance Report

**Date:** 2026-08-30
**Scope:** Context Assembly Foundation. No Stage 3B work performed.
**Baseline:** Stage 2D, 613 tests passing.

---

## Status

### PASS

**672 tests pass, 0 failed, 0 skipped**, stable across three consecutive runs.
Stages 1, 2A, 2B, 2C and 2D all still pass in full.

**Environmental limitation, unchanged and not a defect:** Docker, the Next.js
frontend, and PostgreSQL at runtime could not be exercised — that tooling is
not installed on this machine. Stage 3A adds no schema changes and no frontend
code. Runtime testing used SQLite.

---

## Two architectural conflicts, resolved explicitly

**1. Name collision.** Stage 2D already defined a type called `ContextPackage`.
The specification states that "Stage 2D ends at RetrievalResult", so 2D's type
was **renamed to `RetrievalResult`** — a mechanical rename with a backward
alias, no behaviour change — freeing `ContextPackage` for Stage 3A's assembled
whole. All 613 prior tests passed unchanged after the rename.

**2. Stage 2D already injects retrieval into the chat prompt**, which conflicts
with Stage 3A's "do not inject context into prompts yet". Removing that would
regress an accepted stage. **Stage 2D's chat integration was left exactly as
is**, and Stage 3A's assembler was built alongside it, deliberately **not**
wired into `ChatService`. Stage 3B will switch the pipeline over and retire the
inline path.

Verified: `ChatService` contains no reference to `ContextService` or
`app.context`.

---

## Features Implemented

| Feature | Status |
| --- | --- |
| `ContextPackage` schema with separate categories | **PASS** |
| Current message preserved byte-for-byte | **PASS** |
| Recent conversation, bounded and chronological | **PASS** |
| Stage 2D `RetrievalResult` consumed, never recomputed | **PASS** |
| `context_role` distinguishing reference from instruction | **PASS** |
| Category limits (messages, memories, entities, relationships) | **PASS** |
| Total character budget as final authority | **PASS** |
| Documented drop order, whole items only | **PASS** |
| Dropped items recorded with reason and rank | **PASS** |
| Per-category character accounting | **PASS** |
| Pluggable `Sizer` for future token budgeting | **PASS** |
| Graceful degradation per source | **PASS** |
| `POST /api/context/debug` | **PASS** |
| Read-only — no database mutation | **PASS** |
| No model calls, structurally guaranteed | **PASS** |

---

## Architecture

```
current message ─────────────────────────┐
                                         │
conversation_id ──► ConversationService ──┤
                    get_messages(limit)   │
                    (newest N, oldest-1st)│
                                         ├──► ContextAssembler ──► ContextPackage
current message ──► RetrievalService ─────┤         │
                    .retrieve()           │         ├─ category limits
                    (Stage 2D, once)      │         ├─ total budget
                    → RetrievalResult ────┘         └─ character accounting
```

`app/context/` — `schemas.py`, `budget.py`, `assembler.py`, `service.py`.

---

## ContextPackage

```
ContextPackage
├── current_message : str
├── recent_conversation : [RecentMessage]   role, content, created_at
├── memories       : [ContextMemory]        content, type, importance,
│                                           confidence, rank, score
├── entities       : [ContextEntity]        name, type, description, rank
├── relationships  : [ContextRelationship]  source, type, target, confidence
└── metadata       : ContextMetadata        counts, characters, budget,
                                            dropped_items, degraded_sources
```

Every item carries `context_role`. Retrieved items are always `REFERENCE`;
conversation is `CONVERSATION`; **nothing is ever `INSTRUCTION`** — asserted.

Compactness verified by test: entities expose no `normalized_name`, aliases,
timestamps or status; relationships expose no evidence or endpoint ids;
memories expose no `normalized_content`, `source_conversation_id` or status.

---

## Context Priority

| Rank | Source | Guarantee |
| --- | --- | --- |
| 1 | system instructions | reserved; Stage 3A assigns nothing here |
| 2 | **current user message** | preserved exactly, never dropped |
| 3 | recent conversation | bounded; yields only after long-term knowledge |
| 4 | retrieved knowledge | reference only; dropped first under pressure |

**Verified:** with old memory *"User uses OpenRouter."* and current message
*"I switched to Groq."*, the message is preserved verbatim and the memory
appears separately as `REFERENCE`. With a 10-character budget against a
47-character message, the message still survives and the package is minimal.

---

## Context Budgeting

**Stage 1 — category limits.** Recent conversation trims from the front (oldest
first); long-term categories keep their highest-ranked items.

**Stage 2 — total budget**, the final authority. Order of sacrifice:

```
relationships (lowest rank) → entities → memories → oldest messages
   the current message is never eligible
```

Long-term knowledge yields before short-term conversation — verified by a test
where the conversation survives and all long-term knowledge is dropped.

| Check | Result |
| --- | --- |
| Category limits enforced independently | **PASS** — 6→3 memories, 5→2 entities, 5→2 relationships |
| Highest-ranked survive | **PASS** — ranks 1,2,3 kept |
| Total budget never exceeded | **PASS** |
| Lowest-ranked dropped first | **PASS** — rank 1 always survives |
| Items never partially truncated | **PASS** — every surviving memory matches its original exactly |
| Memory text never altered | **PASS** — including whitespace and em dashes |
| Dropped items recorded | **PASS** — with category, reason and rank |

---

## Short-Term Context

Reuses `ConversationService.get_messages(limit=N)`, which already selects the
newest N and returns them oldest-first. No second implementation was written.

**Verified against the database:** with `CONTEXT_RECENT_MESSAGE_LIMIT=5` and 20
stored messages, exactly `turn 15` … `turn 19` are selected, in chronological
order. Roles and timestamps preserved.

---

## Long-Term Context

Stage 2D's result is consumed as given.

| Check | Result |
| --- | --- |
| Retrieval called exactly once | **PASS** — asserted by counting invocations |
| Ranking order preserved | **PASS** — ranks 1–5 emerge in order |
| Scores carried through, not recomputed | **PASS** — 0.7331 in, 0.7331 out |
| Both-endpoints relationships flagged, not re-ranked | **PASS** — flag set, order unchanged |
| No independent knowledge-base query | **PASS** — 9 SELECTs total, 8 of them Stage 2D's |

---

## Failure Isolation

| Induced failure | Result |
| --- | --- |
| `RetrievalService.retrieve` raises | message + conversation survive; `degraded_sources: ["long_term_knowledge"]` |
| `get_messages` raises | message + knowledge survive; `degraded_sources: ["recent_conversation"]` |
| Both raise | message alone; package still valid; both sources recorded |
| Unknown conversation id | degrades cleanly, no exception |

`ContextService.build()` never raises. The current message is present in every
outcome.

---

## No Database Mutation

Verified two independent ways:

1. **Snapshot** — row counts across `memories`, `entities`, `memory_entities`,
   `relationships`, `relationship_evidence`, plus every memory's `content` and
   `updated_at`, identical before and after assembly.
2. **Statement listener** — **zero** `INSERT`, `UPDATE` or `DELETE` statements
   issued during assembly, and separately during a debug-endpoint call.

---

## No Model Calls

1. **Structural** — a test walks the ASTs of every module in `app/context/` and
   asserts none imports `app.llm`. Assembly *cannot* call a model.
2. **Behavioural** — after a full assembly, the fake provider's chat,
   memory-extraction, entity and relationship call lists are all empty.
3. **Endpoint** — call counts are identical before and after `POST
   /api/context/debug`.

---

## Debug API

```
POST /api/context/debug
{ "conversation_id": "…" (optional), "message": "What stack am I using for Mai?" }
```

Returns the `ContextPackage` itself — categories separate, ranks preserved,
budget and dropped items in `metadata`.

| Check | Result |
| --- | --- |
| Returns the assembled package | **PASS** |
| Reports counts, budget, character totals, duration | **PASS** |
| Preserves retrieval rank; all items `reference` | **PASS** |
| Records dropped items with reasons | **PASS** |
| Works without `conversation_id` | **PASS** — long-term knowledge only |
| Unknown conversation id | **PASS** — 200, degrades |
| Empty message / malformed id | **422** |
| No model call, no mutation, no writes | **PASS** |
| No secrets (`gsk_`, `api_key`, `test-key`, …) | **PASS** |
| No database internals in output | **PASS** |

---

## Performance

| Messages / memories | SELECTs | Selected | Chars | Latency |
| --- | --- | --- | --- | --- |
| 10 / 10 | 9 | 10 msg, 10 mem | 654 | 10.4 ms |
| 50 / 50 | 9 | 12 msg, 10 mem | 704 | 8.8 ms |
| 120 / 120 | 9 | 12 msg, 10 mem | 720 | 9.3 ms |
| 200 / 300 | 9 | 12 msg, 10 mem | 727 | 9.3 ms |

**9 SELECTs, constant** — Stage 2D's 8 plus one indexed, `LIMIT`-ed query for
recent conversation. No N+1; retrieval is invoked exactly once. Selection and
context size stay bounded at every scale.

---

## Tests

| Metric | Count |
| --- | --- |
| **Total** | **672** |
| **Passed** | **672** |
| **Failed** | **0** |
| **Skipped** | **0** |

| Stage | Tests |
| --- | --- |
| Stage 1 | 105 |
| Stage 2A | 160 |
| Stage 2B | 143 |
| Stage 2C | 114 |
| Stage 2D | 91 |
| **Stage 3A** | **59** |

| Stage 3A file | Tests |
| --- | --- |
| `test_context_assembly.py` | 28 |
| `test_context_service.py` | 18 |
| `test_context_api.py` | 13 |

---

## Bugs Found

**A1 — `MissingGreenlet` from reading expired ORM instances.** *(test-only)*
The no-mutation test read `memory.content` and `memory.updated_at` after a
`rollback()`. Rollback expires instances, and touching an expired attribute
triggers a lazy refresh that cannot run outside async context.

**A2 — Repeated `commit()`/`rollback()` corrupted the shared test connection.**
*(test-only)* Under `StaticPool` with `isolation_level=None` and explicit
`BEGIN IMMEDIATE` (the Stage 2A configuration), churning transactions in a
verification helper left the connection unusable — surfacing as
`terminate_force_close() not implemented`, which hid the real cause.

No production defects were found in Stage 3A. Both issues were in the tests
themselves; the assembly layer behaved correctly throughout.

---

## Fixes Applied

| # | Fix |
| --- | --- |
| A1 | The snapshot selects **columns** (`Memory.id, content, updated_at`) rather than ORM instances, so nothing can expire. |
| A2 | The no-mutation test stays inside one transaction. Assembly writes nothing, so there is nothing to commit — removing the churn entirely. |
| — | `noload`-free by design: Stage 3A issues one extra query beyond Stage 2D's eight, verified by a statement listener. |

---

## Known Limitations

Explicitly, as required:

- **The `ContextPackage` is not yet injected into the LLM.** Stage 3A stops at
  the package.
- **No prompt formatting.** Nothing is rendered to text.
- **No new LLM behaviour.** Chat still uses Stage 2D's simpler inline
  rendering, unchanged.
- **Character-based budgeting only.** Token-aware budgeting needs only a
  different `Sizer`, demonstrated by test, but is not implemented.
- **No advanced conflict resolution.** A stale memory and a contradicting
  current message both appear; the package records the distinction and resolves
  nothing.
- **No semantic compression or summarisation.** Items are whole or absent.
- **No autonomous reasoning.**
- **Final prioritization is removal only** — Stage 3A never re-orders Stage 2D's
  ranking.
- **Inherited from Stage 2D:** retrieval recall is bounded by lexical overlap;
  assembly can only work with what retrieval found.

---

## Acceptance Criteria

| # | Criterion | Status |
| --- | --- | --- |
| 1 | Stage 2D reused, not duplicated | **PASS** — retrieval called exactly once, order preserved |
| 2 | Current message always preserved | **PASS** — including under an impossible budget |
| 3 | Short-term and long-term structurally separate | **PASS** |
| 4 | ContextPackage deterministic | **PASS** — identical input, identical output |
| 5 | Context budgets enforced | **PASS** — category and total |
| 6 | Ranked order preserved | **PASS** |
| 7 | Items never partially truncated | **PASS** |
| 8 | No database knowledge modified | **PASS** — snapshot and statement listener |
| 9 | No LLM calls added | **PASS** — structural and behavioural |
| 10 | Works with missing optional sources | **PASS** — all three scenarios |
| 11 | Debug visibility | **PASS** |
| 12 | Configuration tested | **PASS** — all five values change behaviour |
| 13 | Previous stages functional | **PASS** — 613/613 |
| 14 | Full regression suite passes | **PASS** — 672/672 |
| 15 | Unverified systems not marked PASS | **PASS** — Docker/frontend/PostgreSQL marked NOT VERIFIED |

---

## Stage 3B Readiness

**Stage 3B — prompt integration — has everything it needs.**

The package is deliberately un-rendered: categories separate, nothing
pre-formatted, `context_role` already distinguishing reference from
instruction, and `connects_matched_entities` already flagged for whichever
relationships deserve prominence. Stage 3B decides presentation; Stage 3A has
made every decision that precedes it.

**Three things to carry in:**

1. **Retire Stage 2D's inline rendering when wiring this in.** `ChatService`
   currently renders retrieval directly. Leaving both paths active would send
   knowledge twice.
2. **Honour `context_role` at the prompt boundary.** Reference content must not
   reach a system-instruction position — that is the whole reason the field
   exists.
3. **Swap the `Sizer` when token budgeting arrives.** Every measurement already
   routes through one function; changing it changes nothing else.

---

## Appendix — Reproduction

```bash
# Full suite (no API key or network needed)
cd backend && source .venv/bin/activate && pytest

# Stage 3A only
pytest tests/test_context_*.py

# Inspect an assembled package (needs the server running; makes no model call)
uvicorn app.main:app --reload
curl -X POST localhost:8000/api/context/debug \
  -H 'Content-Type: application/json' \
  -d '{"message":"What technology stack am I using for Mai?"}'
```
